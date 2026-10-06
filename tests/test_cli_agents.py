"""Agent CLI commands end to end on the fake backend."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jot.agents.base import Json, JsonObject
from jot.agents.fake import FakeBackend
from jot.cli import CommandLine
from jot.config import JotHome

FAKE_CONFIG = """[triage]
backend = "fake"
[drawdown]
backend = "fake"
"""

TRIAGE: JsonObject = {
    "title": "Enriched title",
    "description": "d",
    "project_slug": "",
    "new_project_name": "",
    "labels": ["ops"],
    "criticality": "high",
    "type": "bug",
    "confidence": 0.9,
    "questions": [],
}


class Cli:
    """Invoke the CLI and decode JSON output."""

    def __init__(self) -> None:
        self.runner = CliRunner()

    def run(self, *args: str, code: int = 0) -> str:
        """Run a command and assert its exit code."""
        result = self.runner.invoke(CommandLine().app, list(args))
        assert result.exit_code == code, (args, result.output, result.exception)
        return result.stdout

    def json(self, *args: str) -> JsonObject:
        """Run a command with --json and return its JSON object."""
        value = Json.loads(self.run(*args, "--json"))
        assert isinstance(value, dict), value
        return value

    def rows(self, *args: str) -> list[JsonObject]:
        """Run a command with --json and return its list of objects."""
        value = Json.loads(self.run(*args, "--json"))
        assert isinstance(value, list), value
        return [Json.obj(row) for row in value]


@pytest.fixture
def cli(home: JotHome) -> Cli:
    """CLI bound to a data home configured for the fake backend."""
    (home.path / "config.toml").write_text(FAKE_CONFIG, encoding="utf-8")
    FakeBackend.canned = {**TRIAGE}
    return Cli()


class TestAgentCommands:
    """enrich, add --wait, plan/approve/run, runs/log, send-back."""

    def test_enrich(self, cli: Cli) -> None:
        """Explicit ids and pending enrichment both work."""
        cli.run("add", "first note")
        cli.run("add", "second note")
        [first] = cli.rows("enrich", "1")
        assert first["title"] == "Enriched title"
        assert first["status"] == "ready"
        assert "Enriched title" in cli.run("enrich")
        assert "Nothing to enrich" in cli.run("enrich")

    def test_add_wait(self, cli: Cli) -> None:
        """--wait enriches the capture immediately."""
        [task] = cli.rows("add", "wait for it", "--wait")
        assert task["labels"] == ["ops"]

    def test_planned_flow(self, cli: Cli) -> None:
        """Plan -> approve -> runs/log; send-back returns review to ready."""
        cli.run("add", "note", "--wait")
        plan = cli.json("plan", "1")
        assert plan["phase"] == "plan"
        assert "awaiting_approval" in cli.run("show", "1")
        execute = cli.json("approve", "1", "--note", "go")
        assert execute["phase"] == "execute"
        runs = cli.rows("runs", "1")
        assert len(runs) == 2
        assert "execute" in cli.run("runs")
        assert "[result]" in cli.run("log", str(execute["id"]))
        assert "No log" in cli.run("log", "99")
        assert "ready" in cli.run("send-back", "1", "redo")

    def test_direct_and_next(self, cli: Cli) -> None:
        """Run --direct, run --next, and the guard errors."""
        cli.run("add", "a", "--wait")
        cli.run("add", "b", "--wait")
        direct = cli.json("run", "1", "--direct")
        assert direct["phase"] == "execute"
        nxt = cli.json("run", "--next")
        assert nxt["task_id"] == 2
        cli.run("run", "--next", code=1)
        cli.run("run", code=1)
        assert "No runs" in CliRunner().invoke(CommandLine().app, ["runs", "9"]).stdout


class TestCleanupAndSkills:
    """cleanup scan/show/apply and install-skills."""

    def test_cleanup(self, cli: Cli) -> None:
        """Scan, show latest, apply all, and reject bad input."""
        cli.run("cleanup", "show", code=1)
        cli.run("add", "Improve the health check endpoint")
        cli.run("add", "Improve the health check endpoints")
        proposal = cli.json("cleanup", "scan")
        assert isinstance(proposal, dict)
        assert proposal["items"]
        assert "Apply with" in cli.run("cleanup", "show")
        cli.run("cleanup", "apply", str(proposal["id"]), "--approve", "x", code=1)
        applied = cli.json("cleanup", "apply", str(proposal["id"]), "--approve", "all")
        assert applied == {"affected": [2]}
        second = cli.json("cleanup", "scan")
        assert isinstance(second, dict)
        cli.run("cleanup", "apply", str(second["id"]), "--approve", "0")

    def test_install_skills(self, cli: Cli, tmp_path: Path) -> None:
        """The jot skill and its references are copied; legacy skills removed."""
        dest = tmp_path / "skills"
        (dest / "jot-capture").mkdir(parents=True)
        assert len(cli.rows("install-skills", "--dest", str(dest))) == 1
        assert sorted(p.name for p in dest.iterdir()) == ["jot"]
        skill = dest / "jot"
        assert (skill / "SKILL.md").read_text("utf-8").startswith("---")
        references = sorted(p.name for p in (skill / "references").iterdir())
        assert references == [
            "capture.md",
            "cleanup.md",
            "drawdown.md",
            "query-and-groom.md",
        ]
        cli.run("install-skills", "--target", "nope", code=1)

    def test_install_skills_target(
        self, cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Named targets install under the user's home directory."""
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
        cli.run("install-skills", "--target", "codex")
        assert (tmp_path / ".codex" / "skills" / "jot" / "SKILL.md").is_file()
