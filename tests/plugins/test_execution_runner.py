"""Execution slice 8b: the runner, worktrees, questions, approval, and review."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import JsonValue
from tests.cli.test_cli import data, jot
from tests.plugins.conftest import USER, FakeClock

from jot.exceptions import InvalidInputError
from jot.plugins.api import JobStatus
from jot.plugins.execution import (
    AGENT,
    Claims,
    Control,
    ExecutionPlugin,
    Phase,
)
from jot.plugins.execution.runner import Answer, Question, RunLog, Runner, dumps
from jot.plugins.execution.workspace import Git, Workspace, WorkspaceError
from jot.plugins.harness import FakePlugin, HarnessPlugin
from jot.plugins.harness.api import EventKind, HarnessEvent, Usage
from jot.plugins.host import PluginCatalog, PluginHost

if TYPE_CHECKING:
    from jot.app.commands import Commands
    from jot.app.session import Session
    from jot.core.models import Metadata

PLAN: Metadata = {
    "summary": "Add a greeting.",
    "plan": "1. Write hello.txt\n2. Check it",
    "questions": [],
}
DONE: Metadata = {
    "summary": "Wrote hello.txt.",
    "verification": "cat hello.txt shows hi",
    "outcome": "completed",
    "questions": [],
}
IDENTITY = ("-c", "user.name=Test", "-c", "user.email=test@example.com")


def make_repo(path: Path) -> Path:
    """A git repository with one commit."""
    path.mkdir(parents=True)
    git = Git(path)
    git("init", "-q", "-b", "main")
    (path / "README.md").write_text("# demo\n", encoding="utf-8")
    git("add", "README.md")
    git(*IDENTITY, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repository."""
    return make_repo(tmp_path / "repos" / "demo")


@pytest.fixture
def host(session: Session, clock: FakeClock, repo: Path, tmp_path: Path) -> PluginHost:
    """Harness, fake backend, and execution enabled; project web maps to the repo."""
    host = PluginHost(
        session,
        PluginCatalog.of(HarnessPlugin, FakePlugin, ExecutionPlugin),
        clock=clock,
    )
    for plugin_id in ("harness", "harness-fake", "execution"):
        host.enable(plugin_id, USER)
    host.configure("harness", {"max_permission": "full"}, USER)
    host.configure(
        "execution",
        {"repos": {"web": str(repo)}, "worktree_root": str(tmp_path / "wt")},
        USER,
    )
    return host


@pytest.fixture
def commands(session: Session) -> Commands:
    """User commands with the web project."""
    commands = session.commands(USER)
    commands.create_project("web", "Website")
    return commands


@pytest.fixture
def control(host: PluginHost) -> Control:
    """Owner actions."""
    return Control(host, "user")


@pytest.fixture
def task(commands: Commands) -> int:
    """A task in the web project."""
    return commands.create(title="Say hello", profile="task", project="web").id


def reply(host: PluginHost, value: Metadata | str, **extra: JsonValue) -> None:
    """Script the fake agent's final answer."""
    text = value if isinstance(value, str) else json.dumps(value)
    host.configure("harness-fake", {"reply": text, **extra}, USER)


def kinds(session: Session, jot_id: int) -> list[str]:
    """Comment kinds on a jot, oldest first."""
    return [c.kind for c in session.queries.comments(jot_id)]


def record(session: Session, jot_id: int) -> Metadata:
    """The jot's execution metadata."""
    return Claims.record_of(session.queries.get(jot_id))


