"""Copilot backend: drives the GitHub Copilot CLI in JSON output mode."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, override

from jot.agents.base import (
    AgentBackend,
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentMode,
    Json,
    JsonObject,
    RunRequest,
)
from jot.agents.process import JsonlProcess, ProcessSpec

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

PLAN_TOOLS = "view,glob,grep,web_fetch"


class CopilotBackend(AgentBackend):
    """Agent backend backed by the GitHub Copilot CLI."""

    name: ClassVar[str] = "copilot"

    def __init__(self, model: str | None = None) -> None:
        super().__init__(model)
        self._process = JsonlProcess()

    @override
    async def structured(
        self, system: str, prompt: str, schema: JsonObject
    ) -> JsonObject:
        """Ask for JSON matching ``schema`` with all tools disabled."""
        full = (
            f"{system}\n\n{prompt}\n\nRespond with ONLY one JSON object (no prose, "
            f"no code fences) matching this JSON Schema:\n{json.dumps(schema)}"
        )
        with tempfile.TemporaryDirectory(prefix="jot-copilot-") as scratch:
            args = [*self._base_args(full), "--available-tools="]
            spec = ProcessSpec(args=tuple(args), cwd=Path(scratch))
            text = ""
            async for event in self._process.stream(spec):
                if Json.text(event.get("type")) == "assistant.message":
                    text = Json.text(Json.obj(event.get("data")).get("content")) or text
        if not text:
            raise AgentError("copilot returned no message")
        return self.parse_json_object(text)

    @override
    async def run(self, request: RunRequest) -> AsyncIterator[AgentEvent]:
        """Stream a Copilot agent run in the request's working directory."""
        prompt = (
            f"{request.system}\n\n---\n\n{request.prompt}"
            if request.system
            else request.prompt
        )
        args = self._base_args(prompt, request.model)
        if request.mode is AgentMode.PLAN:
            args.append(f"--available-tools={PLAN_TOOLS}")
        else:
            args.append("--allow-all-tools")
        spec = ProcessSpec(args=tuple(args), cwd=request.cwd)
        final = ""
        session_id: str | None = None
        async for event in self._process.stream(spec):
            kind = Json.text(event.get("type"))
            data = Json.obj(event.get("data"))
            session_id = session_id or Json.text(data.get("sessionId")) or None
            mapped = self._map(kind, data)
            if mapped is None:
                continue
            if mapped.kind is AgentEventKind.TEXT:
                final = mapped.text
            yield mapped
        yield AgentEvent(AgentEventKind.RESULT, final, session_id)

    def _base_args(self, prompt: str, model: str | None = None) -> list[str]:
        """Return the shared non-interactive command line."""
        copilot = JsonlProcess.resolve(
            "copilot", "install GitHub Copilot CLI and log in"
        )
        args = [copilot, "-p", prompt, "--output-format", "json", "--no-ask-user"]
        chosen = model or self.model
        if chosen:
            args += ["--model", chosen]
        return args

    def _map(self, kind: str, data: JsonObject) -> AgentEvent | None:
        """Translate a Copilot JSONL event into a facade event."""
        if kind == "assistant.message":
            content = Json.text(data.get("content"))
            return AgentEvent(AgentEventKind.TEXT, content) if content.strip() else None
        if kind == "tool.execution_start":
            name = Json.text(data.get("toolName"))
            arguments = json.dumps(data.get("arguments"))[:200]
            return AgentEvent(AgentEventKind.TOOL, f"{name} {arguments}")
        if kind in ("session.error", "error"):
            return AgentEvent(AgentEventKind.ERROR, json.dumps(data)[:800])
        return None
