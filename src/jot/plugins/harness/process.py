"""Run an agent CLI and stream the JSON objects it prints, one per line."""

from __future__ import annotations

import locale
import shutil
import subprocess
import threading
from typing import TYPE_CHECKING

from pydantic import ValidationError

from jot.plugins.harness.api import JSON, HarnessError

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from jot.core.models import Metadata


class JsonlProcess:
    """One CLI invocation: resolve, feed stdin, yield stdout objects, check exit."""

    def __init__(self, command: list[str], hint: str) -> None:
        if not command:
            raise HarnessError("Empty command")
        self.command = command
        self.hint = hint

    def executable(self) -> str:
        """Resolve the program on PATH (or as given), or raise with a hint."""
        found = shutil.which(self.command[0])
        if found is None:
            raise HarnessError(f"{self.command[0]} not found on PATH; {self.hint}")
        return found

    def problem(self) -> str | None:
        """Return why the program cannot run, or None."""
        try:
            self.executable()
        except HarnessError as err:
            return str(err)
        return None

    def stream(
        self, args: list[str], cwd: Path, stdin: str | None = None
    ) -> Generator[Metadata]:
        """Yield each JSON object line; raise if the process exits non-zero."""
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [self.executable(), *self.command[1:], *args],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        errors: list[bytes] = []
        drain = threading.Thread(
            target=lambda: errors.append(
                process.stderr.read() if process.stderr else b""
            ),
            daemon=True,
        )
        drain.start()
        try:
            if process.stdin is not None:
                if stdin:
                    process.stdin.write(stdin.encode("utf-8"))
                process.stdin.close()
            assert process.stdout is not None  # noqa: S101 - piped above
            for raw in process.stdout:
                if (value := self.parse(raw)) is not None:
                    yield value
            code = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            drain.join(timeout=5)
        if code != 0:
            detail = self.decode(b"".join(errors)).strip()[-800:]
            raise HarnessError(f"{self.command[0]} exited {code}: {detail}")

    @classmethod
    def parse(cls, raw: bytes) -> Metadata | None:
        """Return a JSON object from one output line, or None."""
        line = cls.decode(raw).strip()
        if not line.startswith("{"):
            return None
        try:
            value = JSON.validate_json(line)
        except ValidationError:
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def decode(raw: bytes) -> str:
        """Decode UTF-8, falling back to the locale encoding some CLIs use."""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode(
                locale.getpreferredencoding(do_setlocale=False), errors="replace"
            )
