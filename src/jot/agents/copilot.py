"""Copilot backend: drives the GitHub Copilot CLI in JSON output mode."""

from __future__ import annotations

import json
import os
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
    TokenUsage,
)
from jot.agents.process import JsonlProcess, ProcessSpec

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

PLAN_TOOLS = "view,glob,grep,web_fetch"
# Flags for tool-less structured calls. An empty --available-tools= is ignored
# (all 26 tools, ~8.5k tokens, still load), so allow one harmless tool instead.
LEAN_ARGS = (
    "--available-tools=view",
    "--disable-builtin-mcps",
    "--no-custom-instructions",
)


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
            f"{system}\n{self.instructions}\n\n{prompt}\n\n"
            "Respond with ONLY one JSON object (no prose, "
            f"no code fences) matching this JSON Schema:\n{json.dumps(schema)}"
        )
        with tempfile.TemporaryDirectory(prefix="jot-copilot-") as scratch:
            args = [*self._base_args(full), *LEAN_ARGS]
            for name in sorted(self._account_mcp_names()):
                args += ["--disable-mcp-server", name]
            spec = ProcessSpec(args=tuple(args), cwd=Path(scratch))
            text = ""
            async for event in self._process.stream(spec):
                if Json.text(event.get("type")) == "result":
                    premium = Json.obj(event.get("usage")).get("premiumRequests")
                    if isinstance(premium, (int, float)):
                        self.last_usage = TokenUsage(premium_requests=float(premium))
                if Json.text(event.get("type")) == "assistant.message":
                    text = Json.text(Json.obj(event.get("data")).get("content")) or text
        if not text:
            raise AgentError("copilot returned no message")
        return self.parse_json_object(text)

    @override
    async def run(self, request: RunRequest) -> AsyncIterator[AgentEvent]:
        """Stream a Copilot agent run in the request's working directory."""
        prompt = (
            f"{request.system}\n{self.instructions}\n\n---\n\n{request.prompt}"
            if request.system or self.instructions
            else request.prompt
        )
        args = self._base_args(prompt, request.model) + self._loadout_args()
        if request.mode is AgentMode.PLAN:
            args.append(f"--available-tools={PLAN_TOOLS}")
        else:
            args.append("--allow-all-tools")
        spec = ProcessSpec(args=tuple(args), cwd=request.cwd)
        final = ""
        session_id: str | None = None
        model = request.model or self.model
        async for event in self._process.stream(spec):
            kind = Json.text(event.get("type"))
            data = Json.obj(event.get("data"))
            session_id = (
                session_id
                or Json.text(data.get("sessionId"))
                or Json.text(event.get("sessionId"))
                or None
            )
            model = Json.text(data.get("model")) or model
            if kind == "assistant.turn_end":
                # The CLI reports no token counts, only the model and premium requests.
                yield AgentEvent(
                    AgentEventKind.USAGE, "", model=model, usage=TokenUsage()
                )
                continue
            if kind == "result":
                premium = Json.obj(event.get("usage")).get("premiumRequests")
                if isinstance(premium, (int, float)) and not isinstance(premium, bool):
                    yield AgentEvent(
                        AgentEventKind.USAGE,
                        "total",
                        model=model,
                        usage=TokenUsage(premium_requests=float(premium)),
                    )
                continue
            mapped = self._map(kind, data)
            if mapped is None:
                continue
            if mapped.kind is AgentEventKind.TEXT:
                final = mapped.text
            yield mapped
        yield AgentEvent(AgentEventKind.RESULT, final, session_id)

    def _loadout_args(self) -> list[str]:
        """Translate explicit MCP, plugin, and instruction selections to flags."""
        args: list[str] = []
        if self.loadout.mcp != "all":
            args.append("--disable-builtin-mcps")
            configured = (
                self.harness_config.config.mcp_servers if self.harness_config else {}
            )
            known = self._account_mcp_names() | set(configured)
            for name in sorted(known - set(self.loadout.mcp)):
                args += ["--disable-mcp-server", name]
            definitions = {
                name: configured[name]
                for name in self.loadout.mcp
                if configured.get(name)
            }
            if definitions:
                args += [
                    "--additional-mcp-config",
                    json.dumps({"mcpServers": definitions}),
                ]
        if not self.loadout.project_instructions:
            args.append("--no-custom-instructions")
        for plugin in self.loadout.plugins:
            args += ["--plugin-dir", plugin]
        return args

    @staticmethod
    def _account_mcp_names() -> set[str]:
        """Read only configured server names so lean excludes every account MCP."""
        root = Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")
        try:
            data = Json.obj(
                Json.loads((root / "mcp-config.json").read_text(encoding="utf-8"))
            )
        except FileNotFoundError:
            return set()
        except OSError as err:
            raise AgentError(
                "Cannot inspect Copilot MCP names for loadout isolation"
            ) from err
        return set(Json.obj(data.get("mcpServers")))

    @override
    async def discover(self) -> dict[str, list[str]]:
        """Collect capability names from Copilot's session initialization events."""
        found = await super().discover()
        with tempfile.TemporaryDirectory(prefix="jot-discover-") as scratch:
            spec = ProcessSpec(
                args=tuple(self._base_args("Reply OK.")), cwd=Path(scratch)
            )
            async for event in self._process.stream(spec):
                kind = Json.text(event.get("type"))
                data = Json.obj(event.get("data"))
                for target, event_kind, key in (
                    ("skills", "session.skills_loaded", "skills"),
                    ("mcp", "session.mcp_servers_loaded", "mcpServers"),
                ):
                    if kind == event_kind:
                        items = data.get(key, [])
                        if isinstance(items, list):
                            found[target] = [
                                Json.text(item) or Json.text(Json.obj(item).get("name"))
                                for item in items
                            ]
        return found

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
        if kind == "assistant.reasoning":
            thought = Json.text(data.get("content"))
            return AgentEvent(AgentEventKind.THINKING, thought) if thought else None
        if kind == "tool.execution_start":
            name = Json.text(data.get("toolName"))
            arguments = json.dumps(data.get("arguments"))[:200]
            return AgentEvent(AgentEventKind.TOOL, f"{name} {arguments}")
        if kind in ("session.error", "error"):
            return AgentEvent(AgentEventKind.ERROR, json.dumps(data)[:800])
        return None
