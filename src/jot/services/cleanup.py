"""Scan for stale or duplicate tasks and apply only approved cleanup items."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from jot.agents.base import Json, JsonObject
from jot.agents.registry import BackendRegistry
from jot.core.models import Clock, EventKind, Status
from jot.core.workflow import Workflow
from jot.db.repository import ProjectRepository, TaskRepository
from jot.exceptions import AppError, NotFoundError
from jot.services.instructions import InstructionStore

if TYPE_CHECKING:
    from jot.config import Config, JotHome
    from jot.core.models import Task
    from jot.db.connection import Database

logger = logging.getLogger(__name__)

ACTOR = "cleanup"
DUPLICATE_RATIO = 0.85
AGENT_TASK_LIMIT = 200
OPEN = {Status.INBOX, Status.READY, Status.BLOCKED}
FINISHED = {Status.DONE, Status.WONT_DO}

AGENT_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task_id", "action", "reason"],
                "properties": {
                    "task_id": {"type": "integer"},
                    "action": {
                        "type": "string",
                        "enum": ["archive", "delete", "review"],
                    },
                    "reason": {"type": "string"},
                },
            },
        }
    },
}


class Action(StrEnum):
    """What applying a cleanup item does."""

    ARCHIVE = "archive"
    DELETE = "delete"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class CleanupItem:
    """One proposed change with its justification."""

    index: int
    task_id: int
    title: str
    action: Action
    reason: str


class CleanupService:
    """Produce cleanup proposals and apply approved items (archive by default)."""

    def __init__(self, db: Database, home: JotHome, config: Config) -> None:
        self.db = db
        self.home = home
        self.config = config
        self.tasks = TaskRepository(db)
        self.projects = ProjectRepository(db)
        self.workflow = Workflow(db, config.drawdown.default_flow)
        self.instructions = InstructionStore(home)

    async def scan(self, *, use_agent: bool = False) -> int:
        """Create a proposal from heuristics and an optional agent pass."""
        tasks = [t for t in self.tasks.list() if t.claimed_by is None]
        found: dict[int, tuple[Action, str]] = {}
        for task_id, action, reason in self._heuristics(tasks):
            found.setdefault(task_id, (action, reason))
        if use_agent:
            for task_id, action, reason in await self._agent(tasks, found):
                found[task_id] = (action, reason)
        titles = {t.id: t.title for t in tasks}
        items = [
            CleanupItem(index, task_id, titles[task_id], action, reason)
            for index, (task_id, (action, reason)) in enumerate(sorted(found.items()))
            if task_id in titles
        ]
        payload = json.dumps([asdict(item) for item in items], ensure_ascii=False)
        with self.db.write() as connection:
            cursor = connection.execute(
                "INSERT INTO cleanup_proposals(created_at,items,status) "
                "VALUES(?,?,'pending')",
                (Clock.stamp(), payload),
            )
        return int(cursor.lastrowid or 0)

    def proposal(self, proposal_id: int) -> dict[str, object]:
        """Return {id, created_at, status, items: [{index, task_id, title, ...}]}.

        Raises:
            NotFoundError: No such proposal.
        """
        row = self.db.connection.execute(
            "SELECT * FROM cleanup_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Cleanup proposal {proposal_id} not found")
        items = Json.loads(str(row["items"]))
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "status": row["status"],
            "items": items if isinstance(items, list) else [],
        }

    def latest(self) -> int | None:
        """Return the newest proposal id, if any."""
        row = self.db.connection.execute(
            "SELECT id FROM cleanup_proposals ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return int(row["id"]) if row else None

    def apply(self, proposal_id: int, approved: list[int]) -> list[int]:
        """Apply approved item indexes; return affected task ids.

        Raises:
            AppError: The proposal was already applied.
        """
        proposal = self.proposal(proposal_id)
        if proposal["status"] != "pending":
            raise AppError(
                f"Cleanup proposal {proposal_id} is already {proposal['status']}"
            )
        raw = proposal["items"]
        items = [Json.obj(i) for i in raw] if isinstance(raw, list) else []
        affected: list[int] = []
        for item in items:
            index = item.get("index")
            task_id = item.get("task_id")
            if index not in approved or not isinstance(task_id, int):
                continue
            try:
                self._apply_item(
                    task_id,
                    Json.text(item.get("action")),
                    Json.text(item.get("reason")),
                )
                affected.append(task_id)
            except AppError as err:
                logger.warning("cleanup skipped task %s: %s", task_id, err)
        with self.db.write() as connection:
            connection.execute(
                "UPDATE cleanup_proposals SET status='applied' WHERE id=?",
                (proposal_id,),
            )
        return affected

    def _apply_item(self, task_id: int, action: str, reason: str) -> None:
        """Archive, soft-delete, or annotate one task."""
        note = f"Cleanup ({action}): {reason}"
        with self.db.write():
            self.tasks.audit(task_id, EventKind.COMMENT, {"text": note}, actor=ACTOR)
        if action == Action.ARCHIVE:
            self.workflow.move(task_id, Status.ARCHIVED, actor=ACTOR)
        elif action == Action.DELETE:
            self.tasks.delete(task_id)

    def _heuristics(self, tasks: list[Task]) -> list[tuple[int, Action, str]]:
        """Age, duplicate, and missing-path rules from config thresholds."""
        limits = self.config.cleanup
        now = Clock.now()
        found: list[tuple[int, Action, str]] = []
        for task in tasks:
            age = (now - task.updated_at).days
            if task.status is Status.INBOX and age >= limits.inbox_days:
                found.append(
                    (task.id, Action.ARCHIVE, f"In inbox, untouched {age} days")
                )
            elif task.status is Status.READY and age >= limits.ready_days:
                found.append(
                    (task.id, Action.ARCHIVE, f"Ready but untouched {age} days")
                )
            elif task.status in FINISHED and age >= limits.done_days:
                found.append((task.id, Action.ARCHIVE, f"{task.status} for {age} days"))
            if task.repo_path and not Path(task.repo_path).expanduser().is_dir():
                found.append(
                    (task.id, Action.REVIEW, f"repo_path missing: {task.repo_path}")
                )
        found.extend(self._duplicates(tasks))
        found.extend(
            self._idle_projects(tasks, now - timedelta(days=limits.project_days))
        )
        return found

    def _duplicates(self, tasks: list[Task]) -> list[tuple[int, Action, str]]:
        """Flag the newer of two open tasks with near-identical titles."""
        open_tasks = sorted((t for t in tasks if t.status in OPEN), key=lambda t: t.id)
        found: list[tuple[int, Action, str]] = []
        for i, older in enumerate(open_tasks):
            for newer in open_tasks[i + 1 :]:
                if older.project_id != newer.project_id:
                    continue
                ratio = SequenceMatcher(
                    None, older.title.lower(), newer.title.lower()
                ).ratio()
                if ratio >= DUPLICATE_RATIO:
                    found.append(
                        (
                            newer.id,
                            Action.ARCHIVE,
                            f"Likely duplicate of #{older.id} ({ratio:.0%})",
                        )
                    )
        return found

    def _idle_projects(
        self, tasks: list[Task], cutoff: datetime
    ) -> list[tuple[int, Action, str]]:
        """Flag open tasks whose whole project has been idle past the cutoff."""
        found: list[tuple[int, Action, str]] = []
        by_project: dict[int, list[Task]] = {}
        for task in tasks:
            if task.project_id is not None:
                by_project.setdefault(task.project_id, []).append(task)
        for project_tasks in by_project.values():
            if all(t.updated_at < cutoff for t in project_tasks):
                name = self.projects.get(project_tasks[0].project_id or 0).name
                found.extend(
                    (t.id, Action.REVIEW, f"Project {name} has had no activity")
                    for t in project_tasks
                    if t.status in OPEN
                )
        return found

    async def _agent(
        self, tasks: list[Task], found: dict[int, tuple[Action, str]]
    ) -> list[tuple[int, Action, str]]:
        """Ask the triage backend to review candidates using cleanup.md."""
        now = Clock.now()
        lines = [
            f"#{t.id} [{t.status}] {t.title} (updated {(now - t.updated_at).days}d ago"
            + (f"; heuristic: {found[t.id][1]}" if t.id in found else "")
            + ")"
            for t in tasks[:AGENT_TASK_LIMIT]
            if t.status not in {Status.ARCHIVED}
        ]
        prompt = (
            f"## Cleanup guidance\n\n{self.instructions.get('cleanup')}\n\n"
            "## Tasks\n\n" + "\n".join(lines) + "\n\nPropose cleanup items. Keep "
            "heuristic items you agree with, drop ones you don't (omit them), and add "
            "others you find. Every item needs a specific reason."
        )
        agent = BackendRegistry.create(
            self.config.triage.backend, self.config.triage.model
        )
        try:
            data = await agent.structured(
                "You review a personal task list.", prompt, AGENT_SCHEMA
            )
        except AppError as err:
            logger.warning("agent cleanup pass failed: %s", err)
            return []
        raw = data.get("items")
        results: list[tuple[int, Action, str]] = []
        for entry in raw if isinstance(raw, list) else []:
            item = Json.obj(entry)
            task_id, action = item.get("task_id"), Json.text(item.get("action"))
            if isinstance(task_id, int) and action in Action:
                results.append(
                    (task_id, Action(action), "agent: " + Json.text(item.get("reason")))
                )
        return results
