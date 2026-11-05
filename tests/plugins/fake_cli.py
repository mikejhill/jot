"""A stand-in for the claude, codex, and copilot CLIs, driven by environment.

Usage: ``python fake_cli.py <flavor> [args...]``. It appends what it was called
with to ``$FAKE_CLI_LOG`` (one JSON object per call) and prints that CLI's
JSON-lines output. ``$FAKE_CLI_MODE`` picks a scenario: ``ok`` (default),
``prose`` (JSON inside prose), ``error`` (the CLI reports a failure),
``empty`` (no result), ``crash`` (exit 3 with stderr), ``missing`` (the answer
lacks required keys).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path


class FakeCli:
    """Emit one flavor's event stream."""

    def __init__(self, flavor: str, argv: list[str]) -> None:
        self.flavor = flavor
        self.argv = argv
        self.mode = os.environ.get("FAKE_CLI_MODE", "ok")
        self.stdin = "" if sys.stdin.isatty() else sys.stdin.read()

    def after(self, flag: str) -> str | None:
        """Return the argument following ``flag``, if present."""
        if flag in self.argv:
            index = self.argv.index(flag)
            if index + 1 < len(self.argv):
                return self.argv[index + 1]
        return None

    def log(self) -> None:
        """Record argv, cwd, stdin, and any schema file for the test."""
        record: dict[str, object] = {
            "argv": self.argv,
            "cwd": str(Path.cwd()),
            "stdin": self.stdin,
        }
        if schema := self.after("--output-schema"):
            record["schema_file"] = json.loads(Path(schema).read_text("utf-8"))
        if path := os.environ.get("FAKE_CLI_LOG"):
            with Path(path).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")

    @staticmethod
    def emit(*events: Mapping[str, object]) -> None:
        """Print events as JSON lines, with noise a parser must skip."""
        sys.stdout.write("starting up...\n[1, 2]\n{not json\n")
        for event in events:
            sys.stdout.write(json.dumps(event) + "\n")

    def answer(self) -> str:
        """The structured answer text for this mode."""
        if self.mode == "prose":
            return 'Sure:\n```json\n{"ok": true}\n```\nDone.'
        if self.mode == "missing":
            return '{"other": 1}'
        return '{"ok": true}'

    def main(self) -> int:
        """Dispatch on flavor and mode."""
        self.log()
        if self.mode == "crash":
            sys.stderr.write("fatal: not signed in\n")
            return 3
        getattr(self, self.flavor)()
        return 0

    def claude(self) -> None:
        """Claude Code stream-json."""
        session = self.after("--resume") or "claude-session"
        init = {"type": "system", "subtype": "init", "model": "claude-test"}
        init["session_id"] = session
        usage = {
            "input_tokens": 11,
            "output_tokens": 7,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 2,
        }
        if self.mode == "empty":
            self.emit(init)
            return
        result: dict[str, object] = {
            "type": "result",
            "is_error": self.mode == "error",
            "result": "boom" if self.mode == "error" else "All done",
            "session_id": session,
            "usage": usage,
            "total_cost_usd": 0.0125,
        }
        if "--json-schema" in self.argv:
            result["result"] = "boom" if self.mode == "error" else self.answer()
            if self.mode == "ok":
                result["structured_output"] = {"ok": True}
            self.emit(init, result)
            return
        content = [
            {"type": "thinking", "thinking": "Let me look"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}},
            {"type": "tool_use", "name": "TodoWrite", "input": {"todos": []}},
            {"type": "text", "text": "All done"},
            {"type": "text", "text": "   "},
        ]
        assistant = {"type": "assistant", "message": {"content": content}}
        self.emit(init, assistant, result)

    def codex(self) -> None:
        """Codex exec --json."""
        session = self.after("resume") or "codex-thread"
        started = {"type": "thread.started", "thread_id": session}
        if self.mode == "error":
            self.emit(started, {"type": "turn.failed", "error": {"message": "quota"}})
            return
        if self.mode == "empty":
            self.emit(started, {"type": "turn.completed", "usage": {}})
            return
        message = self.answer() if "--output-schema" in self.argv else "All done"
        items = [
            {"type": "reasoning", "text": "Let me look"},
            {"type": "reasoning", "text": " "},
            {"type": "command_execution", "command": "ls"},
            {"type": "file_change", "changes": [{"path": "a.py"}, {"path": "b.py"}]},
            {"type": "error", "message": "minor"},
            {"type": "todo_list"},
            {"type": "agent_message", "text": message},
        ]
        usage = {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 9}
        self.emit(
            started,
            {"type": "turn.started"},
            *({"type": "item.completed", "item": item} for item in items),
            {"type": "turn.completed", "usage": usage},
        )

    def copilot(self) -> None:
        """Copilot --output-format json."""
        session = self.after("--resume") or "copilot-session"
        start = {"type": "session.start", "data": {"sessionId": session}}
        model = {"type": "session.model", "data": {"model": "gpt-test"}}
        result = {"type": "result", "usage": {"premiumRequests": 1}}
        if self.mode == "empty":
            self.emit(start, result)
            return
        if self.mode == "error":
            error = {"type": "session.error", "data": {"message": "quota"}}
            self.emit(start, error, result)
            return
        prompt = self.after("-p") or ""
        text = self.answer() if "Respond with ONLY" in prompt else "All done"
        self.emit(
            start,
            model,
            {"type": "assistant.reasoning", "data": {"content": "Let me look"}},
            {
                "type": "tool.execution_start",
                "data": {"toolName": "view", "arguments": {"path": "a.py"}},
            },
            {"type": "assistant.message", "data": {"content": ""}},
            {"type": "assistant.message", "data": {"content": text}},
            result,
        )


if __name__ == "__main__":
    sys.exit(FakeCli(sys.argv[1], sys.argv[2:]).main())
