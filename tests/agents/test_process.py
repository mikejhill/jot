"""Real JSONL subprocess IO and cancellation without provider installations."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from jot.agents.base import AgentError, JsonObject
from jot.agents.process import JsonlProcess, ProcessSpec


class TestJsonlProcess:
    """A tiny Python subprocess exercises the actual pipe lifecycle."""

    @staticmethod
    async def collect(spec: ProcessSpec) -> list[JsonObject]:
        """Drain a subprocess into a concrete transcript."""
        return [event async for event in JsonlProcess().stream(spec)]

    def test_stream(self, tmp_path: Path) -> None:
        """Stdin reaches the child and noise is omitted from JSON events."""
        code = (
            "import sys,json; s=sys.stdin.read(); "
            'sys.stdout.buffer.write(b"noise\\n{bad}\\n\\xff\\n"); '
            'print(json.dumps({"input":s})); print("[]")'
        )
        spec = ProcessSpec((sys.executable, "-c", code), tmp_path, "hello")
        assert asyncio.run(self.collect(spec)) == [{"input": "hello"}]

    def test_failure(self, tmp_path: Path) -> None:
        """Nonzero exits include stderr diagnostics."""
        code = 'import sys; sys.stderr.write("broken"); sys.exit(7)'
        with pytest.raises(AgentError, match="exited 7: broken"):
            asyncio.run(
                self.collect(ProcessSpec((sys.executable, "-c", code), tmp_path))
            )

    def test_resolve(self) -> None:
        """Executable resolution returns real paths or actionable errors."""
        assert JsonlProcess.resolve(sys.executable, "unused") == sys.executable
        with pytest.raises(AgentError, match=r"CLI not found.*install it"):
            JsonlProcess.resolve("jot-test-nonexistent-executable", "install it")

    def test_cancel(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cancelling a blocked reader kills and reaps its real child."""
        original = asyncio.create_subprocess_exec
        children: list[asyncio.subprocess.Process] = []

        async def spawn(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
            """Capture the child while retaining actual process IO."""
            del kwargs
            child = await original(
                *args,
                cwd=tmp_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            children.append(child)
            return child

        async def scenario() -> None:
            """Wait for the child's ready event before cancelling its next read."""
            code = 'import time; print("{}", flush=True); time.sleep(60)'
            stream = JsonlProcess().stream(
                ProcessSpec((sys.executable, "-c", code), tmp_path)
            )
            assert await anext(stream) == {}
            pending = asyncio.ensure_future(anext(stream))
            await asyncio.sleep(0)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            await stream.aclose()
            assert children[0].returncode is not None

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        asyncio.run(scenario())
