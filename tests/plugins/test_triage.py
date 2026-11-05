"""The triage plugin: queue on capture, enrich through the harness, never clobber."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from pydantic import JsonValue
from tests.cli.test_cli import data, jot
from tests.plugins.conftest import USER, FakeClock

from jot.app.commands import JotPatch
from jot.core.profiles import FieldSpec, FieldType
from jot.exceptions import PluginError
from jot.plugins.api import JobStatus
from jot.plugins.harness import FakePlugin, HarnessPlugin
from jot.plugins.host import PluginCatalog, PluginHost
from jot.plugins.triage import (
    FAILED,
    NEEDS_INPUT,
    TRIAGED,
    TRIAGING,
    Proposal,
    TriagePlugin,
    Triager,
    TriageSettings,
    slugify,
)

if TYPE_CHECKING:
    from jot.app.commands import Commands
    from jot.app.session import Session
    from jot.core.models import Metadata

GOOD: Metadata = {
    "title": "Fix the login bug",
    "body": "Users cannot log in.",
    "project": "web",
    "labels": ["Bug", "triaged", "bug", "!!"],
    "confidence": 0.9,
}


@pytest.fixture
def host(session: Session, clock: FakeClock) -> PluginHost:
    """Harness, fake backend, and triage, all enabled."""
    host = PluginHost(
        session,
        PluginCatalog.of(HarnessPlugin, FakePlugin, TriagePlugin),
        clock=clock,
    )
    for plugin_id in ("harness", "harness-fake", "triage"):
        host.enable(plugin_id, USER)
    return host


@pytest.fixture
def commands(session: Session) -> Commands:
    """User commands, with a project the model can pick."""
    commands = session.commands(USER)
    commands.create_project("web", "Website")
    return commands


def answer(host: PluginHost, canned: Metadata, **settings: JsonValue) -> None:
    """Script the fake's answer and triage settings."""
    host.configure("harness-fake", {"canned": canned}, USER)
    if settings:
        host.configure("triage", settings, USER)


def triage(host: PluginHost) -> None:
    """Deliver events and run jobs."""
    host.deliver()
    host.run_jobs()


