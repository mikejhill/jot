"""Claude backend: Claude Agent SDK (uses the Claude Code login or an API key)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, ClassVar, override

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    query,
)
from claude_agent_sdk.types import StreamEvent, SystemPromptPreset
from pydantic import ValidationError

from jot.agents.base import (
    JSON_ADAPTER,
    AgentBackend,
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentMode,
    JsonObject,
    RunRequest,
    TokenUsage,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

PLAN_TOOLS = ["Read", "Grep", "Glob", "WebSearch", "WebFetch"]
# Claude Code flags for tool-less structured calls (triage, cleanup).
LEAN_CLI_ARGS: dict[str, str | None] = {
    "strict-mcp-config": None,  # ignore account/user MCP servers
    "disable-slash-commands": None,  # no skills or slash commands in context
    "no-session-persistence": None,  # don't save one-shot sessions
}
TOOL_DETAIL_KEYS = ("command", "file_path", "pattern", "path", "url", "query")


class TurnUsage:
    """Turn raw API stream events into one exact USAGE event per API turn.

    The usage on streamed AssistantMessages is a snapshot from ``message_start``,
    taken before output finishes. Each turn's ``message_delta`` stream event
    carries the final counts, so usage is read from there. That requires
    ``include_partial_messages``.
    """

    def __init__(self) -> None:
        self.model: str | None = None

    def observe(self, event: Mapping[str, object]) -> AgentEvent | None:
        """Track the turn's model; return a USAGE event at its ``message_delta``."""
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message")
            if isinstance(message, dict):
                model = message.get("model")
                self.model = model if isinstance(model, str) else self.model
            return None
        usage = event.get("usage")
        if kind != "message_delta" or not isinstance(usage, dict):
            return None
        return AgentEvent(
            AgentEventKind.USAGE, "", model=self.model, usage=self.tokens(usage)
        )

    @classmethod
    def tokens(cls, usage: Mapping[str, object]) -> TokenUsage:
        """Map Anthropic usage fields onto TokenUsage."""
        return TokenUsage(
            input=cls.number(usage.get("input_tokens")),
            output=cls.number(usage.get("output_tokens")),
            cache_read=cls.number(usage.get("cache_read_input_tokens")),
            cache_write=cls.number(usage.get("cache_creation_input_tokens")),
        )

    @staticmethod
    def number(value: object) -> int | None:
        """Return ``value`` if it is an int (not a bool)."""
        return value if isinstance(value, int) and not isinstance(value, bool) else None