class TestPlannedFlow:
    """Plan, approve, execute, review, through queued jobs."""

    def test_end_to_end(
        self,
        host: PluginHost,
        control: Control,
        task: int,
        session: Session,
        repo: Path,
    ) -> None:
        """The full planned flow leaves a reviewed commit on the jot's branch."""
        reply(host, PLAN)
        assert control.start(task).phase == Phase.IDLE
        assert host.run_jobs() == 1
        state = Claims(session).state(task)
        assert (state.phase, state.claim) == (Phase.AWAITING_APPROVAL, None)
        assert session.queries.get(task).labels == ["awaiting-approval"]
        stored = record(session, task)
        plan = stored["plan"]
        assert isinstance(plan, dict)
        assert len(str(plan["revision"])) == 12
        assert stored["runner"] is True
        assert RunLog(Path(str(stored["log"]))).read()[-1].kind == EventKind.RESULT
        body = session.queries.comments(task)[-1].body
        assert "Add a greeting." in body
        assert "Write hello.txt" in body

        reply(host, DONE, files={"README.md": "hi\n", "notes.tmp": "x"})
        assert control.approve(task).phase == Phase.APPROVED
        approval = session.queries.comments(task)[-1]
        assert approval.kind == "execution.approval"
        assert str(plan["revision"]) in approval.body
        assert host.run_jobs() == 1
        jot_ = session.queries.get(task)
        assert (Claims.phase_of(jot_), jot_.labels, jot_.status) == (
            Phase.REVIEW,
            ["review"],
            "doing",
        )
        stored = record(session, task)
        assert stored["branch"] == "jot/1-say-hello"
        assert stored["outcome"] == "completed"
        assert stored["commits"] == 1
        assert stored["session"] == "fake-session"
        worktree = Path(str(stored["worktree"]))
        assert (worktree / "README.md").read_text(encoding="utf-8") == "hi\n"
        assert (repo / "README.md").read_text(encoding="utf-8") == "# demo\n"
        assert Git(repo)("log", "--format=%s", "jot/1-say-hello", "-1") == (
            "jot #1: Say hello"
        )
        result = session.queries.comments(task)[-1]
        assert result.kind == "execution.result"
        for text in ("completed", "cat hello.txt", "jot/1-say-hello", "`notes.tmp`"):
            assert text in result.body
        history = session.queries.history(task)
        assert any(e.actor == AGENT for e in history)

    def test_send_back_from_review_reuses_the_worktree(
        self, host: PluginHost, control: Control, task: int, session: Session
    ) -> None:
        """Rework continues on the same branch and resumes the agent session."""
        reply(host, DONE, files={"README.md": "1\n"})
        control.start(task, direct=True, now=True)
        first = record(session, task)
        reply(host, DONE, files={"README.md": "2\n"})
        state = control.send_back(task, "Also add b", now=True)
        assert state.phase == Phase.REVIEW
        second = record(session, task)
        assert second["worktree"] == first["worktree"]
        assert second["base"] == first["base"]
        assert second["commits"] == 2
        assert "execution.feedback" in kinds(session, task)

    def test_send_back_a_plan_replans(
        self, host: PluginHost, control: Control, task: int, session: Session
    ) -> None:
        """A plan sent back is planned again with the note in its history."""
        reply(host, PLAN)
        control.start(task, now=True)
        control.send_back(task, "Smaller please", now=True)
        assert Claims(session).state(task).phase == Phase.AWAITING_APPROVAL
        assert kinds(session, task).count("execution.plan") == 2
        runner = Runner(host.context_for("execution"), control.settings)
        assert any("Smaller please" in line for line in runner.history(task))

    def test_send_back_needs_a_plan_or_result(
        self, control: Control, task: int
    ) -> None:
        """Only plans and results can be sent back."""
        with pytest.raises(InvalidInputError, match="send back"):
            control.send_back(task, "x")

    def test_approve_without_runner_plan_only_approves(
        self, host: PluginHost, control: Control, task: int, session: Session
    ) -> None:
        """An external agent's plan is approved without queueing the runner."""
        claims = Claims(session)
        claims.claim(task, "agent")
        claims.release(task, "agent", Phase.AWAITING_APPROVAL)
        assert control.approve(task).phase == Phase.APPROVED
        assert host.jobs(plugin_id="execution") == []


