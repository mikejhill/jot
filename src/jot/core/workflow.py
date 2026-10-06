"""Single transition authority and atomic, owner-checked lease operations."""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise

from jot.core.models import Clock, EventKind, Flow, Status, Task
from jot.core.prioritize import Prioritizer
from jot.db.connection import Database
from jot.db.query import TaskQuery
from jot.db.repository import ProjectRepository, TaskRepository
from jot.exceptions import WorkflowError


class Workflow:
    """Validate both flows and serialize state changes with their audit events."""

    def __init__(self, database: Database, default_flow: Flow = Flow.PLANNED) -> None:
        self.db = database
        self.tasks = TaskRepository(database)
        self.projects = ProjectRepository(database)
        self.default_flow = default_flow

    def flow_for(self, task: Task, override: Flow | None = None) -> Flow:
        """Resolve run override, task, project, then global policy."""
        if override is not None:
            return override
        if task.flow is not None:
            return task.flow
        if task.project_id is not None:
            project = self.projects.get(task.project_id)
            if project.default_flow is not None:
                return project.default_flow
        return self.default_flow

    @staticmethod
    def validate(
        current: Status, target: Status, flow: Flow, *, resume: Status | None = None
    ) -> None:
        """Reject skipped gates; blocked tasks resume at the recorded previous state."""
        sequence = (
            Status.INBOX,
            Status.READY,
            Status.PLANNING,
            Status.AWAITING_APPROVAL,
            Status.EXECUTING,
            Status.REVIEW,
            Status.DONE,
        )
        if flow == Flow.DIRECT:
            sequence = (
                Status.INBOX,
                Status.READY,
                Status.EXECUTING,
                Status.REVIEW,
                Status.DONE,
            )
        edges = dict(pairwise(sequence))
        valid = edges.get(current) == target
        if current == Status.BLOCKED:
            valid = target == resume
        if current in {Status.DONE, Status.WONT_DO}:
            valid = target == Status.ARCHIVED
        if current in sequence[:-1] or current == Status.BLOCKED:
            valid = valid or target in {Status.BLOCKED, Status.WONT_DO, Status.ARCHIVED}
        if current in {
            Status.REVIEW,
            Status.AWAITING_APPROVAL,
            Status.PLANNING,
            Status.EXECUTING,
        } and (target == Status.READY):
            valid = True
        if current == target or not valid:
            raise WorkflowError(f"Invalid {flow} transition: {current} -> {target}")

    def move(
        self,
        task_id: int,
        target: Status,
        *,
        actor: str = "cli",
        flow: Flow | None = None,
        manual: bool = False,
    ) -> Task:
        """Move an unclaimed task through the validated status machine.

        Manual (user) moves may not enter planning/executing; only runs do.
        """
        with self.db.write():
            task = self.tasks.get(task_id)
            if task.deleted_at is not None or task.claimed_by is not None:
                raise WorkflowError(
                    "Release the claim or restore the task before moving it"
                )
            if manual and target in {Status.PLANNING, Status.EXECUTING}:
                raise WorkflowError(
                    f"{target} is set by starting a run (Plan / Run now), not by a move"
                )
            resolved = self.flow_for(task, flow)
            self.validate(
                task.status, target, resolved, resume=task.blocked_prior_status
            )
            blocked = task.status if target == Status.BLOCKED else None
            completed = Clock.now() if target == Status.DONE else task.completed_at
            self.db.connection.execute(
                "UPDATE tasks SET status=?,flow=?,blocked_prior_status=?,"
                "completed_at=?,updated_at=? WHERE id=?",
                (
                    target,
                    flow or task.flow,
                    blocked,
                    Clock.stamp(completed) if completed else None,
                    Clock.stamp(),
                    task_id,
                ),
            )
            self._event(task, target, actor, "move")
        return self.tasks.get(task_id)

    def claim(
        self,
        task_id: int,
        agent: str,
        target_status: Status,
        lease_seconds: int = 300,
        *,
        flow: Flow | None = None,
    ) -> bool:
        """Acquire one eligible task with a conditional update under BEGIN IMMEDIATE."""
        self._lease_inputs(agent, lease_seconds)
        if target_status not in {Status.PLANNING, Status.EXECUTING}:
            raise WorkflowError("Claims must target planning or executing")
        with self.db.write():
            task = self.tasks.get(task_id)
            if task.deleted_at is not None or task.claimed_by is not None:
                return False
            resolved = self.flow_for(task, flow)
            self.validate(task.status, target_status, resolved)
            expiry = Clock.stamp(Clock.now() + timedelta(seconds=lease_seconds))
            cursor = self.db.connection.execute(
                "UPDATE tasks SET status=?,flow=?,claimed_by=?,lease_expires_at=?,"
                "lease_prior_status=?,updated_at=? WHERE id=? AND status=? "
                "AND deleted_at IS NULL AND claimed_by IS NULL "
                "AND (lease_expires_at IS NULL OR lease_expires_at < ?)",
                (
                    target_status,
                    resolved,
                    agent,
                    expiry,
                    task.status,
                    Clock.stamp(),
                    task_id,
                    task.status,
                    Clock.stamp(),
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._event(task, target_status, agent, "claim")
            return True

    def next(self, *, project: str | None = None, limit: int = 1) -> list[Task]:
        """Rank eligible ready tasks, excluding archived projects and active claims."""
        if limit < 0:
            raise WorkflowError("limit must be non-negative")
        projects = self.projects.list()
        archived = {p.id for p in projects if p.archived}
        tasks = [
            t
            for t in self.tasks.query(
                TaskQuery(project=project, statuses=(Status.READY,))
            )
            if t.claimed_by is None and t.project_id not in archived
        ]
        return Prioritizer({p.id: p.priority for p in projects}).rank(tasks)[:limit]

    def claim_next(
        self,
        agent: str,
        flow: Flow | None = None,
        project: str | None = None,
        *,
        lease_seconds: int = 300,
    ) -> Task | None:
        """Pick and claim the highest-priority ready task in one write transaction."""
        self._lease_inputs(agent, lease_seconds)
        with self.db.write():
            choices = self.next(project=project)
            if not choices:
                return None
            task = choices[0]
            resolved = self.flow_for(task, flow)
            target = Status.PLANNING if resolved == Flow.PLANNED else Status.EXECUTING
            if self.claim(task.id, agent, target, lease_seconds, flow=resolved):
                return self.tasks.get(task.id)
        return None

    def heartbeat(self, task_id: int, agent: str, lease_seconds: int = 300) -> bool:
        """Extend a still-live lease only for its owner."""
        self._lease_inputs(agent, lease_seconds)
        with self.db.write() as connection:
            expiry = Clock.stamp(Clock.now() + timedelta(seconds=lease_seconds))
            cursor = connection.execute(
                "UPDATE tasks SET lease_expires_at=? WHERE id=? AND claimed_by=? "
                "AND lease_expires_at > ? AND deleted_at IS NULL",
                (expiry, task_id, agent, Clock.stamp()),
            )
            # Heartbeats are deliberately not audited; they would flood timelines.
            return cursor.rowcount == 1

    def release(
        self, task_id: int, agent: str, *, target_status: Status | None = None
    ) -> bool:
        """Restore the pre-claim state or publish a valid result as the lease owner."""
        with self.db.write():
            task = self.tasks.get(task_id)
            if (
                task.claimed_by != agent
                or task.lease_expires_at is None
                or task.lease_expires_at <= Clock.now()
            ):
                return False
            target = target_status or task.lease_prior_status
            if target is None:
                raise WorkflowError("Claim has no prior status")
            if target_status is not None:
                self.validate(task.status, target, self.flow_for(task))
            self._clear(task, target, agent, "release")
        return True

    def reap(self) -> list[int]:
        """Restore expired claims to their exact pre-claim states."""
        ids: list[int] = []
        with self.db.write():
            rows = self.db.connection.execute(
                "SELECT id FROM tasks WHERE lease_expires_at <= ? "
                "AND claimed_by IS NOT NULL ORDER BY id",
                (Clock.stamp(),),
            ).fetchall()
            for row in rows:
                task = self.tasks.get(row["id"])
                if task.lease_prior_status is None:
                    raise WorkflowError("Expired claim has no prior status")
                self._clear(task, task.lease_prior_status, "reaper", "reap")
                ids.append(task.id)
        return ids

    def _clear(self, task: Task, target: Status, actor: str, action: str) -> None:
        """Clear all lease fields together and append the resulting transition."""
        blocked = task.status if target == Status.BLOCKED else task.blocked_prior_status
        self.db.connection.execute(
            "UPDATE tasks SET status=?,claimed_by=NULL,lease_expires_at=NULL,"
            "lease_prior_status=NULL,blocked_prior_status=?,updated_at=? WHERE id=?",
            (target, blocked, Clock.stamp(), task.id),
        )
        self._event(task, target, actor, action)

    def _event(self, task: Task, target: Status, actor: str, action: str) -> None:
        """Record both endpoints and the reason for a state change."""
        self.tasks.audit(
            task.id,
            EventKind.STATUS,
            {"from": task.status, "to": target, "action": action},
            actor=actor,
        )

    @staticmethod
    def _lease_inputs(agent: str, lease_seconds: int) -> None:
        """Reject empty identities and non-positive lease durations."""
        if not agent.strip() or lease_seconds <= 0:
            raise WorkflowError(
                "agent must be non-empty and lease_seconds must be positive"
            )
