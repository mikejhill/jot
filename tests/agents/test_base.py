"""JSON contracts and backend selection."""

from __future__ import annotations

import asyncio

import pytest

from jot.agents.base import AgentBackend, AgentError, Json, JsonValue
from jot.agents.fake import FakeBackend
from jot.agents.registry import BackendRegistry


class TestAgentBackend:
    """Structured responses tolerate ordinary model formatting."""

    @pytest.mark.parametrize(
        "text", ['{"a": 1}', '```json\n{"a": 1}\n```', 'Here: {"a": 1} done']
    )
    def test_parse(self, text: str) -> None:
        """Objects survive fences and surrounding prose."""
        assert AgentBackend.parse_json_object(text) == {"a": 1}

    @pytest.mark.parametrize("text", ["garbage", "[]", "null", "{broken}"])
    def test_invalid(self, text: str) -> None:
        """Nonobjects fail with a useful domain error."""
        with pytest.raises(AgentError, match="JSON object"):
            AgentBackend.parse_json_object(text)

    @pytest.mark.parametrize("value", [None, 1, [], True, "text", {"x": 2}])
    def test_helpers(self, value: JsonValue) -> None:
        """Typed accessors reject values of the wrong shape."""
        assert Json.obj(value) == (value if isinstance(value, dict) else {})
        assert Json.text(value) == (value if isinstance(value, str) else "")
        assert Json.loads("broken") is None
        assert Json.loads('[1, "x"]') == [1, "x"]


class TestBackendRegistry:
    """Provider defaults are normalized without exposing the fake to users."""

    @pytest.mark.parametrize("model", [None, "", "default", "chosen"])
    def test_create(self, model: str | None) -> None:
        """Default aliases become None while explicit models survive."""
        backend = BackendRegistry.create("fake", model)
        assert isinstance(backend, FakeBackend)
        assert backend.model == ("chosen" if model == "chosen" else None)
        assert set(BackendRegistry.names()) == {"claude", "codex", "copilot"}
        assert asyncio.run(backend.structured("", "", {})) == FakeBackend.canned

    def test_unknown(self) -> None:
        """Unknown names include the valid alternatives."""
        with pytest.raises(AgentError, match=r"unknown agent backend.*choose one"):
            BackendRegistry.create("missing")
