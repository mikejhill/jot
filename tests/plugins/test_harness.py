"""The ``harness@1`` contract, run against every backend through a fake CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, override

import pytest

from jot.plugins.harness.api import (
    Capabilities,
    EventKind,
    Harness,
    HarnessError,
    HarnessEvent,
    Permission,
    RunRequest,
    StructuredRequest,
    StructuredResult,
    Usage,
    integer,
    number,
    obj,
    text_of,
    workdir,
)
from jot.plugins.harness.backends import (
    ClaudeHarness,
    CliSettings,
    CodexHarness,
    CopilotHarness,
    FakeHarness,
    FakeSettings,
    minimal,
)
from jot.plugins.harness.process import JsonlProcess

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jot.core.models import Metadata

FAKE_CLI = Path(__file__).with_name("fake_cli.py")
SCHEMA: Metadata = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}
CLASSES: dict[str, type[ClaudeHarness | CodexHarness | CopilotHarness]] = {
    "claude": ClaudeHarness,
    "codex": CodexHarness,
    "copilot": CopilotHarness,
}
ALL = [*CLASSES, "fake"]


class Calls:
    """Reads what the fake CLI was called with."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def all(self) -> list[Metadata]:
        """Every recorded call."""
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text("utf-8").splitlines()]

    def last(self) -> Metadata:
        """The most recent call."""
        return self.all()[-1]

    def argv(self) -> list[str]:
        """Arguments of the most recent call."""
        argv = self.last()["argv"]
        assert isinstance(argv, list)
        return [str(item) for item in argv]


@pytest.fixture
def calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Calls:
    """Log fake CLI calls under the test folder; default to the ok scenario."""
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLI_LOG", str(log))
    monkeypatch.delenv("FAKE_CLI_MODE", raising=False)
    return Calls(log)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A working directory for runs."""
    folder = tmp_path / "work"
    folder.mkdir()
    return folder


def make(flavor: str, **settings: object) -> Harness:
    """Build a harness; CLI flavors run the fake CLI under this Python."""
    if flavor == "fake":
        return FakeHarness(FakeSettings.model_validate(settings))
    command = [sys.executable, str(FAKE_CLI), flavor]
    return CLASSES[flavor](CliSettings.model_validate({"command": command, **settings}))


def mode(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Pick the fake CLI scenario."""
    monkeypatch.setenv("FAKE_CLI_MODE", name)


def collect(events: Iterator[HarnessEvent]) -> list[HarnessEvent]:
    """Drain a run."""
    return list(events)


@pytest.mark.usefixtures("calls")
@pytest.mark.parametrize("flavor", ALL)
class TestContract:
    """Every backend honors the same contract."""

    def test_info_maps_every_permission(self, flavor: str) -> None:
        """Each level maps to explicit, distinct flags."""
        info = make(flavor).info()
        assert info.id == flavor
        assert info.available
        assert info.problem is None
        assert set(info.permissions) == set(Permission)
        mapped = [tuple(args) for args in info.permissions.values()]
        assert all(mapped)
        assert len(set(mapped)) == len(mapped)

    def test_structured(self, flavor: str) -> None:
        """A schema-matching object comes back with usage."""
        result = make(flavor).structured(
            StructuredRequest(
                prompt="Is it ok?", output_schema=SCHEMA, system="Be terse"
            )
        )
        assert "ok" in result.value
        assert result.usage.output is not None
        assert result.usage.output > 0

    def test_run(self, flavor: str, workspace: Path) -> None:
        """A run streams text, tools, and usage, and ends with one RESULT."""
        events = collect(
            make(flavor).run(RunRequest(prompt="Do it", cwd=str(workspace)))
        )
        kinds = [event.kind for event in events]
        assert kinds[-1] == EventKind.RESULT
        assert kinds.count(EventKind.RESULT) == 1
        assert {EventKind.TEXT, EventKind.TOOL, EventKind.USAGE} <= set(kinds)
        final = events[-1]
        assert not final.is_error
        assert final.text
        assert final.session_id

    def test_resume(self, flavor: str, workspace: Path) -> None:
        """A session id is passed back to the runtime."""
        harness = make(flavor)
        assert harness.capabilities().resume
        events = collect(
            harness.run(
                RunRequest(prompt="More", cwd=str(workspace), session_id="prior-1")
            )
        )
        assert events[-1].session_id == "prior-1"

    def test_missing_workdir(self, flavor: str, tmp_path: Path) -> None:
        """Runs refuse a working directory that does not exist."""
        with pytest.raises(HarnessError, match="does not exist"):
            collect(make(flavor).run(RunRequest(prompt="x", cwd=str(tmp_path / "no"))))


