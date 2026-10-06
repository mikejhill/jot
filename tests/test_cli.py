"""CLI capture, filtering, editing, drawdown, snapshots, and error smoke tests."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from jot.__main__ import Application
from jot.cli import CommandLine
from jot.config import JotHome


class TestCommandLine:
    """Every implemented command works with an isolated JOT_HOME."""

    def test_capture(self, home: JotHome) -> None:
        """The exact acceptance capture returns an inbox record via JSON listing."""
        runner = CliRunner()
        app = CommandLine().app
        note = "orbit api - health check is simple ping; need to make holistic"
        result = runner.invoke(app, ["add", note])
        assert result.exit_code == 0, result.output
        assert result.stdout == "1\n"
        result = runner.invoke(app, ["ls", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload[0]["raw_input"] == note
        assert payload[0]["status"] == "inbox"
        assert payload[0]["needs_enrichment"] is True
        assert home.database.is_file()

    def test_crud_commands(self, home: JotHome) -> None:
        """Content and project commands preserve typed fields and audit history."""
        runner = CliRunner()
        app = CommandLine().app
        commands = [
            [
                "projects",
                "add",
                "orbit",
                "Orbit API",
                "--repo",
                str(home.path),
                "--alias",
                "orbit-api",
                "--priority",
                "2",
                "--default-flow",
                "direct",
                "--json",
            ],
            ["projects", "ls"],
            [
                "projects",
                "edit",
                "orbit",
                "--name",
                "Orbit",
                "--priority",
                "3",
                "--description",
                "monitoring",
                "--alias",
                "rai",
                "--active",
                "--json",
            ],
            [
                "add",
                "ping",
                "--project",
                "orbit",
                "--label",
                "reliability",
                "--crit",
                "high",
                "--type",
                "bug",
                "--title",
                "Health",
                "--repo",
                str(home.path),
                "--json",
            ],
            [
                "edit",
                "1",
                "--title",
                "Health check",
                "--description",
                "Holistic health",
                "--project",
                "orbit",
                "--crit",
                "critical",
                "--type",
                "feature",
                "--flow",
                "direct",
                "--due-at",
                "2030-01-01T00:00:00Z",
                "--json",
            ],
            ["label", "add", "1", "observability", "--json"],
            [
                "ls",
                "--project",
                "orbit",
                "--label",
                "observability",
                "--status",
                "inbox",
                "--crit",
                "critical",
                "--q",
                "observability",
                "--older-than",
                "0d",
                "--order",
                "-id",
                "--limit",
                "1",
            ],
            ["label", "rm", "1", "observability", "--json"],
            ["comment", "1", "needs evidence", "--actor", "reviewer", "--json"],
            ["show", "1", "--json"],
            ["move", "1", "ready", "--json"],
            ["next", "-n", "3", "--project", "orbit", "--json"],
            ["claim", "--next", "--agent", "agent", "--json"],
            ["release", "1", "--agent", "agent", "--json"],
            ["claim", "1", "--agent", "agent", "--direct", "--json"],
            ["release", "1", "--agent", "agent", "--status", "review", "--json"],
            ["reap", "--json"],
            ["export", "--format", "jsonl"],
            ["export", "--format", "md", "--json"],
            ["backup", "--json"],
        ]
        for command in commands:
            result = runner.invoke(app, command)
            assert result.exit_code == 0, (command, result.output, result.exception)
        assert list((home.path / "backups").glob("*.db"))

    def test_human_output_and_empty_queue(self, home: JotHome) -> None:
        """Human output works across commands, including empty queue responses."""
        app = CommandLine().app
        runner = CliRunner()
        for command in (
            ["ls"],
            ["projects", "ls", "--json"],
            ["projects", "--json", "ls"],
            ["next"],
            ["claim", "--next", "--agent", "a"],
            ["reap"],
            ["export", "--format", "md"],
            ["backup"],
            ["add", "human"],
            ["show", "1"],
            ["edit", "1", "--repo", str(home.path)],
            ["label", "add", "1", "tag"],
            ["label", "rm", "1", "tag"],
            ["comment", "1", "hello"],
            ["move", "1", "ready"],
            ["claim", "1", "--agent", "a"],
            ["claim", "1", "--agent", "b", "--json"],
            ["release", "1", "--agent", "a"],
            ["projects", "add", "p", "Project"],
            ["projects", "edit", "p", "--repo", str(home.path)],
        ):
            result = runner.invoke(app, command)
            assert result.exit_code == 0, (command, result.output, result.exception)

    @pytest.mark.parametrize(
        "command",
        [
            ["show", "999"],
            ["move", "1", "done"],
            ["claim", "--agent", "a"],
            ["claim", "1", "--next", "--agent", "a"],
            ["ls", "--older-than", "invalid"],
            ["ls", "--older-than", "-1"],
            ["export", "--format", "csv"],
            ["add", "note", "--project", "missing"],
            ["ls", "--order", "wrong"],
        ],
    )
    def test_errors(self, home: JotHome, command: list[str]) -> None:
        """Expected failures exit one without tracebacks."""
        runner = CliRunner()
        app = CommandLine().app
        assert runner.invoke(app, ["add", str(home.path)]).exit_code == 0
        result = runner.invoke(app, ["--json", *command])
        assert result.exit_code == 1
        assert "error" in json.loads(result.stdout)

    def test_module_entry(self, home: JotHome) -> None:
        """The class entry point is usable with explicit arguments."""
        with pytest.raises(SystemExit) as caught:
            Application.main(["add", str(home.path)])
        assert caught.value.code == 0