class TestQuestions:
    """Questions pause the run; answers resume the same phase."""

    def test_plan_questions_and_answers(
        self, host: PluginHost, control: Control, task: int, session: Session
    ) -> None:
        """Two questions; the run resumes after both are answered."""
        reply(
            host,
            {
                "summary": "",
                "plan": "",
                "questions": [
                    {"text": "Which greeting?", "choices": ["hi", "hello", ""]},
                    "Which file?",
                    "",
                ],
            },
        )
        state = control.start(task, now=True)
        assert state.phase == Phase.NEEDS_INPUT
        questions = [
            c for c in session.queries.comments(task) if c.kind == "execution.question"
        ]
        assert len(questions) == 2
        assert "Suggested answers:\n- hi\n- hello" in questions[0].body
        assert record(session, task)["asked_in"] == "planning"
        reply(host, PLAN)
        state = control.answer(task, questions[0].id, "hi", now=True)
        assert state.phase == Phase.NEEDS_INPUT
        state = control.answer(task, questions[1].id, "hello.txt", now=True)
        assert state.phase == Phase.AWAITING_APPROVAL
        runner = Runner(host.context_for("execution"), control.settings)
        assert '- Owner answered "Which file?": hello.txt' in runner.history(task)

    def test_execute_questions_resume_execution(
        self, host: PluginHost, control: Control, task: int, session: Session
    ) -> None:
        """Questions while executing resume execution, not planning."""
        reply(host, {**DONE, "questions": ["Overwrite it?"]})
        assert control.start(task, direct=True, now=True).phase == Phase.NEEDS_INPUT
        assert record(session, task)["asked_in"] == "executing"
        question = session.queries.comments(task)[-1]
        reply(host, DONE)
        assert control.answer(task, question.id, "yes", now=True).phase == (
            Phase.REVIEW
        )

    def test_answer_validation(
        self, control: Control, task: int, commands: Commands
    ) -> None:
        """Only questions on the jot can be answered."""
        note = commands.comment(task, "plain")
        with pytest.raises(InvalidInputError, match="not a question"):
            control.answer(task, note.id, "x")
        with pytest.raises(InvalidInputError, match="not a question"):
            control.answer(task, 999, "x")

    def test_late_answer_is_recorded_only(
        self, host: PluginHost, control: Control, task: int, session: Session
    ) -> None:
        """An answer after the jot moved on is kept but resumes nothing."""
        reply(host, {**PLAN, "questions": ["Sure?"]})
        control.start(task, now=True)
        question = session.queries.comments(task)[-1]
        Claims(session).reopen(task, "user", Phase.IDLE)
        assert control.answer(task, question.id, "yes").phase == Phase.IDLE
        assert host.jobs(plugin_id="execution") == []

    def test_external_agent_questions_reopen_without_runner(
        self, host: PluginHost, control: Control, task: int, session: Session
    ) -> None:
        """Answers to an external agent's questions reopen the jot for agents."""
        claims = Claims(session)
        claims.claim(task, "agent")
        question = session.commands(USER).comment(
            task, "Which?", kind="execution.question"
        )
        claims.release(
            task,
            "agent",
            Phase.NEEDS_INPUT,
            extra={"questions": [question.id], "asked_in": "planning"},
        )
        assert control.answer(task, question.id, "this").phase == Phase.IDLE
        assert host.jobs(plugin_id="execution") == []


class TestFailures:
    """Retries, giving up, cancelling, and lost claims."""

    def test_harness_failure_retries_then_hands_back(
        self,
        host: PluginHost,
        control: Control,
        task: int,
        session: Session,
        clock: FakeClock,
    ) -> None:
        """Three failed attempts leave the jot needing input with the error."""
        host.configure("harness-fake", {"fail": True}, USER)
        control.start(task)
        for _ in range(3):
            host.run_jobs()
            clock.advance(60)
        job = host.jobs(plugin_id="execution")[0]
        assert job.status == JobStatus.DONE
        assert job.attempts == 3
        assert Claims(session).state(task).phase == Phase.NEEDS_INPUT
        assert record(session, task)["outcome"] == "failed"
        note = session.queries.comments(task)[-1]
        assert note.kind == "execution.note"
        assert "jot work retry 1" in note.body
        reply(host, PLAN)
        assert control.retry(task, now=True).phase == Phase.AWAITING_APPROVAL

    def test_missing_repository(self, control: Control, commands: Commands) -> None:
        """Jots without a mapped repository are refused up front."""
        other = commands.create(title="x", profile="task").id
        with pytest.raises(InvalidInputError, match="No repository for jots without"):
            control.start(other)
        commands.create_project("api", "API")
        api = commands.create(title="y", profile="task", project="api").id
        with pytest.raises(InvalidInputError, match="repos --set api="):
            control.start(api)

    def test_repository_gone_gives_up(
        self,
        host: PluginHost,
        control: Control,
        task: int,
        session: Session,
        tmp_path: Path,
    ) -> None:
        """A repository that disappeared ends the run with a note."""
        host.configure("execution", {"repos": {"web": str(tmp_path / "missing")}}, USER)
        assert control.start(task, now=True).phase == Phase.NEEDS_INPUT
        assert "does not exist" in session.queries.comments(task)[-1].body

    def test_harness_required(self, session: Session, task: int) -> None:
        """Without harness@1 the runner refuses to start."""
        host = PluginHost(session, PluginCatalog.of(ExecutionPlugin))
        host.enable("execution", USER)
        with pytest.raises(InvalidInputError, match="needs harness@1"):
            Control(host, "user").start(task)

    def test_not_ready(self, control: Control, task: int, session: Session) -> None:
        """A jot someone holds cannot be run."""
        Claims(session).claim(task, "agent")
        with pytest.raises(InvalidInputError, match="held by agent"):
            control.start(task)

    def test_cancel(
        self, host: PluginHost, control: Control, task: int, session: Session
    ) -> None:
        """Cancel drops queued jobs and running claims."""
        with pytest.raises(InvalidInputError, match="no run to cancel"):
            control.cancel(task)
        control.start(task)
        assert control.cancel(task).phase == Phase.IDLE
        assert host.jobs(plugin_id="execution")[0].status == JobStatus.CANCELLED
        claims = Claims(session)
        claims.claim(task, AGENT, direct=True)
        state = control.cancel(task)
        assert (state.phase, state.claim) == (Phase.APPROVED, None)

    def test_lost_claim_writes_nothing(
        self, host: PluginHost, control: Control, task: int, session: Session
    ) -> None:
        """A job whose jot another agent took stops without writing."""
        control.start(task)
        Claims(session).claim(task, "agent")
        before = session.queries.get(task).version
        host.run_jobs()
        job = host.jobs(plugin_id="execution")[0]
        assert isinstance(job.result, dict)
        assert "claimed by agent" in str(job.result["stopped"])
        assert session.queries.get(task).version == before

    def test_cannot_proceed_needs_input(
        self, host: PluginHost, control: Control, task: int, session: Session
    ) -> None:
        """An agent that cannot proceed hands back for input."""
        reply(host, {**DONE, "outcome": "cannot_proceed"})
        assert control.start(task, direct=True, now=True).phase == Phase.NEEDS_INPUT
        assert record(session, task)["outcome"] == "cannot_proceed"

    def test_unknown_job(self, host: PluginHost) -> None:
        """Unknown job kinds are skipped."""
        context = host.context_for("execution")
        job = context.enqueue("other")
        assert ExecutionPlugin().run(job, context) == {"skipped": "unknown job other"}


