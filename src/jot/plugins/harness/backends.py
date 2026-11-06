"""Harness implementations: Claude Code, Codex, Copilot CLIs and an offline fake.

Each CLI harness runs the user's signed-in command-line agent (no API keys in
Jot) and maps the three permission levels to that CLI's own flags. The
mappings are listed by ``jot harness ls`` so they can be compared.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, override

from pydantic import Field, JsonValue

from jot.core.models import Metadata, Record
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
from jot.plugins.harness.process import JsonlProcess

if TYPE_CHECKING:
    from collections.abc import Iterator

TOOL_DETAIL_KEYS = ("command", "file_path", "pattern", "path", "url", "query")


class CliSettings(Record):
    """Where the CLI is and how to call it."""

    command: list[str] = Field(
        default_factory=list, description="Program and fixed args"
    )
    model: str | None = None
    extra_args: list[str] = Field(default_factory=list)


class CliHarness(Harness):
    """Shared plumbing for harnesses that drive a JSON-lines CLI."""

    program: ClassVar[str] = ""
    hint: ClassVar[str] = ""

    def __init__(self, settings: CliSettings) -> None:
        self.settings = settings
        self.process = JsonlProcess(settings.command or [self.program], self.hint)

    @override
    def problem(self) -> str | None:
        """Report a missing CLI."""
        return self.process.problem()

    def model_args(self, model: str | None) -> list[str]:
        """Return ``--model`` when one is chosen."""
        chosen = model or self.settings.model
        return ["--model", chosen] if chosen else []

    @staticmethod
    def joined(system: str, prompt: str) -> str:
        """Prefix a system text to the prompt for CLIs without a system flag."""
        return f"{system}\n\n---\n\n{prompt}" if system else prompt

    @staticmethod
    def schema_prompt(request: StructuredRequest) -> str:
        """Ask for exactly one JSON object matching the schema."""
        return (
            f"{request.prompt}\n\nRespond with ONLY one JSON object (no prose, "
            "no code fences) matching this JSON Schema:\n"
            f"{json.dumps(request.output_schema)}"
        )


class ClaudeHarness(CliHarness):
    """Claude Code (``claude -p --output-format stream-json``)."""

    id = "claude"
    label = "Claude Code"
    program = "claude"
    hint = "install Claude Code and sign in"
    READ_TOOLS: ClassVar[str] = "Read,Grep,Glob,WebSearch,WebFetch"
    WRITE_TOOLS: ClassVar[str] = (
        "Read,Grep,Glob,WebSearch,WebFetch,Edit,Write,MultiEdit"
    )
    LEAN: ClassVar[list[str]] = [
        "--no-session-persistence",
        "--strict-mcp-config",
        "--disable-slash-commands",
    ]

    @override
    def capabilities(self) -> Capabilities:
        """Everything, with measured usage and cost."""
        return Capabilities(resume=True)

    @override
    def permission_args(self, permission: Permission) -> list[str]:
        """Allow-list tools in dontAsk mode; bypass only for full access."""
        return {
            Permission.READ_ONLY: [
                "--permission-mode",
                "dontAsk",
                "--allowedTools",
                self.READ_TOOLS,
            ],
            Permission.WORKSPACE_WRITE: [
                "--permission-mode",
                "dontAsk",
                "--allowedTools",
                self.WRITE_TOOLS,
            ],
            Permission.FULL: ["--permission-mode", "bypassPermissions"],
        }[permission]

    def base(self, model: str | None) -> list[str]:
        """Shared flags for every call."""
        return [
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            *self.model_args(model),
            *self.settings.extra_args,
        ]

    @override
    def structured(self, request: StructuredRequest) -> StructuredResult:
        """A tool-less, schema-constrained call."""
        args = [
            *self.base(request.model),
            *self.LEAN,
            "--tools",
            "",
            "--json-schema",
            json.dumps(request.output_schema),
        ]
        if request.system:
            args += ["--system-prompt", request.system]
        with tempfile.TemporaryDirectory(prefix="jot-claude-") as scratch:
            result: Metadata | None = None
            model = None
            for event in self.process.stream(args, Path(scratch), request.prompt):
                if event.get("type") == "system":
                    model = text_of(event.get("model")) or model
                if event.get("type") == "result":
                    result = event
        if result is None:
            raise HarnessError("claude returned no result")
        if result.get("is_error"):
            raise HarnessError(f"claude failed: {text_of(result.get('result'))}")
        native = result.get("structured_output")
        value = native if isinstance(native, dict) else None
        value = value or self.parse_object(text_of(result.get("result")))
        return StructuredResult(
            value=self.check_required(value, request.output_schema),
            usage=self.usage(result),
            model=model,
        )

    @override
    def run(self, request: RunRequest) -> Iterator[HarnessEvent]:
        """Stream a Claude Code session."""
        self.check_permission(request.permission)
        args = [*self.base(request.model), *self.permission_args(request.permission)]
        if request.system:
            args += ["--append-system-prompt", request.system]
        if request.session_id:
            args += ["--resume", request.session_id]
        session, model = request.session_id, request.model or self.settings.model
        for event in self.process.stream(args, workdir(request.cwd), request.prompt):
            kind = event.get("type")
            session = text_of(event.get("session_id")) or session
            if kind == "system":
                model = text_of(event.get("model")) or model
            elif kind == "assistant":
                yield from self.blocks(obj(event.get("message")))
            elif kind == "result":
                yield HarnessEvent(
                    kind=EventKind.USAGE, model=model, usage=self.usage(event)
                )
                yield HarnessEvent(
                    kind=EventKind.RESULT,
                    text=text_of(event.get("result")),
                    session_id=session,
                    model=model,
                    is_error=bool(event.get("is_error")),
                )
                return
        yield HarnessEvent(
            kind=EventKind.RESULT, text="claude ended without a result", is_error=True
        )

    @staticmethod
    def blocks(message: Metadata) -> Iterator[HarnessEvent]:
        """Translate assistant content blocks."""
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            item = obj(block)
            kind = item.get("type")
            if kind == "text" and text_of(item.get("text")).strip():
                yield HarnessEvent(kind=EventKind.TEXT, text=text_of(item.get("text")))
            elif kind == "thinking" and text_of(item.get("thinking")).strip():
                yield HarnessEvent(
                    kind=EventKind.THINKING, text=text_of(item.get("thinking"))
                )
            elif kind == "tool_use":
                arguments = obj(item.get("input"))
                detail = next(
                    (
                        text_of(arguments.get(key))[:200]
                        for key in TOOL_DETAIL_KEYS
                        if text_of(arguments.get(key))
                    ),
                    "",
                )
                name = text_of(item.get("name"))
                yield HarnessEvent(kind=EventKind.TOOL, text=f"{name} {detail}".strip())

    @staticmethod
    def usage(result: Metadata) -> Usage:
        """Map the result's usage and cost."""
        usage = obj(result.get("usage"))
        return Usage(
            input=integer(usage.get("input_tokens")),
            output=integer(usage.get("output_tokens")),
            cache_read=integer(usage.get("cache_read_input_tokens")),
            cache_write=integer(usage.get("cache_creation_input_tokens")),
            cost_usd=number(result.get("total_cost_usd")),
        )


