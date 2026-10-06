"""Transition, ownership, atomic claiming, and lease rollback tests."""

from __future__ import annotations

import multiprocessing
from datetime import timedelta
from multiprocessing.synchronize import Barrier
from pathlib import Path

import pytest

from jot.config import JotHome
from jot.core.models import Clock, Flow, Project, Status, Task
from jot.core.workflow import Workflow
from jot.db.connection import Database
from jot.db.repository import ProjectRepository, TaskRepository
from jot.exceptions import RepositoryError, WorkflowError


class ClaimWorker:
    """Picklable process worker with an independent SQLite connection."""

    @staticmethod
    def claim(arguments: tuple[str, int, bool]) -> int:
        """Claim the shared id or next task and return the winning id."""
        path, task_id, next_task = arguments
        with Database.open(Path(path)) as db:
            workflow = Workflow(db)
            agent = f"agent-{multiprocessing.current_process().pid}"
            if next_task:
                task = workflow.claim_next(agent, Flow.PLANNED)
                return task.id if task else 0
            return task_id if workflow.claim(task_id, agent, Status.PLANNING) else 0

    @staticmethod
    def run(arguments: tuple[str, int, bool], output: str, barrier: Barrier) -> None:
        """Persist the result without multiprocessing's blocked named pipes."""
        barrier.wait()
        result = ClaimWorker.claim(arguments)
        Path(output).write_text(str(result), encoding="utf-8")

    @staticmethod
    def race(arguments: list[tuple[str, int, bool]], directory: Path) -> list[int]:
        """Start eight spawned processes and read their independent result files."""
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(len(arguments), timeout=30)
        outputs = [directory / f"claim-{i}.txt" for i in range(len(arguments))]
        processes = [
            context.Process(target=ClaimWorker.run, args=(args, str(output), barrier))
            for args, output in zip(arguments, outputs, strict=True)
        ]
        for process in processes:
            process.start()
        try:
            for process in processes:
                process.join(timeout=30)
                assert process.exitcode == 0
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
                process.close()
        return [int(path.read_text(encoding="utf-8")) for path in outputs]


