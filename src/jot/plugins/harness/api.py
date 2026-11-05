"""The ``harness@1`` contract: one interface over coding-agent runtimes.

A harness turns a prompt into either one JSON object (``structured``) or an
agentic run in a working directory (``run``). Every implementation declares
its capabilities and maps the three permission levels to its own flags
explicitly, so the same request means the same access on every runtime.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import Field, JsonValue, TypeAdapter, ValidationError

from jot.core.models import Metadata, Record
from jot.exceptions import AppError

if TYPE_CHECKING:
    from collections.abc import Iterator

SERVICE = "harness@1"
BACKEND = "harness.backend@1"
JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)
CHARS_PER_TOKEN = 4


class HarnessError(AppError):
    """A harness is missing, misconfigured, or returned unusable output."""


class Permission(StrEnum):
    """How much a run may change. Each harness maps these explicitly."""

    READ_ONLY = "read_only"  # read and search files; no edits, no shell
    WORKSPACE_WRITE = "workspace_write"  # edit files in the working directory
    FULL = "full"  # every tool, no sandbox: only in isolated workspaces


class EventKind(StrEnum):
    """Kinds of streamed run events. The last event is always RESULT."""

    TEXT = "text"
    THINKING = "thinking"
    TOOL = "tool"
    USAGE = "usage"
    ERROR = "error"
    RESULT = "result"


class Usage(Record):
    """Token and cost accounting. ``measured`` is False for estimates."""

    input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    premium_requests: float | None = None
    cost_usd: float | None = None
    measured: bool = True

    @classmethod
    def estimate(cls, prompt: str, output: str) -> Usage:
        """Estimate tokens from text length when a runtime reports none."""
        return cls(
            input=len(prompt) // CHARS_PER_TOKEN + 1,
            output=len(output) // CHARS_PER_TOKEN + 1,
            measured=False,
        )


class Capabilities(Record):
    """What a harness supports, for routing and display."""

    structured: bool = True
    run: bool = True
    resume: bool = False
    permissions: list[Permission] = Field(default_factory=lambda: list(Permission))
    usage: str = "measured"  # measured, estimated, or partial


def object_schema() -> Metadata:
    """The loosest schema: any JSON object."""
    return {"type": "object"}


class StructuredRequest(Record):
    """A tool-less prompt answered with one JSON object matching ``output_schema``."""

    prompt: str = Field(min_length=1)
    output_schema: Metadata = Field(default_factory=object_schema)
    system: str = ""
    model: str | None = None


class StructuredResult(Record):
    """A structured answer and what it cost."""

    value: Metadata
    usage: Usage = Field(default_factory=Usage)
    model: str | None = None


class RunRequest(Record):
    """An agentic run in a working directory."""

    prompt: str = Field(min_length=1)
    cwd: str
    permission: Permission = Permission.READ_ONLY
    system: str = ""
    model: str | None = None
    session_id: str | None = None


class HarnessEvent(Record):
    """One streamed step of a run."""

    kind: EventKind
    text: str = ""
    session_id: str | None = None
    model: str | None = None
    usage: Usage | None = None
    is_error: bool = False


class HarnessInfo(Record):
    """A backend as listed to users."""

    id: str
    label: str
    capabilities: Capabilities
    permissions: dict[Permission, list[str]]
    available: bool
    problem: str | None = None


class Harness(ABC):
    """Base class for harness implementations."""

    id: ClassVar[str] = ""
    label: ClassVar[str] = ""

    @abstractmethod
    def capabilities(self) -> Capabilities:
        """Describe what this harness supports."""

    @abstractmethod
    def permission_args(self, permission: Permission) -> list[str]:
        """Return the exact runtime flags for a permission level."""

    @abstractmethod
    def structured(self, request: StructuredRequest) -> StructuredResult:
        """Answer with one JSON object."""

    @abstractmethod
    def run(self, request: RunRequest) -> Iterator[HarnessEvent]:
        """Stream a run; the final event has kind RESULT."""

    def problem(self) -> str | None:
        """Return why this harness cannot run here (e.g. CLI missing), or None."""
        return None

    def info(self) -> HarnessInfo:
        """Describe this harness, including its permission mapping."""
        capabilities = self.capabilities()
        problem = self.problem()
        return HarnessInfo(
            id=self.id,
            label=self.label or self.id,
            capabilities=capabilities,
            permissions={p: self.permission_args(p) for p in capabilities.permissions},
            available=problem is None,
            problem=problem,
        )

    def check_permission(self, permission: Permission) -> None:
        """Refuse permission levels this harness does not support."""
        if permission not in self.capabilities().permissions:
            raise HarnessError(f"{self.id} does not support {permission.value}")

    @staticmethod
    def parse_object(text: str) -> Metadata:
        """Parse a JSON object from model text, tolerating fences and prose."""
        candidates = [text.strip()]
        if fenced := FENCE.search(text):
            candidates.append(fenced.group(1))
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            candidates.append(text[start : end + 1])
        for candidate in candidates:
            try:
                value = JSON.validate_json(candidate)
            except ValidationError:
                continue
            if isinstance(value, dict):
                return value
        raise HarnessError(f"No JSON object in model output: {text[:300]!r}")

    @staticmethod
    def check_required(value: Metadata, schema: Metadata) -> Metadata:
        """Check the top-level required keys of a JSON schema."""
        required = schema.get("required")
        if isinstance(required, list):
            absent = [
                key for key in required if isinstance(key, str) and key not in value
            ]
            if absent:
                raise HarnessError(f"Model output lacks required keys: {absent}")
        return value


def text_of(value: JsonValue) -> str:
    """Return a string value or ''."""
    return value if isinstance(value, str) else ""


def obj(value: JsonValue) -> Metadata:
    """Return an object value or {}."""
    return value if isinstance(value, dict) else {}


def integer(value: JsonValue) -> int | None:
    """Return an int (not bool) or None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def number(value: JsonValue) -> float | None:
    """Return a number (not bool) or None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def workdir(path: str) -> Path:
    """Return an existing working directory or raise."""
    folder = Path(path)
    if not folder.is_dir():
        raise HarnessError(f"Working directory {path} does not exist")
    return folder