class CodexHarness(CliHarness):
    """OpenAI Codex (``codex exec --json``)."""

    id = "codex"
    label = "Codex"
    program = "codex"
    hint = "install Codex and sign in"
    # Tool-less structured calls disable features that add prompt overhead.
    LEAN: ClassVar[list[str]] = [
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        'web_search="disabled"',
        "--disable",
        "shell_tool",
        "--disable",
        "plugins",
        "--disable",
        "memories",
    ]

    @override
    def capabilities(self) -> Capabilities:
        """Everything; tokens are measured, cost is not reported."""
        return Capabilities(resume=True, usage="partial")

    @override
    def permission_args(self, permission: Permission) -> list[str]:
        """Codex's own sandbox modes."""
        return {
            Permission.READ_ONLY: ["--sandbox", "read-only"],
            Permission.WORKSPACE_WRITE: ["--sandbox", "workspace-write"],
            Permission.FULL: ["--dangerously-bypass-approvals-and-sandbox"],
        }[permission]

    @override
    def structured(self, request: StructuredRequest) -> StructuredResult:
        """One isolated, read-only exec with an output schema file."""
        with tempfile.TemporaryDirectory(prefix="jot-codex-") as scratch:
            root = Path(scratch)
            schema = root / "schema.json"
            schema.write_text(json.dumps(request.output_schema), encoding="utf-8")
            args = [
                "exec",
                "--json",
                "--skip-git-repo-check",
                *self.permission_args(Permission.READ_ONLY),
                "--cd",
                str(root),
                "--output-schema",
                str(schema),
                *self.LEAN,
                *self.model_args(request.model),
                *self.settings.extra_args,
                "-",
            ]
            text, usage = None, Usage()
            prompt = self.joined(request.system, request.prompt)
            for event in self.process.stream(args, root, prompt):
                kind = event.get("type")
                item = obj(event.get("item"))
                if kind == "item.completed" and item.get("type") == "agent_message":
                    text = text_of(item.get("text"))
                elif kind == "turn.completed":
                    usage = self.usage(obj(event.get("usage")))
                elif kind in {"turn.failed", "error"}:
                    raise HarnessError(f"codex failed: {json.dumps(event)[:800]}")
        if text is None:
            raise HarnessError("codex returned no message")
        value = self.parse_object(text)
        return StructuredResult(
            value=self.check_required(value, request.output_schema),
            usage=usage,
            model=request.model or self.settings.model,
        )

    @override
    def run(self, request: RunRequest) -> Iterator[HarnessEvent]:
        """Stream a Codex session."""
        self.check_permission(request.permission)
        cwd = workdir(request.cwd)
        args = [
            "exec",
            "--json",
            "--skip-git-repo-check",
            *self.permission_args(request.permission),
            "--cd",
            str(cwd),
            *self.model_args(request.model),
            *self.settings.extra_args,
        ]
        if request.session_id:
            args += ["resume", request.session_id]
        args.append("-")
        session, final = request.session_id, ""
        model = request.model or self.settings.model
        prompt = self.joined(request.system, request.prompt)
        for event in self.process.stream(args, cwd, prompt):
            kind = event.get("type")
            if kind == "thread.started":
                session = text_of(event.get("thread_id")) or session
            elif kind in {"turn.failed", "error"}:
                yield HarnessEvent(
                    kind=EventKind.RESULT,
                    text=json.dumps(event.get("error") or event)[:800],
                    session_id=session,
                    is_error=True,
                )
                return
            elif kind == "turn.completed":
                yield HarnessEvent(
                    kind=EventKind.USAGE,
                    model=model,
                    usage=self.usage(obj(event.get("usage"))),
                )
            elif kind == "item.completed":
                mapped = self.item(obj(event.get("item")))
                if mapped is not None:
                    final = mapped.text if mapped.kind == EventKind.TEXT else final
                    yield mapped
        yield HarnessEvent(
            kind=EventKind.RESULT, text=final, session_id=session, model=model
        )

    @staticmethod
    def item(item: Metadata) -> HarnessEvent | None:
        """Translate a completed item."""
        kind = item.get("type")
        if kind == "agent_message":
            return HarnessEvent(kind=EventKind.TEXT, text=text_of(item.get("text")))
        if kind == "command_execution":
            return HarnessEvent(
                kind=EventKind.TOOL, text=f"$ {text_of(item.get('command'))}"
            )
        if kind == "file_change":
            changes = item.get("changes")
            paths = [
                text_of(obj(change).get("path"))
                for change in (changes if isinstance(changes, list) else [])
            ]
            return HarnessEvent(kind=EventKind.TOOL, text="edit " + ", ".join(paths))
        if kind == "reasoning" and text_of(item.get("text")).strip():
            return HarnessEvent(kind=EventKind.THINKING, text=text_of(item.get("text")))
        if kind == "error":
            return HarnessEvent(kind=EventKind.ERROR, text=text_of(item.get("message")))
        return None

    @staticmethod
    def usage(usage: Metadata) -> Usage:
        """Input is reported net of cached input."""
        total = integer(usage.get("input_tokens"))
        cached = integer(usage.get("cached_input_tokens"))
        net = total - cached if total is not None and cached is not None else total
        return Usage(
            input=net, output=integer(usage.get("output_tokens")), cache_read=cached
        )


