"""Async subprocess runner that streams JSON Lines from agent CLIs."""

from __future__ import annotations

import asyncio
import locale
import logging
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jot.agents.base import AgentError, Json, JsonObject

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator
    from pathlib import Path

logger = logging.getLogger(__name__)

STREAM_LIMIT = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    """A command line, its working directory, and optional stdin text."""

    args: tuple[str, ...]
    cwd: Path
    stdin: str | None = None


class JsonlProcess:
    """Run a CLI, yield each stdout JSON object, and fail loudly on bad exits."""

    @classmethod
    def resolve(cls, executable: str, hint: str) -> str:
        """Find ``executable`` on PATH.

        Raises:
            AgentError: The executable is not installed.
        """
        found = shutil.which(executable)
        if found is None:
            raise AgentError(f"{executable} CLI not found on PATH; {hint}")
        return found

    async def stream(self, spec: ProcessSpec) -> AsyncGenerator[JsonObject]:
        """Yield parsed JSON objects from stdout; kill the process if cancelled.

        Raises:
            AgentError: The process exited non-zero.
        """
        logger.debug("spawning %s in %s", spec.args[0], spec.cwd)
        process = await asyncio.create_subprocess_exec(
            *spec.args,
            cwd=spec.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
        )
        stderr_task = asyncio.create_task(self._drain(process.stderr))
        try:
            await self._feed(process, spec.stdin)
            async for event in self._lines(process.stdout):
                yield event
            code = await process.wait()
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        stderr = await stderr_task
        if code != 0:
            raise AgentError(f"{spec.args[0]} exited {code}: {stderr[-800:]}")

    async def _feed(
        self, process: asyncio.subprocess.Process, text: str | None
    ) -> None:
        """Write optional stdin text and close the pipe."""
        if process.stdin is None:
            return
        if text:
            process.stdin.write(text.encode("utf-8"))
            await process.stdin.drain()
        process.stdin.close()

    async def _lines(
        self, stdout: asyncio.StreamReader | None
    ) -> AsyncIterator[JsonObject]:
        """Yield JSON objects, skipping blank and non-JSON lines."""
        if stdout is None:
            return
        async for raw in stdout:
            line = self._decode(raw).strip()
            if not line.startswith("{"):
                continue
            value = Json.loads(line)
            if isinstance(value, dict):
                yield value

    def _decode(self, raw: bytes) -> str:
        """Decode UTF-8, falling back to the Windows ANSI code page some CLIs use."""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode(
                locale.getpreferredencoding(do_setlocale=False), errors="replace"
            )

    async def _drain(self, stream: asyncio.StreamReader | None) -> str:
        """Collect a stream to text so the pipe never blocks."""
        if stream is None:
            return ""
        data = await stream.read()
        return data.decode("utf-8", errors="replace")
