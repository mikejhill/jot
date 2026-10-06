"""Claude backend: Claude Agent SDK (uses the Claude Code login or an API key)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, override

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)
from claude_agent_sdk.types import SystemPromptPreset
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
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

PLAN_TOOLS = ["Read", "Grep", "Glob", "WebSearch", "WebFetch"]
TOOL_DETAIL_KEYS = ("command", "file_path", "pattern", "path", "url", "query")


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
            system_prompt=system,
            tools=[],
            setting_sources=[],
            output_format={"type": "json_schema", "schema": schema},
            model=self.model,
        )
        result = await self._result(prompt, options)
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
        try:
            # Drain the SDK generator fully; leaving early breaks its shutdown.
            async for message in query(prompt=request.prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for event in self._assistant_events(message):
                        yield event
                elif isinstance(message, ResultMessage):
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
            type="preset", preset="claude_code", append=request.system
        )
        common = ClaudeAgentOptions(
            cwd=request.cwd,
            system_prompt=system,
            setting_sources=["project"],
            model=request.model or self.model,
        )
        if request.mode is AgentMode.PLAN:
            common.allowed_tools = list(PLAN_TOOLS)
            common.permission_mode = "dontAsk"
        else:
            # Execution happens only after approval (or by explicit direct flow),
            # normally inside an isolated git worktree.
            common.permission_mode = "bypassPermissions"
        return common

    def _assistant_events(self, message: AssistantMessage) -> list[AgentEvent]:
        """Translate text and tool-use blocks into facade events."""
        events: list[AgentEvent] = []
        for block in message.content:
            if isinstance(block, TextBlock) and block.text.strip():
                events.append(AgentEvent(AgentEventKind.TEXT, block.text))
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
