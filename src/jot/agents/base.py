"""Provider-neutral agent facade: the contract every backend implements."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

from pydantic import TypeAdapter, ValidationError

from jot.exceptions import AppError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

type JsonValue = (
    str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
)
type JsonObject = dict[str, JsonValue]

JSON_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
FENCE_PATTERN = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


class AgentError(AppError):
    """An agent backend failed or returned unusable output."""


class AgentMode(StrEnum):
    """How much an agent run may change: read-only planning or full execution."""

    PLAN = "plan"
    EXECUTE = "execute"


class AgentEventKind(StrEnum):
    """Kinds of streamed events a run produces."""

    TEXT = "text"
    THINKING = "thinking"
    TOOL = "tool"
    USAGE = "usage"
    ERROR = "error"
    RESULT = "result"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token counts for one model turn; None means the provider doesn't report it."""

    input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    premium_requests: float | None = None

    def plus(self, other: TokenUsage) -> TokenUsage:
        """Return the field-wise sum, keeping None only where both are None."""

        def add[T: (int, float)](a: T | None, b: T | None) -> T | None:
            if a is None:
                return b
            return a if b is None else a + b

        return TokenUsage(
            add(self.input, other.input),
            add(self.output, other.output),
            add(self.cache_read, other.cache_read),
            add(self.cache_write, other.cache_write),
            add(self.premium_requests, other.premium_requests),
        )

    def prefer(self, reported: TokenUsage) -> TokenUsage:
        """Return ``reported`` values where present, else this (summed) usage."""
        return TokenUsage(
            reported.input if reported.input is not None else self.input,
            reported.output if reported.output is not None else self.output,
            reported.cache_read if reported.cache_read is not None else self.cache_read,
            reported.cache_write
            if reported.cache_write is not None
            else self.cache_write,
            reported.premium_requests
            if reported.premium_requests is not None
            else self.premium_requests,
        )

    def as_json(self) -> JsonObject:
        """Return a JSON object with the reported fields."""
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "premium_requests": self.premium_requests,
        }


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One streamed step of an agent run; the final event has kind RESULT."""

    kind: AgentEventKind
    text: str
    session_id: str | None = None
    cost_usd: float | None = None
    is_error: bool = False
    model: str | None = None
    usage: TokenUsage | None = None


@dataclass(frozen=True, slots=True)
class RunRequest:
    """Everything a backend needs to run an agent in a working directory."""

    prompt: str
    cwd: Path
    mode: AgentMode
    system: str = ""
    model: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


class AgentBackend(ABC):
    """Swappable agent provider: structured completions plus agentic runs."""

    name: ClassVar[str]

    def __init__(self, model: str | None = None) -> None:
        self.model = model

    @abstractmethod
    async def structured(
        self, system: str, prompt: str, schema: JsonObject
    ) -> JsonObject:
        """Return one JSON object matching ``schema`` for a tool-less prompt."""

    @abstractmethod
    def run(self, request: RunRequest) -> AsyncIterator[AgentEvent]:
        """Stream an agentic run; the last event is always kind RESULT."""

    @classmethod
    def parse_json_object(cls, text: str) -> JsonObject:
        """Parse a JSON object from model text, tolerating code fences and prose.

        Raises:
            AgentError: No JSON object could be parsed.
        """
        candidates = [text.strip()]
        fenced = FENCE_PATTERN.search(text)
        if fenced:
            candidates.append(fenced.group(1))
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            candidates.append(text[start : end + 1])
        for candidate in candidates:
            value = Json.loads(candidate)
            if isinstance(value, dict):
                return value
        raise AgentError(f"agent did not return a JSON object: {text[:300]!r}")


class Json:
    """Typed helpers for untyped JSON text and payloads."""

    @classmethod
    def loads(cls, text: str) -> JsonValue | None:
        """Parse JSON text, returning None when it is not valid JSON."""
        try:
            return JSON_ADAPTER.validate_json(text)
        except ValidationError:
            return None

    @classmethod
    def obj(cls, value: JsonValue | None) -> JsonObject:
        """Return ``value`` if it is an object, else an empty one."""
        return value if isinstance(value, dict) else {}

    @classmethod
    def text(cls, value: JsonValue | None) -> str:
        """Return ``value`` if it is a string, else an empty string."""
        return value if isinstance(value, str) else ""
