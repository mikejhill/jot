"""Backend registry: maps config names to agent backend classes."""

from __future__ import annotations

from typing import ClassVar

from jot.agents.base import AgentBackend, AgentError
from jot.agents.claude import ClaudeBackend
from jot.agents.codex import CodexBackend
from jot.agents.copilot import CopilotBackend
from jot.agents.fake import FakeBackend
from jot.config import Config
from jot.harnesses import KINDS

DEFAULT_MODEL = "default"
# Suggestions shown in the UI model picker; any model id the CLI accepts works.
MODEL_SUGGESTIONS: dict[str, list[str]] = {
    "claude": ["opus", "sonnet", "haiku"],
    "codex": ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna", "gpt-6-sol"],
    "copilot": ["auto", "claude-sonnet-4.5", "gpt-5"],
}


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
    def configured(
        cls,
        config: Config,
        action: str,
        harness: str | None = None,
        model: str | None = None,
    ) -> AgentBackend:
        """Create and configure a validated named runtime instance."""
        name, chosen = config.resolve(action, harness, model)
        settings = config.effective_harnesses()[name]
        backend = cls.create(KINDS[settings.kind], chosen)
        backend.configure(settings, action)
        return backend

    @classmethod
    async def discover(cls, config: Config, name: str) -> dict[str, list[str]]:
        """Discover an enabled instance even when it has no triage model default."""
        harness = config.effective_harnesses().get(name)
        if harness is None:
            raise AgentError(f"Unknown harness {name!r}")
        model = harness.models.default.get("triage", "default")
        if not harness.models.permits(model):
            model = next(
                (m for m in harness.models.allowed if harness.models.permits(m)),
                "default",
            )
        return await cls.configured(config, "triage", name, model).discover()

    @classmethod
    def names(cls) -> list[str]:
        """Return user-selectable backend names (excludes the test fake)."""
        return [name for name in cls.backends if name != FakeBackend.name]
