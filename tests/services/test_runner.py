"""Runner flows: planned, direct, worktrees, failures, cancellation, send-back."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jot.agents.base import AgentEvent, AgentEventKind, RunRequest
from jot.agents.fake import FakeBackend
from jot.config import Config, JotHome
from jot.core.models import EventKind, Flow, Project, Run, Status, Task
from jot.core.workflow import Workflow
from jot.db.connection import Database
from jot.db.repository import ProjectRepository, RunRepository, TaskRepository
from jot.exceptions import AppError, WorkflowError
from jot.services.bus import EventBus
from jot.services.runner import PlanOutcome, RunService
from jot.services.workspace import GitWorkspaces, Workspace, WorkspaceError


class Harness:
    """Create ready tasks and drive runs to completion."""

    def __init__(self, db: Database, home: JotHome, config: Config) -> None:
        self.db = db
        self.service = RunService(db, home, config, EventBus())
        self.tasks = TaskRepository(db)

    def ready(self, title: str = "Add a thing", **fields: object) -> Task:
        """Create a task and move it to ready."""
        values: dict[str, object] = {"title": title, "raw_input": title}
        values.update(fields)
        task = self.tasks.create(Task.model_validate(values))
        return Workflow(self.db).move(task.id, Status.READY)

    def finish(self, launch: Run) -> Run:
        """Wait for a launched run (must be called inside the loop)."""
        return asyncio.get_event_loop().run_until_complete(self.service.wait(launch.id))

    def kinds(self, task_id: int) -> list[EventKind]:
        """Return the event kinds of a task."""
        return [e.kind for e in self.tasks.events.for_task(task_id)]


@pytest.fixture
def harness(db: Database, home: JotHome, config: Config) -> Harness:
    """Runner harness on the fake backend."""
    return Harness(db, home, config)


class TestFlows:
    """End-to-end status transitions."""

    def test_planned_then_approve(self, harness: Harness, home: JotHome) -> None:
        """Plan -> awaiting_approval -> approve -> review, with events and logs."""
        task = harness.ready()

        async def scenario() -> tuple[Run, Run]:
            plan = await harness.service.start(task.id)
            plan = await harness.service.wait(plan.id)
            assert harness.tasks.get(task.id).status is Status.AWAITING_APPROVAL
            execute = await harness.service.approve(task.id, "ship it")
            return plan, await harness.service.wait(execute.id)

        plan, execute = asyncio.run(scenario())
        assert plan.status == "succeeded"
        assert execute.status == "succeeded"
        final = harness.tasks.get(task.id)
        assert final.status is Status.REVIEW
        assert final.claimed_by is None
        kinds = harness.kinds(task.id)
        assert {EventKind.PLAN, EventKind.APPROVAL, EventKind.RESULT} <= set(kinds)
        assert (home.path / "workspaces" / str(task.id)).is_dir()
        assert harness.service.log(execute.id)[-1]["kind"] == "result"
        assert harness.service.log(999) == []

    def test_direct(self, harness: Harness) -> None:
        """Direct flow executes immediately without approval."""
        task = harness.ready()

        async def scenario() -> Run:
            run = await harness.service.start(task.id, flow=Flow.DIRECT)
            return await harness.service.wait(run.id)

        run = asyncio.run(scenario())
        assert run.phase == "execute"
        assert harness.tasks.get(task.id).status is Status.REVIEW
        assert EventKind.APPROVAL not in harness.kinds(task.id)

    def test_guards(self, harness: Harness) -> None:
        """Only ready tasks start; approval needs awaiting_approval; no doubles."""
        task = harness.ready()
        inbox = harness.tasks.create(Task(title="x", raw_input="x"))

        async def scenario() -> None:
            with pytest.raises(WorkflowError):
                await harness.service.start(inbox.id)
            with pytest.raises(WorkflowError):
                await harness.service.approve(task.id)
            run = await harness.service.start(task.id)
            with pytest.raises(WorkflowError):
                await harness.service.start(task.id)
            await harness.service.wait(run.id)
            with pytest.raises(WorkflowError):
                await harness.service.cancel(run.id)

        asyncio.run(scenario())

    def test_send_back(self, harness: Harness) -> None:
        """Awaiting approval returns to ready with the feedback recorded."""
        task = harness.ready()

        async def scenario() -> Task:
            run = await harness.service.start(task.id)
            await harness.service.wait(run.id)
            return await harness.service.send_back(task.id, "smaller scope")

        assert asyncio.run(scenario()).status is Status.READY
        assert harness.kinds(task.id).count(EventKind.COMMENT) >= 1


class TestFailures:
    """Failed, erroring, and cancelled runs release the task."""

    def test_error_result(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An error result fails the run and restores the prior status."""

        async def broken(
            self: FakeBackend, request: RunRequest
        ) -> AsyncIterator[AgentEvent]:
            del self, request
            yield AgentEvent(AgentEventKind.RESULT, "quota", is_error=True)

        monkeypatch.setattr(FakeBackend, "run", broken)
        task = harness.ready()

        async def scenario() -> Run:
            run = await harness.service.start(task.id)
            return await harness.service.wait(run.id)

        run = asyncio.run(scenario())
        assert run.status == "failed"
        assert harness.tasks.get(task.id).status is Status.READY

    def test_app_error(self, harness: Harness, tmp_path: Path) -> None:
        """A missing repo path fails the run cleanly."""
        task = harness.ready(repo_path=str(tmp_path / "missing"))

        async def scenario() -> Run:
            run = await harness.service.start(task.id)
            return await harness.service.wait(run.id)

        run = asyncio.run(scenario())
        assert run.status == "failed"
        assert "does not exist" in (run.summary or "")

    def test_cancel(self, harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cancelling marks the run cancelled and releases the lease."""

        async def slow(
            self: FakeBackend, request: RunRequest
        ) -> AsyncIterator[AgentEvent]:
            del self, request
            await asyncio.sleep(30)
            yield AgentEvent(AgentEventKind.RESULT, "late")

        monkeypatch.setattr(FakeBackend, "run", slow)
        task = harness.ready()

        async def scenario() -> Run:
            run = await harness.service.start(task.id)
            await asyncio.sleep(0.05)
            assert harness.service.active() == [run.id]
            await harness.service.shutdown()
            return RunRepository(harness.db).get(run.id)

        run = asyncio.run(scenario())
        assert run.status == "cancelled"
        assert harness.tasks.get(task.id).claimed_by is None


class TestWorkspaces:
    """Repository resolution and git worktree isolation."""

    def test_worktree(self, harness: Harness, db: Database, git_repo: Path) -> None:
        """Execution in a git repo uses a jot/<id>-<slug> worktree; plans don't."""
        project = ProjectRepository(db).create(
            Project(slug="demo", name="Demo", repo_path=str(git_repo))
        )
        task = harness.ready("Fix the Widget!", project_id=project.id)

        async def scenario() -> Run:
            plan = await harness.service.start(task.id)
            await harness.service.wait(plan.id)
            run = await harness.service.approve(task.id)
            return await harness.service.wait(run.id)

        run = asyncio.run(scenario())
        assert run.branch == f"jot/{task.id}-fix-the-widget"
        assert run.worktree is not None
        assert Path(run.worktree).is_dir()

    def test_git_helpers(self, git_repo: Path, tmp_path: Path) -> None:
        """Worktrees are reused; non-repos are detected; diffstat is safe."""
        git = GitWorkspaces()

        async def scenario() -> None:
            assert await git.is_repo(git_repo)
            assert not await git.is_repo(tmp_path)
            first = await git.worktree(git_repo, 1, "")
            again = await git.worktree(git_repo, 1, "")
            assert first == again
            assert first.branch == "jot/1-task"
            (first.cwd / "new.txt").write_text("x", encoding="utf-8")
            assert "new.txt" in await git.diffstat(first)
            assert await git.diffstat(Workspace(cwd=tmp_path)) == ""
            with pytest.raises(WorkspaceError):
                await git.toplevel(tmp_path)

        asyncio.run(scenario())

    def test_in_place(self, harness: Harness, tmp_path: Path) -> None:
        """A non-git repo path runs in place."""
        plain = tmp_path / "plain"
        plain.mkdir()
        task = harness.ready(repo_path=str(plain))

        async def scenario() -> Run:
            run = await harness.service.start(task.id, flow=Flow.DIRECT)
            return await harness.service.wait(run.id)

        assert asyncio.run(scenario()).worktree is None


class TestPlanOutcome:
    """Plan parsing fallbacks."""

    def test_parse(self) -> None:
        """JSON plans parse; prose becomes the plan text."""
        parsed = PlanOutcome.parse('{"summary": "s", "plan": "p", "questions": ["q"]}')
        assert parsed == PlanOutcome("s", "p", ("q",))
        assert PlanOutcome.parse("just text").plan == "just text"
        assert PlanOutcome.parse('{"questions": "no"}').questions == ()

    def test_app_error_type(self) -> None:
        """Workspace errors are application errors."""
        assert issubclass(WorkspaceError, AppError)
