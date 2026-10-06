"""Backend registry: maps config names to agent backend classes."""

from __future__ import annotations

from typing import ClassVar

from jot.agents.base import AgentBackend, AgentError
from jot.agents.claude import ClaudeBackend
from jot.agents.codex import CodexBackend
from jot.agents.copilot import CopilotBackend
from jot.agents.fake import FakeBackend

DEFAULT_MODEL = "default"


class BackendRegistry:
    """Create backends by name; adding a provider = one class + one entry here."""

    backends: ClassVar[dict[str, type[AgentBackend]]] = {
        backend.name: backend
        for backend in (ClaudeBackend, CodexBackend, CopilotBackend, FakeBackend)
    }

    @classmethod
    def create(cls, name: str, model: str | None = None) -> AgentBackend:
        """Instantiate backend ``name``; ``"default"`` model means provider default.

        Raises:
            AgentError: Unknown backend name.
        """
        backend = cls.backends.get(name)
        if backend is None:
            known = ", ".join(sorted(cls.backends))
            raise AgentError(f"unknown agent backend {name!r}; choose one of {known}")
        return backend(None if model in (None, "", DEFAULT_MODEL) else model)

    @classmethod
    def names(cls) -> list[str]:
        """Return user-selectable backend names (excludes the test fake)."""
        return [name for name in cls.backends if name != FakeBackend.name]