class TestControl:
    """Logs, cleanup, repository mapping, and permissions."""

    def test_log_and_clean(self, host: PluginHost, control: Control, task: int) -> None:
        """The log reads back; clean removes the worktree but keeps the branch."""
        with pytest.raises(InvalidInputError, match="no run log"):
            control.log(task)
        reply(host, DONE, files={"README.md": "x"})
        control.start(task, direct=True, now=True)
        events = control.log(task)
        assert [e.kind for e in events][-1] == EventKind.RESULT
        workspace = control.clean(task)
        assert not workspace.path.exists()
        assert Git(workspace.repo).ok("rev-parse", "--verify", workspace.branch)

    def test_clean_refuses_running(
        self, control: Control, task: int, session: Session
    ) -> None:
        """A running jot's worktree stays."""
        Claims(session).claim(task, AGENT, direct=True)
        with pytest.raises(InvalidInputError, match="cancel it first"):
            control.clean(task)

    def test_repos(
        self, control: Control, commands: Commands, repo: Path, tmp_path: Path
    ) -> None:
        """Set, unset, default, and import repository paths."""
        commands.create_project("api", "API")
        commands.record(
            "project.imported",
            {"project": {"slug": "api", "repo_path": str(repo), "name": "API"}},
        )
        commands.record("project.imported", {"project": {"slug": "old"}})
        settings = control.repos({}, from_import=True)
        assert settings.repos["api"] == str(repo)
        settings = control.repos({"api": None}, default=str(repo))
        assert "api" not in settings.repos
        assert settings.default_repo == str(repo)
        assert control.repos({}, default="").default_repo == ""
        with pytest.raises(InvalidInputError, match="No project nope"):
            control.repos({"nope": str(repo)})
        with pytest.raises(WorkspaceError, match="does not exist"):
            control.repos({"api": str(tmp_path / "missing")})
        assert control.repos({"api": str(repo / ".")}).repos["api"] == str(repo)

    def test_permissions(
        self, host: PluginHost, control: Control, task: int, session: Session
    ) -> None:
        """Execute runs at workspace_write unless the project allows a shell."""
        runner = Runner(host.context_for("execution"), control.settings)
        jot_ = session.queries.get(task)
        assert runner.permission(jot_).value == "workspace_write"
        host.configure(
            "execution",
            control.settings.model_dump(mode="json") | {"allow_shell": ["web"]},
            USER,
        )
        runner = Runner(host.context_for("execution"), control.settings)
        assert runner.permission(jot_).value == "full"

    def test_default_worktree_root(
        self, host: PluginHost, control: Control, task: int, session: Session
    ) -> None:
        """Worktrees default to <JOT_HOME>/worktrees/<repo>/<id>-<slug>."""
        host.configure(
            "execution",
            control.settings.model_dump(mode="json") | {"worktree_root": ""},
            USER,
        )
        runner = Runner(host.context_for("execution"), control.settings)
        workspace = runner.workspace(session.queries.get(task))
        assert (
            workspace.path == session.home.path / "worktrees" / "demo" / "1-say-hello"
        )

    def test_off(self, session: Session) -> None:
        """Control refuses when the plugin is disabled."""
        host = PluginHost(session, PluginCatalog.of(ExecutionPlugin))
        with pytest.raises(InvalidInputError, match="Execution is off"):
            Control(host, "user")


