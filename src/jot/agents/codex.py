"""Codex backend: drives the signed-in ``codex exec --json`` CLI (no API key)."""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
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

# Lean, tool-less settings for structured calls (measured in prompt-eval-atlas):
# cuts fixed per-call overhead from ~16k to ~5k input tokens.
DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "image_generation",
    "memories",
    "multi_agent",
    "goals",
    "hooks",
    "plugins",
    "remote_plugin",
    "shell_tool",
    "unified_exec",
    "view_image",
    "skill_search",
    "tool_suggest",
    "sleep_tool",
    "in_app_browser",
    "workspace_dependencies",
    "worktrees",
    "realtime_conversation",
)
LEAN_OVERRIDES = (
    "project_doc_max_bytes=0",
    "include_permissions_instructions=false",
    "include_environment_context=false",
    'web_search="disabled"',
)
SANDBOX_BY_MODE = {AgentMode.PLAN: "read-only", AgentMode.EXECUTE: "workspace-write"}


class CodexBackend(AgentBackend):
    """Agent backend backed by the Codex CLI on a ChatGPT plan."""

    name: ClassVar[str] = "codex"

    def __init__(self, model: str | None = None) -> None:
        super().__init__(model)
        self._process = JsonlProcess()

    @override
    async def structured(
        self, system: str, prompt: str, schema: JsonObject
    ) -> JsonObject:
        """Run one isolated, tool-less ``codex exec`` with an output schema."""
        codex = JsonlProcess.resolve("codex", "install Codex and sign in")
        with tempfile.TemporaryDirectory(prefix="jot-codex-") as scratch:
            root = Path(scratch)
            instructions = root / "instructions.md"
            instructions.write_text(
                (system or "Answer exactly.") + "\n" + self.instructions,
                encoding="utf-8",
            )
            schema_file = root / "schema.json"
            schema_file.write_text(json.dumps(schema), encoding="utf-8")
            args = [codex, "exec", "--json", "--ephemeral", "--ignore-user-config"]
            args += [
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
            ]
            args += ["--cd", str(root), "--output-schema", str(schema_file)]
            args += self._model_args()
            args += ["--enable", "skip_host_skill_discovery"]
            for feature in DISABLED_FEATURES:
                args += ["--disable", feature]
            overrides = [*LEAN_OVERRIDES]
            overrides.append(f"model_instructions_file='{instructions.as_posix()}'")
            for override in overrides:
                args += ["-c", override]
            args.append("-")
            spec = ProcessSpec(args=tuple(args), cwd=root, stdin=prompt)
            text = await self._final_text(spec)
        return self.parse_json_object(text)

    @override
    async def run(self, request: RunRequest) -> AsyncIterator[AgentEvent]:
        """Stream a full Codex agent run in the request's working directory."""
        codex = JsonlProcess.resolve("codex", "install Codex and sign in")
        args = [codex, "exec", "--json", "--skip-git-repo-check"]
        args += ["--sandbox", SANDBOX_BY_MODE[request.mode], "--cd", str(request.cwd)]
        args += self._model_args(request.model)
        args += self._loadout_args()
        args.append("-")
        prompt = (
            f"{request.system}\n{self.instructions}\n\n---\n\n{request.prompt}"
            if request.system or self.instructions
            else request.prompt
        )
        spec = ProcessSpec(args=tuple(args), cwd=request.cwd, stdin=prompt)
        session_id: str | None = None
        final = ""
        async for event in self._process.stream(spec):
            kind = Json.text(event.get("type"))
            if kind == "thread.started":
                session_id = Json.text(event.get("thread_id")) or None
                continue
            if kind in ("turn.failed", "error"):
                detail = json.dumps(event.get("error") or event)
                yield AgentEvent(
                    AgentEventKind.RESULT, detail, session_id, is_error=True
                )
                return
            if kind == "turn.completed":
                yield self._usage(Json.obj(event.get("usage")), request.model)
                continue
            if kind != "item.completed":
                continue
            mapped = self._map_item(Json.obj(event.get("item")))
            if mapped is None:
                continue
            if mapped.kind is AgentEventKind.TEXT:
                final = mapped.text
            yield mapped
        yield AgentEvent(AgentEventKind.RESULT, final, session_id)

    def _loadout_args(self) -> list[str]:
        """Disable account capabilities by default while preserving repo docs."""
        args: list[str] = [] if self.loadout.mcp == "all" else ["--ignore-user-config"]
        features = ["apps", "plugins", "remote_plugin", "skill_search", "memories"]
        if self.harness_config:
            features += self.harness_config.config.disable_features
        for feature in dict.fromkeys(features):
            args += ["--disable", feature]
        if not self.loadout.project_instructions:
            args += ["-c", "project_doc_max_bytes=0"]
        if self.loadout.skills != "all":
            args += ["--enable", "skip_host_skill_discovery"]
            args += [
                "-c",
                "skills.config="
                + "["
                + ", ".join(
                    "{path=" + json.dumps(path) + ",enabled=true}"
                    for path in self.loadout.skills
                )
                + "]",
            ]
        if self.harness_config and self.loadout.mcp != "all":
            for name in self.loadout.mcp:
                definition = self.harness_config.config.mcp_servers.get(name)
                if definition is None:
                    raise AgentError(f"MCP definition required for {name}")
                for key, value in definition.items():
                    args += [
                        "-c",
                        f"mcp_servers.{json.dumps(name)}.{key}={json.dumps(value)}",
                    ]
        return args

    def _model_args(self, override: str | None = None) -> list[str]:
        """Return ``--model`` arguments when a model is configured."""
        model = override or self.model
        return ["--model", model] if model else []

    def _map_item(self, item: JsonObject) -> AgentEvent | None:
        """Translate a completed Codex item into a facade event."""
        item_type = Json.text(item.get("type"))
        if item_type == "agent_message":
            return AgentEvent(AgentEventKind.TEXT, Json.text(item.get("text")))
        if item_type == "command_execution":
            return AgentEvent(
                AgentEventKind.TOOL, f"$ {Json.text(item.get('command'))}"
            )
        if item_type == "file_change":
            changes = item.get("changes")
            paths = (
                [Json.text(Json.obj(c).get("path")) for c in changes]
                if isinstance(changes, list)
                else []
            )
            return AgentEvent(AgentEventKind.TOOL, "edit " + ", ".join(paths))
        if item_type == "reasoning" and Json.text(item.get("text")).strip():
            return AgentEvent(AgentEventKind.THINKING, Json.text(item.get("text")))
        if item_type == "error":
            return AgentEvent(AgentEventKind.ERROR, Json.text(item.get("message")))
        return None

    def _usage(self, usage: JsonObject, override: str | None) -> AgentEvent:
        """Build a USAGE event; input is reported net of cached input, like Claude."""
        total_input = usage.get("input_tokens")
        cached = usage.get("cached_input_tokens")
        output = usage.get("output_tokens")
        uncached = (
            total_input - cached
            if isinstance(total_input, int) and isinstance(cached, int)
            else total_input
        )
        return AgentEvent(
            AgentEventKind.USAGE,
            "",
            model=override or self.model or self.default_model(),
            usage=TokenUsage(
                input=uncached if isinstance(uncached, int) else None,
                output=output if isinstance(output, int) else None,
                cache_read=cached if isinstance(cached, int) else None,
            ),
        )

    @classmethod
    def default_model(cls) -> str:
        """Return the model from the user's Codex config, if one is set."""
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        try:
            with (home / "config.toml").open("rb") as stream:
                model = tomllib.load(stream).get("model")
        except (OSError, tomllib.TOMLDecodeError):
            return "codex default"
        return model if isinstance(model, str) and model else "codex default"

    async def _final_text(self, spec: ProcessSpec) -> str:
        """Return the last agent message of a run, or raise on failure.

        Raises:
            AgentError: The run failed or produced no message.
        """
        text: str | None = None
        async for event in self._process.stream(spec):
            kind = Json.text(event.get("type"))
            item = Json.obj(event.get("item"))
            if kind == "item.completed" and item.get("type") == "agent_message":
                text = Json.text(item.get("text"))
            elif kind == "turn.completed":
                self.last_usage = (
                    self._usage(Json.obj(event.get("usage")), self.model).usage
                    or TokenUsage()
                )
            elif kind in ("turn.failed", "error"):
                raise AgentError(json.dumps(event.get("error") or event))
        if text is None:
            raise AgentError("codex returned no agent message")
        return text