class TestWorkflow:
    """Both flows preserve gates, leases, and audit history."""

    @pytest.mark.parametrize("flow", list(Flow))
    def test_flow(self, db: Database, flow: Flow) -> None:
        """Every adjacent transition succeeds and is audited."""
        workflow = Workflow(db, flow)
        task = workflow.tasks.create(Task(title="flow"))
        states = [
            Status.READY,
            Status.PLANNING,
            Status.AWAITING_APPROVAL,
            Status.EXECUTING,
            Status.REVIEW,
            Status.DONE,
            Status.ARCHIVED,
        ]
        if flow == Flow.DIRECT:
            states = [
                Status.READY,
                Status.EXECUTING,
                Status.REVIEW,
                Status.DONE,
                Status.ARCHIVED,
            ]
        for state in states:
            task = workflow.move(task.id, state)
            assert task.status == state
        assert task.completed_at is not None
        assert task.flow is None
        assert len(workflow.tasks.events.for_task(task.id)) == len(states) + 1

    @pytest.mark.parametrize("flow", list(Flow))
    def test_invalid_and_terminal(self, flow: Flow) -> None:
        """Skipped states and terminal resurrection are forbidden."""
        for current, target in (
            (Status.INBOX, Status.EXECUTING),
            (Status.DONE, Status.READY),
            (Status.ARCHIVED, Status.INBOX),
            (Status.READY, Status.READY),
        ):
            with pytest.raises(WorkflowError, match="Invalid"):
                Workflow.validate(current, target, flow)
        wrong = Status.EXECUTING if flow == Flow.PLANNED else Status.PLANNING
        with pytest.raises(WorkflowError):
            Workflow.validate(Status.READY, wrong, flow)

    def test_blocked_and_rework(self, db: Database) -> None:
        """Blocked tasks resume exactly where they stopped; review allows rework."""
        workflow = Workflow(db)
        task = workflow.tasks.create(Task(title="blocked"))
        workflow.move(task.id, Status.READY)
        workflow.move(task.id, Status.BLOCKED)
        with pytest.raises(WorkflowError):
            workflow.move(task.id, Status.EXECUTING)
        workflow.move(task.id, Status.READY)
        workflow.move(task.id, Status.WONT_DO)
        workflow.move(task.id, Status.ARCHIVED)
        Workflow.validate(Status.REVIEW, Status.READY, Flow.DIRECT)

    def test_policy_precedence(self, db: Database) -> None:
        """Per-run, task, project, and global flow defaults are resolved in order."""
        project = ProjectRepository(db).create(
            Project(slug="direct", name="Direct", default_flow=Flow.DIRECT)
        )
        workflow = Workflow(db)
        task = workflow.tasks.create(Task(title="policy", project_id=project.id))
        assert workflow.flow_for(task) == Flow.DIRECT
        workflow.tasks.update(task.id, {"flow": Flow.PLANNED})
        assert workflow.flow_for(workflow.tasks.get(task.id)) == Flow.PLANNED
        assert workflow.flow_for(task, Flow.PLANNED) == Flow.PLANNED

    def test_ready_keeps_inherited_flow(self, db: Database) -> None:
        """Moving to ready does not freeze an inherited project policy."""
        projects = ProjectRepository(db)
        project = projects.create(Project(slug="inherit", name="Inherited"))
        workflow = Workflow(db)
        task = workflow.tasks.create(Task(title="inherit", project_id=project.id))
        ready = workflow.move(task.id, Status.READY)
        assert ready.flow is None
        projects.update(project.id, {"default_flow": Flow.DIRECT})
        claimed = workflow.claim_next("agent")
        assert claimed is not None
        assert claimed.status == Status.EXECUTING

    def test_queue_and_direct_claim(self, db: Database) -> None:
        """Queue ranking respects project priority and excludes archived projects."""
        projects = ProjectRepository(db)
        low = projects.create(Project(slug="low", name="Low"))
        high = projects.create(
            Project(slug="high", name="High", priority=3, default_flow=Flow.DIRECT)
        )
        archived = projects.create(Project(slug="old", name="Old", archived=True))
        workflow = Workflow(db)
        tasks = [
            workflow.tasks.create(Task(title=project.slug, project_id=project.id))
            for project in (low, high, archived)
        ]
        for task in tasks:
            workflow.move(task.id, Status.READY)
        assert [task.id for task in workflow.next(limit=8)] == [
            tasks[1].id,
            tasks[0].id,
        ]
        claimed = workflow.claim_next("agent")
        assert claimed is not None
        assert claimed.id == tasks[1].id
        assert claimed.status == Status.EXECUTING
        with pytest.raises(RepositoryError, match="Release"):
            workflow.tasks.update(claimed.id, {"title": "changed"})
        with pytest.raises(RepositoryError, match="Release"):
            workflow.tasks.delete(claimed.id)
        assert workflow.release(claimed.id, "agent")
        assert workflow.claim_next("other", Flow.PLANNED, "low") is not None

    def test_owner_and_release(self, db: Database) -> None:
        """Only the live owner may heartbeat, publish, or cancel a lease."""
        workflow = Workflow(db)
        task = workflow.tasks.create(Task(title="lease"))
        workflow.move(task.id, Status.READY)
        assert workflow.claim(task.id, "alice", Status.PLANNING)
        assert not workflow.claim(task.id, "bob", Status.PLANNING)
        assert not workflow.heartbeat(task.id, "bob")
        assert workflow.heartbeat(task.id, "alice")
        assert not workflow.release(task.id, "bob")
        with pytest.raises(WorkflowError):
            workflow.move(task.id, Status.BLOCKED)
        assert workflow.release(
            task.id, "alice", target_status=Status.AWAITING_APPROVAL
        )
        assert workflow.claim(task.id, "alice", Status.EXECUTING)
        assert workflow.release(task.id, "alice")
        assert workflow.tasks.get(task.id).status == Status.AWAITING_APPROVAL

    @pytest.mark.parametrize("prior", [Status.READY, Status.AWAITING_APPROVAL])
    def test_reap_prior_status(self, db: Database, prior: Status) -> None:
        """Expired planning and execution claims restore their prior states."""
        workflow = Workflow(db)
        task = workflow.tasks.create(Task(title="expired"))
        workflow.move(task.id, Status.READY)
        if prior == Status.AWAITING_APPROVAL:
            workflow.move(task.id, Status.PLANNING)
            workflow.move(task.id, Status.AWAITING_APPROVAL)
        target = Status.PLANNING if prior == Status.READY else Status.EXECUTING
        assert workflow.claim(task.id, "agent", target)
        db.connection.execute(
            "UPDATE tasks SET lease_expires_at=? WHERE id=?",
            (Clock.stamp(Clock.now() - timedelta(seconds=1)), task.id),
        )
        assert not workflow.heartbeat(task.id, "agent")
        assert not workflow.release(task.id, "agent")
        assert workflow.reap() == [task.id]
        restored = workflow.tasks.get(task.id)
        assert restored.status == prior
        assert restored.claimed_by is None
        assert restored.lease_prior_status is None
        assert workflow.reap() == []

    def test_empty_deleted_archived_and_validation(self, db: Database) -> None:
        """Invalid lease inputs and ineligible work cannot be claimed."""
        workflow = Workflow(db)
        assert workflow.claim_next("agent") is None
        with pytest.raises(WorkflowError, match="positive"):
            workflow.claim_next("")
        task = workflow.tasks.create(Task(title="deleted"))
        with pytest.raises(WorkflowError, match="target"):
            workflow.claim(task.id, "a", Status.DONE)
        with pytest.raises(WorkflowError):
            workflow.heartbeat(task.id, "a", 0)
        with pytest.raises(WorkflowError):
            workflow.next(limit=-1)
        workflow.tasks.delete(task.id)
        assert not workflow.claim(task.id, "a", Status.PLANNING)
        with pytest.raises(WorkflowError):
            workflow.move(task.id, Status.READY)

    def test_claim_race(
        self, home: JotHome, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Eight independent processes claiming one task produce exactly one winner."""
        task = TaskRepository(db).create(Task(title="race"))
        Workflow(db).move(task.id, Status.READY)
        arguments = [(str(home.database), task.id, False)] * 8
        monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
        results = ClaimWorker.race(arguments, home.path)
        assert results.count(task.id) == 1
        assert results.count(0) == 7

    def test_claim_next_race(
        self, home: JotHome, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Concurrent next claims never duplicate assignments."""
        workflow = Workflow(db)
        ids = [workflow.tasks.create(Task(title=f"race {i}")).id for i in range(8)]
        for task_id in ids:
            workflow.move(task_id, Status.READY)
        monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
        results = ClaimWorker.race([(str(home.database), 0, True)] * 8, home.path)
        assert sorted(results) == ids


class TestManualMoves:
    """User-initiated moves cannot fake a run, and stuck runs can recover."""

    def test_manual_guard_and_recovery(self, db: Database) -> None:
        """Manual moves into planning fail; an unclaimed planning task returns."""
        workflow = Workflow(db)
        task = TaskRepository(db).create(Task(title="t", raw_input="t"))
        workflow.move(task.id, Status.READY)
        with pytest.raises(WorkflowError, match="starting a run"):
            workflow.move(task.id, Status.PLANNING, manual=True)
        workflow.move(task.id, Status.PLANNING)
        assert workflow.move(task.id, Status.READY, manual=True).status is Status.READY