class TestParsing:
    """What the agent returns, and how logs read back."""

    def test_answer_parse(self) -> None:
        """JSON answers are coerced; prose becomes the summary and plan."""
        prose = Answer.parse("Just did it.")
        assert (prose.summary, prose.plan, prose.outcome) == (
            "Just did it.",
            "",
            "completed",
        )
        odd = Answer.parse('{"outcome": "WEIRD", "questions": "no", "summary": 3}')
        assert (odd.outcome, odd.questions, odd.summary) == ("completed", [], "")
        many = Question.parse_all([f"q{i}" for i in range(20)])
        assert len(many) == 10
        assert Question.parse_all("nope") == []
        assert Question(text="Plain?").body() == "Plain?"

    def test_prose_plan(
        self, host: PluginHost, control: Control, task: int, session: Session
    ) -> None:
        """A plan written as prose is kept as the plan."""
        reply(host, "Step one, then step two.")
        assert control.start(task, now=True).phase == Phase.AWAITING_APPROVAL
        assert session.queries.comments(task)[-1].body == "Step one, then step two."

    def test_dumps(self) -> None:
        """Logs render as readable lines."""
        events = [
            HarnessEvent(kind=EventKind.TOOL, text="Read x"),
            HarnessEvent(kind=EventKind.THINKING, text="hmm"),
            HarnessEvent(kind=EventKind.USAGE, usage=Usage(input=1)),
            HarnessEvent(kind=EventKind.USAGE),
            HarnessEvent(kind=EventKind.RESULT, text="bad", is_error=True),
        ]
        assert dumps(events) == (
            '[tool] Read x\n[usage] {"input": 1, "measured": true}\n[result error] bad'
        )
        assert RunLog(Path("/nonexistent/log.jsonl")).read() == []


