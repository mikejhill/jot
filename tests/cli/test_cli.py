"""The CLI as an automation path: JSON output, exit codes, write context."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jot.__main__ import Application
from jot.cli.main import CommandLine
from jot.config import JotHome

runner = CliRunner()


def jot(*args: str, stdin: str | None = None, code: int = 0) -> str:
    """Run one CLI command in a fresh application and return stdout."""
    result = runner.invoke(CommandLine().app, list(args), input=stdin)
    assert result.exit_code == code, result.output
    return result.stdout


def data(*args: str, code: int = 0) -> object:
    """Run a command with --json and parse the output."""
    return json.loads(jot(*args, "--json", code=code))


@pytest.mark.usefixtures("home")
class TestAcceptance:
    """Capture and manage jots with no plugins, models, or credentials."""

    def test_capture_organize_query(self) -> None:
        """The full information workflow works from the CLI alone."""
        assert jot("project", "add", "sales", "Sales", "-d", "Reports") == (
            "sales                Sales - Reports\n"
        )
        assert jot("add", "Optimize", "sales", "model", "-l", "sales") == "1\n"
        assert jot("add", "-p", "task", "-P", "sales", "-t", "Ship", "Details") == "2\n"
        assert jot("add", "--stdin", stdin="Long note\nwith lines\n") == "3\n"
        listing = jot("ls")
        assert "1  note:open       Optimize sales model #sales" in listing
        assert "task:todo       Ship @sales" in listing
        assert "Long note" in listing
        assert "doing" in jot("status", "2", "doing", "--reason", "started")
        assert jot("comment", "2", "first", "pass") == "1\n"
        assert "first pass!" in jot("comment-edit", "1", "first", "pass!")
        assert "#ok" in jot("label", "2", "-a", "ok")
        jot("rank", "2", "--top")
        jot("link", "2", "1", "--rel", "relates")
        shown = jot("show", "2", "--with", "comments,links,history")
        for text in (
            "#2 Ship",
            "status: doing (active)",
            "project: sales",
            "Details",
            "2 relates 1",
            "first pass!",
            "jot.status_changed v2 by user via cli (started): status todo -> doing",
        ):
            assert text in shown
        assert jot("ls", "-n", "1").startswith("    2")
        assert "Optimize" in jot("ls", "-q", "optim")
        assert "Deleted 3" in jot("rm", "3")
        assert "Long note" not in jot("ls")
        assert "Long note" in jot("restore", "3")
        assert "(archived)" in jot("archive", "3")
        assert "Long note" in jot("ls", "--archived")
        assert "(archived)" not in jot("unarchive", "3")
        assert jot("unlink", "2", "1") == "Unlinked\n"
        assert "Deleted comment 1" in jot("comment-rm", "1")
        assert "jot.created" in jot("history", "1")
        assert "    1  ok\n" in jot("labels")
        assert "project.created" in jot("events")
        assert "Sales report" in jot(
            "project", "edit", "sales", "--name", "Sales report"
        )
        assert "Sales report" in jot("project", "ls")
        jot("project", "edit", "sales", "--archive")
        assert jot("project", "ls") == "No projects\n"
        assert "(archived)" in jot("project", "ls", "--archived")

    def test_json_contract(self) -> None:
        """Agents get structured output and version-checked writes."""
        created = data("add", "Plan", "--actor", "agent:claude-code", "-f", "x=1")
        assert isinstance(created, dict)
        assert created["created_by"] == "agent:claude-code"
        assert created["metadata"] == {"fields": {"x": "1"}}
        edited = data(
            "edit",
            "1",
            "-t",
            "Plan v2",
            "-m",
            'triage={"done": true}',
            "-m",
            "note=plain",
            "--if-version",
            "1",
        )
        assert isinstance(edited, dict)
        assert edited["version"] == 2
        assert edited["metadata"]["triage"] == {"done": True}
        assert edited["metadata"]["note"] == "plain"
        conflict = data("edit", "1", "-t", "stale", "--if-version", "1", code=3)
        assert conflict == {"error": "Jot 1 is at version 2, not 1", "code": 3}
        merged = data("edit", "1", "-b", "body", "--base-version", "1")
        assert isinstance(merged, dict)
        assert merged["body"] == "body"
        detail = data("show", "1", "--with", "history")
        assert isinstance(detail, dict)
        assert set(detail) == {"jot", "history"}
        assert detail["history"][0]["actor"] == "agent:claude-code"
        listing = data("ls", "-c", "open")
        assert isinstance(listing, list)
        assert [j["id"] for j in listing] == [1]
        assert data("labels") == []
        assert isinstance(data("events", "--after", "1"), list)
        assert isinstance(data("profile", "ls"), list)

    def test_edit_options(self) -> None:
        """Body from stdin, project clearing, profile switch, field removal."""
        jot("project", "add", "p", "P")
        jot("add", "-P", "p", "-f", "x=1", "Thing")
        result = runner.invoke(
            CommandLine().app,
            ["edit", "1", "--stdin", "-P", "", "-p", "task", "-f", "x="],
            input="new body",
        )
        assert result.exit_code == 0, result.output
        shown = jot("show", "1")
        assert "new body" in shown
        assert "project:" not in shown
        assert "task" in shown
        assert "metadata" not in shown

    def test_profiles(self) -> None:
        """Profiles are listed and shown."""
        assert "task: Task" in jot("profile", "ls")
        assert "todo* (open)" in jot("profile", "show", "task")


@pytest.mark.usefixtures("home")
class TestErrors:
    """Expected failures exit with distinct codes."""

    @pytest.mark.parametrize(
        ("args", "code", "message"),
        [
            (("show", "9"), 2, "Jot 9 not found"),
            (("add", "x", "-p", "epic"), 1, "Unknown profile"),
            (("add", "x", "-f", "novalue"), 1, "Expected key=value"),
            (("show", "1", "--with", "bogus"), 1, "Unknown section"),
            (("rank", "1"), 1, "exactly one of"),
            (("install-skills", "--target", "vim"), 1, "Unknown target"),
        ],
    )
    def test_codes(self, args: tuple[str, ...], code: int, message: str) -> None:
        """The message goes to stderr and JSON mode echoes it on stdout."""
        jot("add", "seed")
        result = runner.invoke(CommandLine().app, ["--json", *args])
        assert result.exit_code == code
        assert message in result.stderr
        assert json.loads(result.stdout)["code"] == code

    def test_bad_config(self, home: JotHome) -> None:
        """A broken config file is reported, not raised."""
        home.path.mkdir(parents=True, exist_ok=True)
        home.config_file.write_text("[", encoding="utf-8")
        result = runner.invoke(CommandLine().app, ["ls"])
        assert result.exit_code == 1
        assert "Cannot load Jot home" in result.stderr

    def test_invalid_model_input(self) -> None:
        """Pydantic validation errors exit 1."""
        result = runner.invoke(CommandLine().app, ["project", "add", "Bad Slug", "x"])
        assert result.exit_code == 1


class TestSkills:
    """The CLI skill installs into agent folders."""

    @pytest.mark.usefixtures("home")
    def test_install(self, tmp_path: Path) -> None:
        """The skill lands in the destination folder."""
        out = jot("install-skills", "--dest", str(tmp_path / "skills"))
        assert (tmp_path / "skills" / "jot" / "SKILL.md").is_file()
        assert "Installed" in out

    def test_default_targets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Named targets install under the home directory."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        jot("install-skills", "--target", "codex", "--target", "copilot")
        assert (tmp_path / ".codex" / "skills" / "jot" / "SKILL.md").is_file()
        assert (tmp_path / ".copilot" / "skills" / "jot" / "SKILL.md").is_file()


@pytest.mark.usefixtures("home")
def test_main_entry(capsys: pytest.CaptureFixture[str]) -> None:
    """The console entry point runs commands."""
    with pytest.raises(SystemExit) as exit_info:
        Application.main(["add", "hello"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == "1\n"
