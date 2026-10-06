"""Local scratch paths and scripted provider boundaries."""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jot.agents.base import JsonObject
from jot.agents.process import JsonlProcess, ProcessSpec


class ProcessScript:
    """Capture CLI requests and stream configured JSON objects."""

    def __init__(self) -> None:
        self.events: list[JsonObject] = []
        self.specs: list[ProcessSpec] = []

    async def stream(self, spec: ProcessSpec) -> AsyncIterator[JsonObject]:
        """Record requests before yielding the scripted transcript."""
        self.specs.append(spec)
        for event in self.events:
            yield event

    @staticmethod
    def resolve(executable: str, hint: str) -> str:
        """Avoid looking up an installed provider CLI."""
        del hint
        return executable


@pytest.fixture(autouse=True)
def local_scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep provider temporary files inside the repository."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot"))


@pytest.fixture
def process_script(monkeypatch: pytest.MonkeyPatch) -> ProcessScript:
    """Replace CLI process boundaries with a recorded script."""
    script = ProcessScript()
    monkeypatch.setattr(JsonlProcess, "stream", script.stream)
    monkeypatch.setattr(JsonlProcess, "resolve", script.resolve)
    return script