class TestWorkspace:
    """Git worktrees in temp repositories."""

    def test_lifecycle(self, repo: Path, tmp_path: Path) -> None:
        """Create, reuse, re-attach an existing branch, and remove."""
        workspace = Workspace(repo, tmp_path / "wt", 7, "Fix: the *build*!")
        assert workspace.branch == "jot/7-fix-the-build"
        base = workspace.ensure()
        assert base == Git(repo)("rev-parse", "HEAD")
        assert workspace.ensure() == base
        (workspace.path / "f.txt").write_text("x", encoding="utf-8")
        with pytest.raises(WorkspaceError, match="uncommitted"):
            workspace.remove()
        assert not workspace.commit_pending("untracked only")
        assert workspace.untracked() == ["f.txt"]
        workspace.git("add", "f.txt")
        assert workspace.commit_pending("add f")
        assert not workspace.commit_pending("nothing")
        assert workspace.commits(base) == [f"{workspace.head()[:7]} add f"]
        assert "f.txt" in workspace.diffstat(base)
        workspace.remove()
        with pytest.raises(WorkspaceError, match="No worktree"):
            workspace.remove()
        assert workspace.ensure() == base
        assert (workspace.path / "f.txt").exists()
        (workspace.path / "g.txt").write_text("y", encoding="utf-8")
        workspace.remove(force=True)

    def test_slug_and_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Slugs are bounded; non-repositories are refused."""
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        assert Workspace.slug("!!!") == "work"
        assert len(Workspace.slug("word " * 40)) <= 40
        with pytest.raises(WorkspaceError, match="does not exist"):
            Workspace.check_repo(tmp_path / "missing")
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(WorkspaceError, match="rev-parse"):
            Workspace.check_repo(plain)

    def test_fallback_identity(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Commits work when git has no identity configured."""
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "none"))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        workspace = Workspace(repo, tmp_path / "wt", 1, "x")
        workspace.ensure()
        assert workspace.identity() == [
            "-c",
            "user.name=Jot",
            "-c",
            "user.email=jot@localhost",
        ]
        (workspace.path / "README.md").write_text("changed", encoding="utf-8")
        assert workspace.commit_pending("a")
        Git(workspace.path)("config", "user.name", "Me")
        Git(workspace.path)("config", "user.email", "me@example.com")
        assert workspace.identity() == []

    def test_git_missing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A missing git binary is a clear error."""
        monkeypatch.setattr("shutil.which", lambda _name: None)
        with pytest.raises(WorkspaceError, match="not on PATH"):
            Git(tmp_path)("status")


class TestClaimsAdditions:
    """Owner moves added for the runner."""

    def test_reopen_and_annotate(self, session: Session, task: int) -> None:
        """Reopen only from waiting phases; annotate only while holding."""
        claims = Claims(session)
        with pytest.raises(InvalidInputError, match="idle or approved"):
            claims.reopen(task, "user", Phase.REVIEW)
        with pytest.raises(InvalidInputError, match="not waiting on you"):
            claims.reopen(task, "user", Phase.IDLE)
        claims.claim(task, "a")
        state = claims.annotate(task, "a", {"note": "x"})
        assert state.claim is not None
        assert record(session, task)["note"] == "x"
        claims.release(task, "a", Phase.REVIEW)
        assert record(session, task)["note"] == "x"
        assert claims.reopen(task, "user", Phase.APPROVED).phase == Phase.APPROVED


@pytest.mark.usefixtures("home")
class TestWorkRunnerCommands:
    """``jot work run/approve/answer/log/repos/clean`` from the command line."""

    def test_cli_flow(self, tmp_path: Path) -> None:
        """Map a repo, plan, approve, execute, read the log, clean up."""
        repo = make_repo(tmp_path / "cli-repo")
        for plugin_id in ("harness", "harness-fake", "execution"):
            jot("plugin", "enable", plugin_id)
        jot("project", "add", "web", "Web")
        assert jot("work", "repos") == "No repositories mapped\n"
        bad = data("work", "repos", "--set", "web", code=1)
        assert "project=path" in bad["error"]  # ty: ignore[not-subscriptable]
        assert jot("work", "repos", "--set", f"web={repo}", "--default", str(repo)) == (
            f"web  {repo}\n(default)  {repo}\n"
        )
        jot(
            "plugin",
            "config",
            "harness-fake",
            "--set",
            json.dumps({"reply": json.dumps(PLAN)}),
        )
        jot("add", "-p", "task", "-P", "web", "Say hello")
        assert "awaiting_approval" in jot("work", "run", "1", "--now")
        jot(
            "plugin",
            "config",
            "harness-fake",
            "--set",
            json.dumps({"reply": json.dumps(DONE), "files": {"README.md": "hi"}}),
        )
        assert "review" in jot("work", "approve", "1", "--now")
        assert "[result]" in jot("work", "log", "1")
        assert "Removed" in jot("work", "clean", "1")
        assert "review" in jot("work", "state", "1")
        jot("work", "repos", "--unset", "web")
        out = data("work", "cancel", "1", code=1)
        assert "no run to cancel" in out["error"]  # ty: ignore[not-subscriptable]

    def test_cli_questions_and_retry(self, tmp_path: Path) -> None:
        """Answer, send back, and retry from the command line."""
        repo = make_repo(tmp_path / "cli-repo")
        for plugin_id in ("harness", "harness-fake", "execution"):
            jot("plugin", "enable", plugin_id)
        jot("work", "repos", "--default", str(repo))
        jot(
            "plugin",
            "config",
            "harness-fake",
            "--set",
            json.dumps({"reply": json.dumps({**PLAN, "questions": ["Which?"]})}),
        )
        jot("add", "-p", "task", "Say hello")
        assert "needs_input" in jot("work", "run", "1", "--now")
        jot(
            "plugin",
            "config",
            "harness-fake",
            "--set",
            json.dumps({"reply": json.dumps(PLAN)}),
        )
        comments = data("show", "1", "--with", "comments")
        question = comments["comments"][0]["id"]  # ty: ignore[not-subscriptable]
        assert "awaiting_approval" in jot(
            "work", "answer", "1", str(question), "this", "--now"
        )
        assert "awaiting_approval" in jot("work", "send-back", "1", "shorter", "--now")
        jot("work", "approve", "1", "--no-run")
        assert "approved" in jot("work", "retry", "1")
        assert "approved" in jot("work", "cancel", "1")
        assert "approved" in jot("work", "run", "1", "--direct")
