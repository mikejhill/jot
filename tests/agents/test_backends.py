"""Argument construction and event mapping for each CLI/SDK agent backend."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import StreamEvent
from tests.agents.conftest import ProcessScript

from jot.agents import claude as claude_module
from jot.agents.base import (
    AgentBackend,
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentMode,
    JsonObject,
    RunRequest,
    TokenUsage,
)
from jot.agents.claude import ClaudeBackend
from jot.agents.codex import CodexBackend
from jot.agents.copilot import CopilotBackend
from jot.agents.fake import FakeBackend
from jot.agents.registry import BackendRegistry

SCHEMA: JsonObject = {"type": "object"}


class Collect:
    """Drain backend runs synchronously."""

    @staticmethod
    def run(backend: AgentBackend, request: RunRequest) -> list[AgentEvent]:
        """Return every event of a run."""

        async def drain() -> list[AgentEvent]:
            return [event async for event in backend.run(request)]

        return asyncio.run(drain())


class TestCodex:
    """Codex exec argument and JSONL mapping."""

    def test_structured(self, process_script: ProcessScript) -> None:
        """Structured calls are lean, read-only, and parse the final message."""
        process_script.events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "x"}},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"a": 1}'},
            },
        ]
        result = asyncio.run(CodexBackend("m1").structured("sys", "go", SCHEMA))
        assert result == {"a": 1}
        args = process_script.specs[0].args
        assert "--output-schema" in args
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert args[args.index("--model") + 1] == "m1"
        assert process_script.specs[0].stdin == "go"

    @pytest.mark.parametrize(
        "events",
        [[], [{"type": "turn.failed", "error": {"message": "limit"}}]],
    )
    def test_structured_failure(
        self, process_script: ProcessScript, events: list[JsonObject]
    ) -> None:
        """Missing messages and failed turns raise AgentError."""
        process_script.events = events
        with pytest.raises(AgentError):
            asyncio.run(CodexBackend().structured("", "go", SCHEMA))

    def test_run(self, process_script: ProcessScript, tmp_path: Path) -> None:
        """Run events map to tool/text/error and end with a result."""
        process_script.events = [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "ls"},
            },
            {
                "type": "item.completed",
                "item": {"type": "file_change", "changes": [{"path": "a.py"}]},
            },
            {"type": "item.completed", "item": {"type": "error", "message": "warn"}},
            {"type": "item.completed", "item": {"type": "reasoning"}},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "done"},
            },
        ]
        request = RunRequest("do", tmp_path, AgentMode.EXECUTE, system="rules")
        events = Collect.run(CodexBackend(), request)
        assert [e.kind for e in events] == [
            AgentEventKind.TOOL,
            AgentEventKind.TOOL,
            AgentEventKind.ERROR,
            AgentEventKind.TEXT,
            AgentEventKind.RESULT,
        ]
        assert events[-1].text == "done"
        assert events[-1].session_id == "t1"
        args = process_script.specs[0].args
        assert args[args.index("--sandbox") + 1] == "workspace-write"
        assert (process_script.specs[0].stdin or "").startswith("rules")

    def test_run_failure(self, process_script: ProcessScript, tmp_path: Path) -> None:
        """A failed turn yields an error result."""
        process_script.events = [{"type": "error", "message": "boom"}]
        events = Collect.run(CodexBackend(), RunRequest("do", tmp_path, AgentMode.PLAN))
        assert events[-1].is_error
        args = process_script.specs[0].args
        assert args[args.index("--sandbox") + 1] == "read-only"


class TestCopilot:
    """Copilot CLI argument and JSONL mapping."""

    def test_structured(self, process_script: ProcessScript) -> None:
        """Tools are disabled and the last assistant message is parsed."""
        process_script.events = [
            {"type": "assistant.message", "data": {"content": '```json\n{"b": 2}\n```'}}
        ]
        assert asyncio.run(CopilotBackend("gpt").structured("s", "p", SCHEMA)) == {
            "b": 2
        }
        args = process_script.specs[0].args
        assert "--available-tools=" in args
        assert args[args.index("--model") + 1] == "gpt"

    @pytest.mark.usefixtures("process_script")
    def test_structured_empty(self) -> None:
        """No assistant message raises."""
        with pytest.raises(AgentError):
            asyncio.run(CopilotBackend().structured("s", "p", SCHEMA))

    @pytest.mark.parametrize(
        ("mode", "flag"),
        [
            (AgentMode.PLAN, "--available-tools=view,glob,grep,web_fetch"),
            (AgentMode.EXECUTE, "--allow-all-tools"),
        ],
    )
    def test_run(
        self,
        process_script: ProcessScript,
        tmp_path: Path,
        mode: AgentMode,
        flag: str,
    ) -> None:
        """Plan runs restrict tools; execute runs allow all; events map."""
        process_script.events = [
            {"type": "session.start", "data": {"sessionId": "s1"}},
            {
                "type": "tool.execution_start",
                "data": {"toolName": "view", "arguments": {}},
            },
            {"type": "assistant.message", "data": {"content": "  "}},
            {"type": "session.error", "data": {"message": "x"}},
            {"type": "assistant.message", "data": {"content": "final"}},
        ]
        events = Collect.run(CopilotBackend(), RunRequest("do", tmp_path, mode, "sys"))
        assert flag in process_script.specs[0].args
        assert [e.kind for e in events] == [
            AgentEventKind.TOOL,
            AgentEventKind.ERROR,
            AgentEventKind.TEXT,
            AgentEventKind.RESULT,
        ]
        assert events[-1].text == "final"
        assert events[-1].session_id == "s1"


class SdkScript:
    """Stand-in for claude_agent_sdk.query."""

    def __init__(self, messages: list[object], error: Exception | None = None) -> None:
        self.messages = messages
        self.error = error
        self.options: list[ClaudeAgentOptions] = []

    async def query(
        self, *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[object]:
        """Yield scripted messages, then optionally raise."""
        del prompt
        self.options.append(options)
        for message in self.messages:
            yield message
        if self.error is not None:
            raise self.error


class TestClaude:
    """Claude Agent SDK options and message mapping."""

    @staticmethod
    def result(
        *,
        result: str | None = "",
        structured_output: JsonObject | None = None,
        is_error: bool = False,
    ) -> ResultMessage:
        """Build a ResultMessage with sensible defaults."""
        return ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=is_error,
            num_turns=1,
            session_id="sess",
            result=result,
            total_cost_usd=0.5,
            structured_output=structured_output,
        )

    def patch(self, monkeypatch: pytest.MonkeyPatch, script: SdkScript) -> None:
        """Route the backend's query calls to the script."""
        monkeypatch.setattr(claude_module, "query", script.query)

    def test_structured_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Schema output is returned and tools are disabled."""
        script = SdkScript([self.result(structured_output={"c": 3})])
        self.patch(monkeypatch, script)
        assert asyncio.run(ClaudeBackend("haiku").structured("s", "p", SCHEMA)) == {
            "c": 3
        }
        assert script.options[0].tools == []
        assert script.options[0].model == "haiku"

    def test_structured_text_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without structured output, the result text is parsed."""
        self.patch(monkeypatch, SdkScript([self.result(result='{"d": 4}')]))
        assert asyncio.run(ClaudeBackend().structured("s", "p", SCHEMA)) == {"d": 4}

    @pytest.mark.parametrize(
        "script",
        [
            SdkScript([]),
            SdkScript([], ClaudeSDKError("down")),
        ],
    )
    def test_structured_failures(
        self, monkeypatch: pytest.MonkeyPatch, script: SdkScript
    ) -> None:
        """Missing results and SDK errors raise AgentError."""
        self.patch(monkeypatch, script)
        with pytest.raises(AgentError):
            asyncio.run(ClaudeBackend().structured("s", "p", SCHEMA))

    def test_structured_error_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An error result raises AgentError."""
        self.patch(monkeypatch, SdkScript([self.result(is_error=True)]))
        with pytest.raises(AgentError):
            asyncio.run(ClaudeBackend().structured("s", "p", SCHEMA))

    def test_run_plan(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Plan runs map blocks; usage comes from message_delta, totals from result."""
        message = AssistantMessage(
            content=[
                ThinkingBlock("hmm", "sig"),
                TextBlock("thinking out loud"),
                TextBlock("  "),
                ToolUseBlock("1", "Read", {"file_path": "a.py"}),
                ToolUseBlock("2", "Glob", {}),
            ],
            model="m",
            usage={"input_tokens": 1, "output_tokens": 1},
        )

        def stream(event: dict[str, object]) -> StreamEvent:
            return StreamEvent(uuid="u", session_id="s", event=event)

        final = {
            "input_tokens": 10,
            "output_tokens": 124,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 7,
        }
        script = SdkScript(
            [
                stream({"type": "message_start", "message": {"model": "m1"}}),
                stream({"type": "content_block_delta"}),
                message,
                stream({"type": "message_delta", "usage": final}),
                stream({"type": "message_delta"}),
                self.result(result="plan"),
            ]
        )
        self.patch(monkeypatch, script)
        events = Collect.run(ClaudeBackend(), RunRequest("p", tmp_path, AgentMode.PLAN))
        assert [(e.kind.value, e.text) for e in events] == [
            ("thinking", "hmm"),
            ("text", "thinking out loud"),
            ("tool", "Read a.py"),
            ("tool", "Glob"),
            ("usage", ""),
            ("usage", "total"),
            ("result", "plan"),
        ]
        assert events[4].model == "m1"
        assert events[4].usage == TokenUsage(10, 124, 100, 7)
        assert events[5].usage == TokenUsage()
        assert events[-1].cost_usd == 0.5
        options = script.options[0]
        assert options.permission_mode == "dontAsk"
        assert "Write" not in options.allowed_tools

    def test_run_execute(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Execute runs bypass permissions; SDK errors become error results."""
        script = SdkScript([], ClaudeSDKError("boom"))
        self.patch(monkeypatch, script)
        events = Collect.run(
            ClaudeBackend(), RunRequest("p", tmp_path, AgentMode.EXECUTE)
        )
        assert events[-1].is_error
        assert script.options[0].permission_mode == "bypassPermissions"

    def test_run_without_result(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A run that never reports a result ends with an error result."""
        self.patch(monkeypatch, SdkScript([]))
        events = Collect.run(ClaudeBackend(), RunRequest("p", tmp_path, AgentMode.PLAN))
        assert events == [AgentEvent(AgentEventKind.RESULT, "", is_error=True)]


class TestUsageAndThinking:
    """Per-turn usage and reasoning mapping for the CLI backends."""

    def test_codex(
        self,
        process_script: ProcessScript,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reasoning maps to thinking; turn usage is net of cached input."""
        process_script.events = [
            {"type": "item.completed", "item": {"type": "reasoning", "text": "hmm"}},
            {"type": "item.completed", "item": {"type": "reasoning", "text": " "}},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 120,
                    "cached_input_tokens": 100,
                    "output_tokens": 7,
                },
            },
        ]
        events = Collect.run(
            CodexBackend("gpt-x"), RunRequest("p", tmp_path, AgentMode.PLAN)
        )
        assert [e.kind for e in events] == [
            AgentEventKind.THINKING,
            AgentEventKind.USAGE,
            AgentEventKind.RESULT,
        ]
        assert events[1].model == "gpt-x"
        assert events[1].usage == TokenUsage(input=20, output=7, cache_read=100)
        codex_home = tmp_path / "codex"
        codex_home.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        assert CodexBackend.default_model() == "codex default"
        (codex_home / "config.toml").write_text('model = "gpt-6.1-sol"\n', "utf-8")
        assert CodexBackend.default_model() == "gpt-6.1-sol"

    def test_copilot(self, process_script: ProcessScript, tmp_path: Path) -> None:
        """Model per turn, reasoning, and premium requests from the result."""
        process_script.events = [
            {"type": "assistant.message", "data": {"model": "mai", "content": "hi"}},
            {"type": "assistant.reasoning", "data": {"content": "think"}},
            {"type": "assistant.turn_end", "data": {}},
            {"type": "result", "sessionId": "s9", "usage": {"premiumRequests": 2}},
            {"type": "result", "usage": {}},
        ]
        events = Collect.run(
            CopilotBackend(), RunRequest("p", tmp_path, AgentMode.PLAN)
        )
        kinds = [e.kind for e in events]
        assert kinds == [
            AgentEventKind.TEXT,
            AgentEventKind.THINKING,
            AgentEventKind.USAGE,
            AgentEventKind.USAGE,
            AgentEventKind.RESULT,
        ]
        assert events[2].model == "mai"
        assert events[2].usage == TokenUsage()
        assert events[3].usage == TokenUsage(premium_requests=2.0)
        assert events[-1].session_id == "s9"

    def test_token_usage(self) -> None:
        """Sums keep None only where both sides are unreported."""
        total = TokenUsage(1, None, 3).plus(TokenUsage(2, None, None, 4, 1.0))
        assert total == TokenUsage(3, None, 3, 4, 1.0)
        assert total.as_json()["cache_write"] == 4


class TestRegistryAndFake:
    """Backend lookup and the deterministic fake."""

    def test_create(self) -> None:
        """Known names build backends; 'default' model means provider default."""
        backend = BackendRegistry.create("codex", "default")
        assert isinstance(backend, CodexBackend)
        assert backend.model is None
        assert BackendRegistry.create("claude", "opus").model == "opus"
        assert "fake" not in BackendRegistry.names()

    def test_unknown(self) -> None:
        """Unknown names raise with the valid choices."""
        with pytest.raises(AgentError, match="choose one of"):
            BackendRegistry.create("nope")

    def test_fake(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fake returns canned output and scripted runs."""
        monkeypatch.setattr(FakeBackend, "canned", {"x": 1})
        assert asyncio.run(FakeBackend().structured("", "", SCHEMA)) == {"x": 1}
        plan = Collect.run(FakeBackend(), RunRequest("p", tmp_path, AgentMode.PLAN))
        assert '"plan"' in plan[-1].text