class ClaudeBackend(AgentBackend):
    """Agent backend backed by the Claude Agent SDK."""

    name: ClassVar[str] = "claude"

    @override
    async def structured(
        self, system: str, prompt: str, schema: JsonObject
    ) -> JsonObject:
        """Run a tool-less query constrained to a JSON schema.

        Raises:
            AgentError: The query failed or returned no object.
        """
        options = ClaudeAgentOptions(
            system_prompt=system + "\n" + self.instructions,
            tools=[],
            setting_sources=[],
            output_format={"type": "json_schema", "schema": schema},
            model=self.model,
            # Without these, account MCP connectors, skills, and slash commands
            # still load: ~14.5k prompt tokens per call instead of ~1.2k.
            skills=[],
            extra_args=dict(LEAN_CLI_ARGS),
        )
        result = await self._result(prompt, options)
        self.last_usage = TurnUsage.tokens(result.usage or {})
        if result.is_error:
            raise AgentError(f"claude query failed: {result.errors or result.result}")
        if result.structured_output is not None:
            try:
                value = JSON_ADAPTER.validate_python(result.structured_output)
            except ValidationError as err:
                raise AgentError("claude returned non-JSON structured output") from err
            if isinstance(value, dict):
                return value
        return self.parse_json_object(result.result or "")

    @override
    async def run(self, request: RunRequest) -> AsyncIterator[AgentEvent]:
        """Stream a Claude Code agent run in the request's working directory."""
        options = self._run_options(request)
        final: AgentEvent | None = None
        turns = TurnUsage()
        try:
            # Drain the SDK generator fully; leaving early breaks its shutdown.
            async for message in query(prompt=request.prompt, options=options):
                if isinstance(message, StreamEvent):
                    usage = turns.observe(message.event)
                    if usage is not None:
                        yield usage
                elif isinstance(message, AssistantMessage):
                    for event in self._assistant_events(message):
                        yield event
                elif isinstance(message, ResultMessage):
                    yield AgentEvent(
                        AgentEventKind.USAGE,
                        "total",
                        model=turns.model,
                        usage=TurnUsage.tokens(message.usage or {}),
                    )
                    final = AgentEvent(
                        AgentEventKind.RESULT,
                        message.result or "",
                        message.session_id,
                        message.total_cost_usd,
                        is_error=message.is_error,
                    )
        except ClaudeSDKError as err:
            final = AgentEvent(AgentEventKind.RESULT, str(err), is_error=True)
        yield final or AgentEvent(AgentEventKind.RESULT, "", is_error=True)

    def _run_options(self, request: RunRequest) -> ClaudeAgentOptions:
        """Build SDK options: read-only tools for plans, full access for execution."""
        system = SystemPromptPreset(
            type="preset",
            preset="claude_code",
            append=request.system + "\n" + self.instructions,
        )
        common = ClaudeAgentOptions(
            cwd=request.cwd,
            system_prompt=system,
            setting_sources=["project"] if self.loadout.project_instructions else [],
            skills=self.loadout.skills,
            plugins=[{"type": "local", "path": path} for path in self.loadout.plugins],
            extra_args=self._loadout_args(),
            model=request.model or self.model,
            # Needed for message_delta events, which carry each turn's final usage.
            include_partial_messages=True,
        )
        if request.mode is AgentMode.PLAN:
            common.allowed_tools = list(PLAN_TOOLS)
            common.permission_mode = "dontAsk"
        else:
            # Execution happens only after approval (or by explicit direct flow),
            # normally inside an isolated git worktree.
            common.permission_mode = "bypassPermissions"
        return common

    def _loadout_args(self) -> dict[str, str | None]:
        """Isolate MCP and skills unless explicitly included in this loadout."""
        args: dict[str, str | None] = {"no-session-persistence": None}
        if not self.loadout.skills:
            args["disable-slash-commands"] = None
        if self.loadout.mcp != "all":
            args["strict-mcp-config"] = None
            definitions = (
                self.harness_config.config.mcp_servers if self.harness_config else {}
            )
            missing = set(self.loadout.mcp) - set(definitions)
            if missing:
                raise AgentError(
                    f"MCP definitions required for: {', '.join(sorted(missing))}"
                )
            args["mcp-config"] = json.dumps(
                {"mcpServers": {key: definitions[key] for key in self.loadout.mcp}}
            )
        return args

    @override
    async def discover(self) -> dict[str, list[str]]:
        """Inspect the SDK init message from one short discovery session."""
        found = await super().discover()
        options = ClaudeAgentOptions(
            model=self.model,
            tools=[],
            skills="all",
            setting_sources=["user", "project"],
            max_turns=1,
        )
        async for message in query(prompt="Reply OK.", options=options):
            if isinstance(message, SystemMessage) and message.subtype == "init":
                for target, source in (
                    ("skills", "skills"),
                    ("mcp", "mcp_servers"),
                    ("plugins", "plugins"),
                ):
                    items = message.data.get(source, [])
                    if isinstance(items, list):
                        found[target] = [
                            item if isinstance(item, str) else str(item.get("name", ""))
                            for item in items
                            if isinstance(item, (str, dict))
                        ]
        return found

    def _assistant_events(self, message: AssistantMessage) -> list[AgentEvent]:
        """Translate text and tool-use blocks into facade events."""
        events: list[AgentEvent] = []
        for block in message.content:
            if isinstance(block, TextBlock) and block.text.strip():
                events.append(AgentEvent(AgentEventKind.TEXT, block.text))
            elif isinstance(block, ThinkingBlock) and block.thinking.strip():
                events.append(AgentEvent(AgentEventKind.THINKING, block.thinking))
            elif isinstance(block, ToolUseBlock):
                events.append(AgentEvent(AgentEventKind.TOOL, self._describe(block)))
        return events

    def _describe(self, block: ToolUseBlock) -> str:
        """Summarize a tool call as ``Name detail``."""
        for key in TOOL_DETAIL_KEYS:
            detail = block.input.get(key)
            if isinstance(detail, str) and detail:
                return f"{block.name} {detail[:200]}"
        return block.name

    async def _result(self, prompt: str, options: ClaudeAgentOptions) -> ResultMessage:
        """Drain a query and return its result message.

        Raises:
            AgentError: The SDK failed or no result arrived.
        """
        result: ResultMessage | None = None
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    result = message
        except ClaudeSDKError as err:
            raise AgentError(f"claude query failed: {err}") from err
        if result is None:
            raise AgentError("claude query ended without a result")
        return result
