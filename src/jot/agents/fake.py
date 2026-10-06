"""Deterministic offline backend for tests and demos."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, override

from jot.agents.base import (
    AgentBackend,
    AgentEvent,
    AgentEventKind,
    AgentMode,
    JsonObject,
    RunRequest,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class FakeBackend(AgentBackend):
    """Return canned structured output and a scripted run transcript."""

    name: ClassVar[str] = "fake"
    canned: ClassVar[JsonObject] = {}

    @override
    async def structured(
        self, system: str, prompt: str, schema: JsonObject
    ) -> JsonObject:
        """Return the class-level canned object (tests set ``FakeBackend.canned``)."""
        result: JsonObject = {**self.canned}
        return result

    @override
    async def run(self, request: RunRequest) -> AsyncIterator[AgentEvent]:
        """Emit one tool event, one text event, then a result."""
        yield AgentEvent(AgentEventKind.TOOL, f"Read {request.cwd}")
        if request.mode is AgentMode.PLAN:
            text = '{"plan": "1. Do the thing", "questions": [], "summary": "ok"}'
        else:
            text = "Done: implemented the change."
        yield AgentEvent(AgentEventKind.TEXT, text)
        yield AgentEvent(AgentEventKind.RESULT, text, session_id="fake-session")
