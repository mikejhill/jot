"""Cleanup heuristics, proposals, approval-only application, and agent review."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from jot.agents.base import AgentError, JsonObject
from jot.agents.fake import FakeBackend
from jot.config import Config, JotHome
from jot.core.models import Clock, Project, Status, Task
from jot.core.workflow import Workflow
from jot.db.connection import Database
from jot.db.repository import ProjectRepository, TaskRepository
from jot.exceptions import AppError, NotFoundError
from jot.services.cleanup import CleanupService
from jot.services.instructions import InstructionStore


class Seed:
    """Create tasks with controlled ages."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.tasks = TaskRepository(db)

    def task(
        self,
        title: str,
        *,
        days: int = 0,
        status: Status = Status.INBOX,
        **fields: object,
    ) -> Task:
        """Insert a task, walk it to ``status``, then backdate it."""
        values: dict[str, object] = {"title": title, "raw_input": title}
        values.update(fields)
        task = self.tasks.create(Task.model_validate(values))
        workflow = Workflow(self.db)
        path = {
            Status.READY: [Status.READY],
            Status.DONE: [Status.READY, Status.EXECUTING, Status.REVIEW, Status.DONE],
        }.get(status, [])
        flow_fields = {"flow": "direct"} if status is Status.DONE else {}
        if flow_fields:
            self.tasks.update(task.id, flow_fields)
        for target in path:
            workflow.move(task.id, target)
        stamp = Clock.stamp(Clock.now() - timedelta(days=days))
        with self.db.write() as connection:
            connection.execute(
                "UPDATE tasks SET updated_at=? WHERE id=?", (stamp, task.id)
            )
        return self.tasks.get(task.id)


@pytest.fixture
def seed(db: Database) -> Seed:
    """Task seeding helper."""
    return Seed(db)


class TestCleanup:
    """Proposal lifecycle."""

    def test_heuristics_and_apply(
        self, db: Database, home: JotHome, config: Config, seed: Seed, tmp_path: Path
    ) -> None:
        """Stale, duplicate, and missing-path items are proposed and applied."""
        stale = seed.task("Old inbox idea", days=40)
        ready = seed.task("Old ready work", days=70, status=Status.READY)
        done = seed.task("Finished long ago", days=100, status=Status.DONE)
        original = seed.task("Improve the health check endpoint")
        duplicate = seed.task("Improve the health check endpoints")
        missing = seed.task("Has a repo", repo_path=str(tmp_path / "gone"))
        fresh = seed.task("Brand new and unique")
        service = CleanupService(db, home, config)
        proposal_id = asyncio.run(service.scan())
        proposal = service.proposal(proposal_id)
        items = proposal["items"]
        assert isinstance(items, list)
        by_task = {item["task_id"]: item for item in items}
        assert {stale.id, ready.id, done.id, duplicate.id, missing.id} <= set(by_task)
        assert original.id not in by_task
        assert fresh.id not in by_task
        assert service.latest() == proposal_id

        approve = [
            by_task[stale.id]["index"],
            by_task[missing.id]["index"],
            by_task[done.id]["index"],
        ]
        affected = service.apply(proposal_id, approve)
        assert sorted(affected) == sorted([stale.id, missing.id, done.id])
        tasks = TaskRepository(db)
        assert tasks.get(stale.id).status is Status.ARCHIVED
        assert tasks.get(done.id).status is Status.ARCHIVED
        assert tasks.get(missing.id).status is Status.INBOX
        assert tasks.get(ready.id).status is Status.READY
        with pytest.raises(AppError, match="already applied"):
            service.apply(proposal_id, approve)

    def test_delete_action_and_idle_project(
        self, db: Database, home: JotHome, config: Config, seed: Seed
    ) -> None:
        """Agent items can soft-delete; idle projects are flagged for review."""
        project = ProjectRepository(db).create(Project(slug="old", name="Old"))
        idle = seed.task("Quiet project task", days=200, project_id=project.id)
        target = seed.task("Delete me")
        FakeBackend.canned = {
            "items": [
                {"task_id": target.id, "action": "delete", "reason": "obsolete"},
                {"task_id": target.id, "action": "explode", "reason": "bad"},
                "junk",
            ]
        }
        service = CleanupService(db, home, config)
        proposal_id = asyncio.run(service.scan(use_agent=True))
        items = service.proposal(proposal_id)["items"]
        assert isinstance(items, list)
        by_task = {item["task_id"]: item for item in items}
        assert by_task[target.id]["action"] == "delete"
        assert "agent:" in by_task[target.id]["reason"]
        assert idle.id in by_task
        service.apply(proposal_id, [by_task[target.id]["index"]])
        assert TaskRepository(db).get(target.id).deleted_at is not None

    def test_agent_failure_tolerated(
        self,
        db: Database,
        home: JotHome,
        config: Config,
        seed: Seed,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing agent pass still yields the heuristic proposal."""

        async def fail(
            self: FakeBackend, system: str, prompt: str, schema: JsonObject
        ) -> JsonObject:
            del self, system, prompt, schema
            raise AgentError("offline")

        monkeypatch.setattr(FakeBackend, "structured", fail)
        seed.task("Ancient", days=400)
        service = CleanupService(db, home, config)
        items = service.proposal(asyncio.run(service.scan(use_agent=True)))["items"]
        assert isinstance(items, list)
        assert len(items) == 1

    def test_missing(self, db: Database, home: JotHome, config: Config) -> None:
        """Unknown proposals raise; an empty database has no latest proposal."""
        service = CleanupService(db, home, config)
        assert service.latest() is None
        with pytest.raises(NotFoundError):
            service.proposal(42)


class TestInstructions:
    """Instruction loading and composition."""

    def test_compose(self, home: JotHome) -> None:
        """General guidance is followed by project overrides when present."""
        store = InstructionStore(home)
        (home.path / "instructions" / "projects" / "demo.md").write_text(
            "Run make test.", encoding="utf-8"
        )
        assert "Drawdown" in store.get("drawdown")
        assert store.get("missing") == ""
        assert store.for_project(None) == ""
        assert store.for_project("nope") == ""
        composed = store.compose("drawdown", "demo")
        assert composed.endswith("Run make test.")
        assert "Project-specific guidance (demo)" in composed