class CopilotHarness(CliHarness):
    """GitHub Copilot CLI (``copilot -p --output-format json``)."""

    id = "copilot"
    label = "GitHub Copilot"
    program = "copilot"
    hint = "install GitHub Copilot CLI and log in"
    READ_TOOLS: ClassVar[str] = "view,glob,grep,web_fetch"
    WRITE_TOOLS: ClassVar[str] = "view,glob,grep,web_fetch,edit,create"
    LEAN: ClassVar[list[str]] = [
        "--available-tools=view",
        "--disable-builtin-mcps",
        "--no-custom-instructions",
    ]

    @override
    def capabilities(self) -> Capabilities:
        """Everything; only premium requests are reported, tokens are estimated."""
        return Capabilities(resume=True, usage="estimated")

    @override
    def permission_args(self, permission: Permission) -> list[str]:
        """Restrict the tool list; allow tools to run without asking."""
        return {
            Permission.READ_ONLY: [f"--available-tools={self.READ_TOOLS}"],
            Permission.WORKSPACE_WRITE: [
                f"--available-tools={self.WRITE_TOOLS}",
                "--allow-all-tools",
            ],
            Permission.FULL: ["--allow-all-tools"],
        }[permission]

    def base(self, prompt: str, model: str | None) -> list[str]:
        """Shared flags for every call."""
        return [
            "-p",
            prompt,
            "--output-format",
            "json",
            "--no-ask-user",
            *self.model_args(model),
            *self.settings.extra_args,
        ]

    @override
    def structured(self, request: StructuredRequest) -> StructuredResult:
        """Ask for JSON with only a harmless tool available."""
        prompt = self.joined(request.system, self.schema_prompt(request))
        text, premium = "", None
        with tempfile.TemporaryDirectory(prefix="jot-copilot-") as scratch:
            args = [*self.base(prompt, request.model), *self.LEAN]
            for event in self.process.stream(args, Path(scratch)):
                kind = event.get("type")
                if kind == "assistant.message":
                    text = text_of(obj(event.get("data")).get("content")) or text
                elif kind == "result":
                    premium = number(obj(event.get("usage")).get("premiumRequests"))
        if not text:
            raise HarnessError("copilot returned no message")
        usage = Usage.estimate(prompt, text).model_copy(
            update={"premium_requests": premium}
        )
        return StructuredResult(
            value=self.check_required(self.parse_object(text), request.output_schema),
            usage=usage,
            model=request.model or self.settings.model,
        )

    @override
    def run(self, request: RunRequest) -> Iterator[HarnessEvent]:
        """Stream a Copilot session."""
        self.check_permission(request.permission)
        prompt = self.joined(request.system, request.prompt)
        args = [
            *self.base(prompt, request.model),
            *self.permission_args(request.permission),
        ]
        if request.session_id:
            args += ["--resume", request.session_id]
        session, final, failed = request.session_id, "", False
        model = request.model or self.settings.model
        for event in self.process.stream(args, workdir(request.cwd)):
            kind = text_of(event.get("type"))
            data = obj(event.get("data"))
            session = text_of(data.get("sessionId")) or session
            model = text_of(data.get("model")) or model
            if kind == "result":
                premium = number(obj(event.get("usage")).get("premiumRequests"))
                usage = Usage.estimate(prompt, final).model_copy(
                    update={"premium_requests": premium}
                )
                yield HarnessEvent(kind=EventKind.USAGE, model=model, usage=usage)
                continue
            mapped = self.event(kind, data)
            if mapped is not None:
                final = mapped.text if mapped.kind == EventKind.TEXT else final
                failed = failed or mapped.kind == EventKind.ERROR
                yield mapped
        yield HarnessEvent(
            kind=EventKind.RESULT,
            text=final,
            session_id=session,
            model=model,
            is_error=failed,
        )

    @staticmethod
    def event(kind: str, data: Metadata) -> HarnessEvent | None:
        """Translate one Copilot event."""
        content = text_of(data.get("content"))
        if kind == "assistant.message" and content.strip():
            return HarnessEvent(kind=EventKind.TEXT, text=content)
        if kind == "assistant.reasoning" and content:
            return HarnessEvent(kind=EventKind.THINKING, text=content)
        if kind == "tool.execution_start":
            arguments = json.dumps(data.get("arguments"))[:200]
            name = text_of(data.get("toolName"))
            return HarnessEvent(kind=EventKind.TOOL, text=f"{name} {arguments}")
        if kind in {"session.error", "error"}:
            return HarnessEvent(kind=EventKind.ERROR, text=json.dumps(data)[:800])
        return None