class TestTriage:
    """End to end through the host with the fake harness."""

    def test_enriches_new_jots(
        self, host: PluginHost, commands: Commands, session: Session
    ) -> None:
        """Title, body, project, and labels are applied; markers swap."""
        answer(host, GOOD)
        commands.create(title="login broken")
        host.deliver()
        assert session.queries.get(1).labels == [TRIAGING]
        host.run_jobs()
        done = session.queries.get(1)
        assert (done.title, done.body, done.project) == (
            "Fix the login bug",
            "Users cannot log in.",
            "web",
        )
        assert done.labels == ["bug", TRIAGED]
        assert done.metadata["triage"] == {
            "confidence": 0.9,
            "questions": [],
            "original_title": "login broken",
            "model": "fake-model",
        }
        last = session.queries.history(1)[-1]
        assert (last.actor, last.reason) == ("plugin:triage", "triage")
        assert host.jobs()[0].status == JobStatus.DONE

    def test_user_edits_win(
        self, host: PluginHost, commands: Commands, session: Session
    ) -> None:
        """Fields the user changed after capture are never overwritten."""
        answer(host, GOOD)
        commands.create_project("ops", "Ops")
        commands.create(title="login broken")
        host.deliver()
        commands.update(1, JotPatch(title="My title", project="ops"))
        host.run_jobs()
        done = session.queries.get(1)
        assert (done.title, done.body, done.project) == (
            "My title",
            "Users cannot log in.",
            "ops",
        )
        assert "original_title" not in done.metadata["triage"]  # ty: ignore[unsupported-operator]

    def test_existing_values_are_kept(
        self, host: PluginHost, commands: Commands, session: Session
    ) -> None:
        """Bodies and projects set at capture stay; titles can be left alone."""
        answer(host, GOOD, rewrite_title=False)
        commands.create_project("ops", "Ops")
        commands.create(title="t", body="mine", project="ops", labels=["keep"])
        triage(host)
        done = session.queries.get(1)
        assert (done.title, done.body, done.project) == ("t", "mine", "ops")
        assert done.labels == ["bug", "keep", TRIAGED]

    def test_unsure_answers_ask_for_input(
        self, host: PluginHost, commands: Commands, session: Session
    ) -> None:
        """Low confidence or questions add needs-input and question comments."""
        answer(host, {"confidence": 0.2, "questions": ["Which page?", "  "]})
        commands.create(title="it is slow")
        triage(host)
        done = session.queries.get(1)
        assert done.labels == [NEEDS_INPUT, TRIAGED]
        assert done.title == "it is slow"
        comments = session.queries.comments(1)
        assert [(c.kind, c.body) for c in comments] == [
            ("triage.question", "Which page?")
        ]

    def test_new_projects_only_when_allowed(
        self, host: PluginHost, commands: Commands, session: Session
    ) -> None:
        """A proposed new project is created only with create_projects."""
        canned: Metadata = {"project": "nope", "new_project": "Home Lab"}
        answer(host, canned)
        commands.create(title="a")
        triage(host)
        assert session.queries.get(1).project is None
        answer(host, canned, create_projects=True)
        commands.create(title="b")
        triage(host)
        assert session.queries.get(2).project == "home-lab"
        commands.create(title="c")
        triage(host)
        assert session.queries.get(3).project == "home-lab"
        answer(host, {"new_project": "!!!"}, create_projects=True)
        commands.create(title="d")
        triage(host)
        assert session.queries.get(4).project is None

    def test_profile_fields(
        self, host: PluginHost, commands: Commands, session: Session
    ) -> None:
        """Only declared, unset profile fields are filled."""
        task = session.profiles.get("task")
        fields = {
            "size": FieldSpec(type=FieldType.ENUM, options=("S", "L")),
            "due": FieldSpec(type=FieldType.DATE),
            "owner": FieldSpec(type=FieldType.STRING, label="Owner"),
        }
        session.profiles.register(task.model_copy(update={"fields": fields}))
        answer(host, {"fields": {"size": "L", "due": "2026-12-01", "x": 1}})
        commands.create(title="a", profile="task", fields={"owner": "me"})
        jot_ = session.queries.get(1)
        schema = Triager(host.context_for("triage"), TriageSettings()).schema(jot_)
        properties = schema["properties"]
        assert isinstance(properties, dict)
        assert properties["fields"] == {
            "type": "object",
            "properties": {
                "size": {"description": "size", "enum": ["S", "L"]},
                "due": {"description": "due as YYYY-MM-DD", "type": "string"},
            },
        }
        triage(host)
        assert session.queries.get(1).metadata["fields"] == {
            "owner": "me",
            "size": "L",
            "due": "2026-12-01",
        }

    def test_profile_filter(
        self, host: PluginHost, commands: Commands, session: Session
    ) -> None:
        """Only listed profiles are triaged."""
        host.configure("triage", {"profiles": ["task"]}, USER)
        commands.create(title="a note", profile="note")
        commands.create(title="a task", profile="task")
        triage(host)
        assert session.queries.get(1).labels == []
        assert TRIAGED in session.queries.get(2).labels

    def test_skips_jots_no_longer_waiting(
        self, host: PluginHost, commands: Commands, session: Session
    ) -> None:
        """Deleted jots and jots whose marker was removed are skipped."""
        commands.create(title="a")
        commands.create(title="b")
        host.deliver()
        commands.delete(1)
        commands.label(2, remove=[TRIAGING])
        host.run_jobs()
        results = [job.result for job in host.jobs()]
        assert results == [
            {"skipped": "no longer waiting for triage"},
            {"skipped": "no longer waiting for triage"},
        ]
        assert session.queries.get(2).labels == []

    def test_capture_works_with_the_model_down(
        self,
        host: PluginHost,
        commands: Commands,
        session: Session,
        clock: FakeClock,
    ) -> None:
        """Capture succeeds offline; triage retries, then marks the jot failed."""
        host.configure("harness-fake", {"fail": True}, USER)
        assert commands.create(title="offline idea").id == 1
        triage(host)
        assert session.queries.get(1).labels == [TRIAGING]
        for _ in range(3):
            clock.advance(1_000)
            host.run_jobs()
        done = session.queries.get(1)
        assert done.labels == [FAILED]
        assert done.title == "offline idea"
        note = session.queries.comments(1)[0]
        assert note.kind == "triage.note"
        assert "jot triage run 1" in note.body
        job = host.jobs()[0]
        assert (job.status, job.attempts) == (JobStatus.DONE, 3)

    def test_requires_the_harness(self, session: Session) -> None:
        """Triage cannot be enabled without the harness router."""
        host = PluginHost(session, PluginCatalog.of(HarnessPlugin, TriagePlugin))
        with pytest.raises(PluginError, match="enable harness"):
            host.enable("triage", USER)

    def test_job_without_jot(self, host: PluginHost) -> None:
        """A stray job without a jot is skipped."""
        host.context_for("triage").enqueue("triage")
        host.run_jobs()
        assert host.jobs()[0].result == {"skipped": "no jot"}