@pytest.mark.parametrize("flavor", list(CLASSES))
class TestCliContract:
    """Behavior shared by the CLI-backed harnesses."""

    @pytest.mark.parametrize("permission", list(Permission))
    def test_permission_flags_are_passed(
        self, flavor: str, permission: Permission, workspace: Path, calls: Calls
    ) -> None:
        """The run's argv contains exactly the documented mapping."""
        harness = make(flavor)
        collect(
            harness.run(
                RunRequest(prompt="x", cwd=str(workspace), permission=permission)
            )
        )
        argv = calls.argv()
        expected = harness.permission_args(permission)
        assert any(argv[i : i + len(expected)] == expected for i in range(len(argv))), (
            argv
        )
        for other in Permission:
            flags = harness.permission_args(other)
            if other == permission or set(flags) <= set(expected):
                continue
            assert not any(
                argv[i : i + len(flags)] == flags for i in range(len(argv))
            ), (other, argv)
        assert Path(str(calls.last()["cwd"])).resolve() == workspace.resolve()

    def test_model_and_extra_args(
        self, flavor: str, workspace: Path, calls: Calls
    ) -> None:
        """Configured models and extra args reach the CLI; requests override."""
        harness = make(flavor, model="base-model", extra_args=["--extra"])
        collect(harness.run(RunRequest(prompt="x", cwd=str(workspace))))
        argv = calls.argv()
        assert argv[argv.index("--model") + 1] == "base-model"
        assert "--extra" in argv
        collect(harness.run(RunRequest(prompt="x", cwd=str(workspace), model="m2")))
        argv = calls.argv()
        assert argv[argv.index("--model") + 1] == "m2"

    def test_no_model_flag_by_default(
        self, flavor: str, workspace: Path, calls: Calls
    ) -> None:
        """Without a model the CLI picks its own."""
        collect(make(flavor).run(RunRequest(prompt="x", cwd=str(workspace))))
        assert "--model" not in calls.argv()

    def test_structured_runs_in_a_scratch_folder(
        self, flavor: str, calls: Calls
    ) -> None:
        """Structured calls never run in the user's folders."""
        make(flavor).structured(StructuredRequest(prompt="x", output_schema=SCHEMA))
        cwd = Path(str(calls.last()["cwd"]))
        assert f"jot-{flavor}-" in cwd.name
        assert not cwd.exists()

    def test_prose_is_parsed(
        self, flavor: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JSON wrapped in prose or fences is still found."""
        mode(monkeypatch, "prose")
        result = make(flavor).structured(
            StructuredRequest(prompt="x", output_schema=SCHEMA)
        )
        assert result.value == {"ok": True}

    def test_missing_required_keys(
        self, flavor: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Answers lacking required keys are refused."""
        mode(monkeypatch, "missing")
        with pytest.raises(HarnessError, match="lacks required keys"):
            make(flavor).structured(StructuredRequest(prompt="x", output_schema=SCHEMA))

    def test_crash_reports_stderr(
        self, flavor: str, monkeypatch: pytest.MonkeyPatch, workspace: Path
    ) -> None:
        """A non-zero exit raises with the CLI's own message."""
        mode(monkeypatch, "crash")
        with pytest.raises(HarnessError, match="exited 3: fatal: not signed in"):
            make(flavor).structured(StructuredRequest(prompt="x", output_schema=SCHEMA))
        with pytest.raises(HarnessError, match="not signed in"):
            collect(make(flavor).run(RunRequest(prompt="x", cwd=str(workspace))))

    def test_empty_output(self, flavor: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """A CLI that answers nothing is an error."""
        mode(monkeypatch, "empty")
        with pytest.raises(HarnessError, match="returned no"):
            make(flavor).structured(StructuredRequest(prompt="x", output_schema=SCHEMA))

    def test_runtime_error_ends_in_error_result(
        self, flavor: str, monkeypatch: pytest.MonkeyPatch, workspace: Path
    ) -> None:
        """A failure the CLI reports is a RESULT with is_error."""
        mode(monkeypatch, "error")
        events = collect(make(flavor).run(RunRequest(prompt="x", cwd=str(workspace))))
        assert events[-1].kind == EventKind.RESULT
        assert events[-1].is_error

    def test_missing_executable(self, flavor: str, tmp_path: Path) -> None:
        """A CLI that is not installed is reported, not crashed on."""
        harness = CLASSES[flavor](CliSettings(command=[str(tmp_path / "nope")]))
        info = harness.info()
        assert not info.available
        assert info.problem is not None
        assert "not found" in info.problem
        with pytest.raises(HarnessError, match="not found"):
            harness.structured(StructuredRequest(prompt="x"))

    def test_default_program(self, flavor: str) -> None:
        """Without a configured command the CLI's own name is used."""
        harness = CLASSES[flavor](CliSettings())
        assert harness.process.command == [flavor]


class TestClaude:
    """Claude Code specifics."""

    def test_structured_flags(self, calls: Calls) -> None:
        """Structured calls are tool-less, lean, and schema-constrained."""
        result = make("claude").structured(
            StructuredRequest(prompt="Q?", output_schema=SCHEMA, system="Sys")
        )
        argv = calls.argv()
        assert argv[argv.index("--tools") + 1] == ""
        assert json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMA
        assert argv[argv.index("--system-prompt") + 1] == "Sys"
        assert "--no-session-persistence" in argv
        assert calls.last()["stdin"] == "Q?"
        assert result.model == "claude-test"
        assert result.usage == Usage(
            input=11, output=7, cache_read=3, cache_write=2, cost_usd=0.0125
        )

    def test_structured_error(
        self, calls: Calls, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An is_error result raises with its text."""
        del calls
        mode(monkeypatch, "error")
        with pytest.raises(HarnessError, match="claude failed: boom"):
            make("claude").structured(StructuredRequest(prompt="x"))

    def test_run_events(self, calls: Calls, workspace: Path) -> None:
        """Assistant blocks become thinking, tool, and text events."""
        events = collect(
            make("claude").run(
                RunRequest(
                    prompt="Go",
                    cwd=str(workspace),
                    system="Extra",
                    session_id="s9",
                )
            )
        )
        assert [(e.kind, e.text) for e in events[:4]] == [
            (EventKind.THINKING, "Let me look"),
            (EventKind.TOOL, "Read a.py"),
            (EventKind.TOOL, "TodoWrite"),
            (EventKind.TEXT, "All done"),
        ]
        argv = calls.argv()
        assert argv[argv.index("--append-system-prompt") + 1] == "Extra"
        assert argv[argv.index("--resume") + 1] == "s9"
        assert events[-1].model == "claude-test"
        usage = events[-2].usage
        assert usage is not None
        assert usage.cost_usd == 0.0125

    def test_run_without_result(
        self, calls: Calls, monkeypatch: pytest.MonkeyPatch, workspace: Path
    ) -> None:
        """A stream that stops early still ends with an error RESULT."""
        del calls
        mode(monkeypatch, "empty")
        events = collect(make("claude").run(RunRequest(prompt="x", cwd=str(workspace))))
        assert [e.kind for e in events] == [EventKind.RESULT]
        assert events[0].is_error


class TestCodex:
    """Codex specifics."""

    def test_structured_uses_a_schema_file(self, calls: Calls) -> None:
        """The schema is written to a file and the call is read-only and lean."""
        result = make("codex").structured(
            StructuredRequest(prompt="Q?", output_schema=SCHEMA, system="Sys")
        )
        last = calls.last()
        assert last["schema_file"] == SCHEMA
        assert last["stdin"] == "Sys\n\n---\n\nQ?"
        argv = calls.argv()
        assert argv[:2] == ["exec", "--json"]
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert "--ephemeral" in argv
        assert argv[-1] == "-"
        assert result.usage == Usage(input=60, output=9, cache_read=40)

    def test_structured_failure(
        self, calls: Calls, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """turn.failed raises."""
        del calls
        mode(monkeypatch, "error")
        with pytest.raises(HarnessError, match="quota"):
            make("codex").structured(StructuredRequest(prompt="x"))

    def test_run_items(self, calls: Calls, workspace: Path) -> None:
        """Items map to events; resume comes after the options."""
        events = collect(
            make("codex").run(
                RunRequest(prompt="Go", cwd=str(workspace), session_id="t-1")
            )
        )
        assert [(e.kind, e.text) for e in events[:5]] == [
            (EventKind.THINKING, "Let me look"),
            (EventKind.TOOL, "$ ls"),
            (EventKind.TOOL, "edit a.py, b.py"),
            (EventKind.ERROR, "minor"),
            (EventKind.TEXT, "All done"),
        ]
        argv = calls.argv()
        assert argv[-3:] == ["resume", "t-1", "-"]
        assert events[-1].text == "All done"
        assert events[-1].session_id == "t-1"

    def test_usage_without_cache(self) -> None:
        """Missing cache counts leave input as reported."""
        assert CodexHarness.usage({"input_tokens": 5}).input == 5
        assert CodexHarness.usage({}).input is None


class TestCopilot:
    """Copilot specifics."""

    def test_structured_prompt_and_usage(self, calls: Calls) -> None:
        """The schema goes in the prompt; tokens are estimated."""
        result = make("copilot").structured(
            StructuredRequest(prompt="Q?", output_schema=SCHEMA, system="Sys")
        )
        argv = calls.argv()
        prompt = argv[argv.index("-p") + 1]
        assert prompt.startswith("Sys\n\n---\n\nQ?")
        assert '"required": ["ok"]' in prompt
        assert "--available-tools=view" in argv
        assert not result.usage.measured
        assert result.usage.premium_requests == 1

    def test_run_events(self, calls: Calls, workspace: Path) -> None:
        """Copilot events map to the shared kinds."""
        del calls
        events = collect(
            make("copilot").run(RunRequest(prompt="Go", cwd=str(workspace)))
        )
        assert [e.kind for e in events] == [
            EventKind.THINKING,
            EventKind.TOOL,
            EventKind.TEXT,
            EventKind.USAGE,
            EventKind.RESULT,
        ]
        assert events[1].text == 'view {"path": "a.py"}'
        assert events[-1].model == "gpt-test"
        assert events[-1].session_id == "copilot-session"

    def test_error_event(
        self, calls: Calls, monkeypatch: pytest.MonkeyPatch, workspace: Path
    ) -> None:
        """A session error is surfaced and marks the result failed."""
        del calls
        mode(monkeypatch, "error")
        events = collect(
            make("copilot").run(RunRequest(prompt="x", cwd=str(workspace)))
        )
        assert events[0].kind == EventKind.ERROR
        assert "quota" in events[0].text


class TestFake:
    """The offline fake."""

    def test_canned_overlay(self) -> None:
        """Canned values override the minimal instance."""
        harness = FakeHarness(FakeSettings(canned={"ok": False, "extra": 1}))
        result = harness.structured(StructuredRequest(prompt="x", output_schema=SCHEMA))
        assert result.value == {"ok": False, "extra": 1}
        assert result.model == "fake-model"

    def test_fail(self, workspace: Path) -> None:
        """Configured failures raise or end in an error result."""
        harness = FakeHarness(FakeSettings(fail=True))
        with pytest.raises(HarnessError):
            harness.structured(StructuredRequest(prompt="x"))
        events = collect(harness.run(RunRequest(prompt="x", cwd=str(workspace))))
        assert [e.kind for e in events] == [EventKind.ERROR, EventKind.RESULT]
        assert events[-1].is_error

    @pytest.mark.parametrize(
        ("schema", "expected"),
        [
            ({"const": 3}, 3),
            ({"type": "string", "default": "d"}, "d"),
            ({"enum": ["a", "b"]}, "a"),
            ({"type": ["integer", "null"]}, 0),
            ({"type": "array"}, []),
            ({"type": "number"}, 0),
            ({"type": "boolean"}, False),
            ({"type": "null"}, None),
            ({}, None),
            (
                {
                    "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
                    "required": ["a", 7],
                },
                {"a": ""},
            ),
        ],
    )
    def test_minimal(self, schema: Metadata, expected: object) -> None:
        """The smallest value satisfying common shapes."""
        assert minimal(schema) == expected


class Limited(Harness):
    """A harness with only read-only runs."""

    id = "limited"

    @override
    def capabilities(self) -> Capabilities:
        """Read-only runs, no structured mode."""
        return Capabilities(structured=False, permissions=[Permission.READ_ONLY])

    @override
    def permission_args(self, permission: Permission) -> list[str]:
        """No flags."""
        return []

    @override
    def structured(self, request: StructuredRequest) -> StructuredResult:
        """Unsupported."""
        raise NotImplementedError

    @override
    def run(self, request: RunRequest) -> Iterator[HarnessEvent]:
        """Refuse levels above read-only."""
        self.check_permission(request.permission)
        yield HarnessEvent(kind=EventKind.RESULT)


class TestApi:
    """Shared helpers in the contract module."""

    def test_check_permission(self, workspace: Path) -> None:
        """Unsupported levels are refused; info lists only supported ones."""
        harness = Limited()
        info = harness.info()
        assert info.label == "limited"
        assert list(info.permissions) == [Permission.READ_ONLY]
        with pytest.raises(HarnessError, match="does not support full"):
            collect(
                harness.run(
                    RunRequest(
                        prompt="x", cwd=str(workspace), permission=Permission.FULL
                    )
                )
            )

    @pytest.mark.parametrize(
        "text",
        [
            '{"a": 1}',
            'Here:\n```json\n{"a": 1}\n```',
            'prefix {"a": 1} suffix',
            '```\n{"a": 1}\n```',
        ],
    )
    def test_parse_object(self, text: str) -> None:
        """Objects are found in bare, fenced, or embedded text."""
        assert Harness.parse_object(text) == {"a": 1}

    @pytest.mark.parametrize("text", ["", "[1, 2]", "no json", "{broken"])
    def test_parse_object_refuses(self, text: str) -> None:
        """Text without an object raises."""
        with pytest.raises(HarnessError, match="No JSON object"):
            Harness.parse_object(text)

    def test_check_required(self) -> None:
        """Only top-level string keys are checked."""
        assert Harness.check_required({"a": 1}, {"required": ["a", 5]}) == {"a": 1}
        assert Harness.check_required({}, {"required": "a"}) == {}
        with pytest.raises(HarnessError, match="'b'"):
            Harness.check_required({"a": 1}, {"required": ["a", "b"]})

    def test_usage_estimate(self) -> None:
        """Estimates are rough and flagged."""
        usage = Usage.estimate("x" * 40, "y" * 8)
        assert (usage.input, usage.output, usage.measured) == (11, 3, False)

    def test_value_helpers(self) -> None:
        """Helpers reject the wrong JSON types, including bools as numbers."""
        assert text_of(1) == ""
        assert text_of("a") == "a"
        assert obj([1]) == {}
        assert integer(value=True) is None
        assert integer(2) == 2
        assert number(value=False) is None
        assert number("1") is None
        assert number(2) == 2.0

    def test_workdir(self, workspace: Path) -> None:
        """Existing folders pass; files do not."""
        assert workdir(str(workspace)) == workspace
        file = workspace / "f.txt"
        file.write_text("x")
        with pytest.raises(HarnessError):
            workdir(str(file))

    def test_schema_alias_free(self) -> None:
        """Requests default to any object."""
        assert StructuredRequest(prompt="x").output_schema == {"type": "object"}


class TestProcess:
    """The JSON-lines process runner."""

    def test_empty_command(self) -> None:
        """A command needs a program."""
        with pytest.raises(HarnessError, match="Empty command"):
            JsonlProcess([], "hint")

    def test_parse_and_decode(self) -> None:
        """Only object lines are kept; non-UTF-8 bytes decode anyway."""
        assert JsonlProcess.parse(b'{"a": 1}\n') == {"a": 1}
        assert JsonlProcess.parse(b"[1]\n") is None
        assert JsonlProcess.parse(b'{"a": }\n') is None
        assert JsonlProcess.parse(b"text\n") is None
        assert JsonlProcess.decode(b"caf\xe9")

    def test_stopping_early_kills_the_process(self, workspace: Path) -> None:
        """Closing the stream ends the child process."""
        script = "import time\nprint('{\"n\": 1}', flush=True)\ntime.sleep(30)\n"
        process = JsonlProcess([sys.executable, "-c", script], "hint")
        stream = process.stream([], workspace)
        assert next(stream) == {"n": 1}
        stream.close()

    def test_stdin_is_optional(self, workspace: Path) -> None:
        """Without stdin the child sees end of input."""
        script = "import sys\nprint('{\"got\": %d}' % len(sys.stdin.read()))\n"
        process = JsonlProcess([sys.executable, "-c", script], "hint")
        assert list(process.stream([], workspace)) == [{"got": 0}]
        assert list(process.stream([], workspace, "abc")) == [{"got": 3}]