class FakeSettings(Record):
    """Scripted behavior for tests and demos."""

    canned: Metadata = Field(default_factory=dict)
    reply: str = "Done."
    fail: bool = False
    files: dict[str, str] = Field(
        default_factory=dict,
        description="Files to write in the working directory on writable runs",
    )


class FakeHarness(Harness):
    """Offline and deterministic: answers from the schema and settings."""

    id = "fake"
    label = "Fake (offline)"

    def __init__(self, settings: FakeSettings) -> None:
        self.settings = settings

    @override
    def capabilities(self) -> Capabilities:
        """Everything, with fixed measured usage."""
        return Capabilities(resume=True)

    @override
    def permission_args(self, permission: Permission) -> list[str]:
        """Pretend flags so the mapping is visible."""
        return [f"--fake-permission={permission.value}"]

    @override
    def structured(self, request: StructuredRequest) -> StructuredResult:
        """Return a minimal instance of the schema overlaid with canned values."""
        if self.settings.fail:
            raise HarnessError("fake harness configured to fail")
        value = obj(minimal(request.output_schema)) | self.settings.canned
        return StructuredResult(
            value=self.check_required(value, request.output_schema),
            usage=Usage(input=10, output=5),
            model=request.model or "fake-model",
        )

    @override
    def run(self, request: RunRequest) -> Iterator[HarnessEvent]:
        """Emit a tool step, the reply, usage, and a result."""
        self.check_permission(request.permission)
        workdir(request.cwd)
        if self.settings.fail:
            yield HarnessEvent(kind=EventKind.ERROR, text="configured to fail")
            yield HarnessEvent(kind=EventKind.RESULT, text="failed", is_error=True)
            return
        model = request.model or "fake-model"
        session = request.session_id or "fake-session"
        yield HarnessEvent(kind=EventKind.TOOL, text=f"Read {request.cwd}")
        if request.permission != Permission.READ_ONLY:
            for name, content in self.settings.files.items():
                target = workdir(request.cwd) / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                yield HarnessEvent(kind=EventKind.TOOL, text=f"Write {name}")
        yield HarnessEvent(kind=EventKind.TEXT, text=self.settings.reply)
        yield HarnessEvent(
            kind=EventKind.USAGE, model=model, usage=Usage(input=10, output=5)
        )
        yield HarnessEvent(
            kind=EventKind.RESULT,
            text=self.settings.reply,
            session_id=session,
            model=model,
        )


def minimal(schema: Metadata) -> JsonValue:
    """Build the smallest value that satisfies common JSON Schema shapes."""
    if "const" in schema:
        return schema["const"]
    if "default" in schema:
        return schema["default"]
    options = schema.get("enum")
    if isinstance(options, list) and options:
        return options[0]
    kind = schema.get("type")
    kind = kind[0] if isinstance(kind, list) and kind else kind
    if kind == "object" or "properties" in schema:
        properties = obj(schema.get("properties"))
        required = schema.get("required")
        names = required if isinstance(required, list) else []
        return {
            name: minimal(obj(properties.get(name)))
            for name in names
            if isinstance(name, str)
        }
    blanks: dict[str, JsonValue] = {
        "array": [],
        "string": "",
        "integer": 0,
        "number": 0,
        "boolean": False,
    }
    return blanks.get(kind if isinstance(kind, str) else "")