class TestPrompt:
    """What the model is told."""

    def test_prompt_lists_context(
        self, host: PluginHost, commands: Commands, session: Session
    ) -> None:
        """Projects, popular labels first, owner-set values, and the note."""
        commands.create(title="x", labels=["rare"])
        commands.create(title="y", labels=["common", TRIAGED])
        commands.create(title="z", labels=["common"])
        commands.create(title="Note", body="More", project="web", labels=["mine"])
        triager = Triager(
            host.context_for("triage"), TriageSettings(guidance="Be kind")
        )
        prompt = triager.prompt(session.queries.get(4))
        assert "## Guidance\n\nBe kind" in prompt
        assert "- web: Website" in prompt
        assert "common, mine, rare" in prompt
        assert "project: web\nlabels: mine" in prompt
        assert prompt.endswith("## Note\n\nNote\n\nMore")


class TestProposal:
    """Lenient parsing of model output."""

    def test_garbage_is_dropped(self) -> None:
        """Wrong types fall back to safe defaults."""
        proposal = Proposal.parse(
            {
                "title": 5,
                "labels": "bug",
                "questions": "why",
                "confidence": "high",
                "fields": [],
            },
            5,
        )
        assert proposal == Proposal()

    def test_values_are_normalized(self) -> None:
        """Whitespace, case, range, and the label limit are enforced."""
        proposal = Proposal.parse(
            {
                "title": "  Two\n words ",
                "project": " Web ",
                "labels": ["A b", "c", "d", "e"],
                "confidence": 7,
            },
            2,
        )
        assert (proposal.title, proposal.project, proposal.labels) == (
            "Two words",
            "web",
            ["a-b", "c"],
        )
        assert proposal.confidence == 1.0

    @pytest.mark.parametrize(
        ("text", "expected"), [("Bug Fix", "bug-fix"), ("--", None), ("9x", None)]
    )
    def test_slugify(self, text: str, expected: str | None) -> None:
        """Labels must be valid names."""
        from jot.core.models import NAME  # noqa: PLC0415 - local to this case

        assert slugify(text, NAME.pattern) == expected


@pytest.mark.usefixtures("home")
class TestTriageCommands:
    """``jot triage run``."""

    def test_run(self) -> None:
        """Queue or triage now through the CLI."""
        off = data("triage", "run", "1", code=1)
        assert "Triage is off" in off["error"]  # ty: ignore[not-subscriptable]
        for plugin_id in ("harness", "harness-fake", "triage"):
            jot("plugin", "enable", plugin_id)
        canned = json.dumps({"canned": {"title": "Tidy", "confidence": 1}})
        jot("plugin", "config", "harness-fake", "--set", canned)
        jot("add", "messy")
        assert jot("triage", "run", "1") == "1 queued: messy\n"
        assert jot("triage", "run", "1", "--now") == "1 triaged: Tidy\n"
        shown = data("show", "1")
        assert shown["jot"]["labels"] == [TRIAGED]  # ty: ignore[not-subscriptable]
        jobs = data("job", "ls")
        assert isinstance(jobs, list)
        assert {job["status"] for job in jobs} == {"cancelled"}

    def test_errors(self) -> None:
        """Excluded profiles and failed runs are reported."""
        for plugin_id in ("harness", "harness-fake", "triage"):
            jot("plugin", "enable", plugin_id)
        jot("plugin", "config", "triage", "--set", json.dumps({"profiles": ["task"]}))
        jot("add", "note one")
        excluded = data("triage", "run", "1", code=1)
        assert "excluded" in excluded["error"]  # ty: ignore[not-subscriptable]
        jot("plugin", "config", "triage", "--set", "{}")
        jot("plugin", "config", "harness-fake", "--set", json.dumps({"fail": True}))
        jot("triage", "run", "1", "--now", code=1)
        assert data("show", "1")["jot"]["labels"] == [FAILED]  # ty: ignore[not-subscriptable]
