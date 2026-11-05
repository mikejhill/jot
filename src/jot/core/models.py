"""Typed core records: jots, projects, comments, links, events, and write context."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

SLUG = r"^[a-z0-9][a-z0-9_-]*$"
NAME = re.compile(r"^[a-z][a-z0-9_.-]*$")
CORE_LINK_RELATIONS = frozenset({"parent", "relates", "blocks", "duplicates"})

type Metadata = dict[str, JsonValue]


class Clock:
    """UTC timestamps shared by persistence and records."""

    @staticmethod
    def now() -> datetime:
        """Return an aware UTC timestamp."""
        return datetime.now(UTC)

    @staticmethod
    def stamp(value: datetime | None = None) -> str:
        """Return a lexically sortable UTC timestamp."""
        return (value or Clock.now()).astimezone(UTC).isoformat()


class StatusCategory(StrEnum):
    """Profile-independent meaning of a status, used for cross-profile queries."""

    OPEN = "open"
    ACTIVE = "active"
    DONE = "done"
    CANCELLED = "cancelled"


class Origin(StrEnum):
    """Interface or component a write came through."""

    CLI = "cli"
    API = "api"
    UI = "ui"
    PLUGIN = "plugin"
    IMPORT = "import"
    SYSTEM = "system"


class Record(BaseModel):
    """Strict base for persisted records."""

    model_config = ConfigDict(extra="forbid")


class WriteContext(Record):
    """Who is writing, through what, why, and which logical operation it belongs to.

    Every command carries one, so history is attributable and plugins can
    recognize (and skip) their own changes.
    """

    actor: str = Field(default="user", min_length=1)
    origin: Origin = Origin.CLI
    correlation_id: str = Field(default_factory=lambda: uuid4().hex)
    reason: str | None = None


class Project(Record):
    """A named grouping of jots."""

    id: int = 0
    slug: str = Field(min_length=1, pattern=SLUG)
    name: str = Field(min_length=1)
    description: str = ""
    archived: bool = False
    created_at: datetime = Field(default_factory=Clock.now)


class Jot(Record):
    """One bite-sized unit of information."""

    id: int = 0
    title: str = ""
    body: str = ""
    profile: str
    status: str
    category: StatusCategory
    project: str | None = None
    labels: list[str] = Field(default_factory=list)
    rank: str
    metadata: Metadata = Field(default_factory=dict)
    version: int = 1
    created_by: str = "user"
    created_at: datetime = Field(default_factory=Clock.now)
    updated_at: datetime = Field(default_factory=Clock.now)
    archived_at: datetime | None = None
    deleted_at: datetime | None = None

    @property
    def display_title(self) -> str:
        """Return the title, or the body's first line when no title was given."""
        if self.title:
            return self.title
        first = self.body.strip().splitlines()[0] if self.body.strip() else ""
        return first[:80] or f"Jot {self.id}"

    @field_validator("created_at", "updated_at", "archived_at", "deleted_at")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        """Reject naive timestamps and normalize to UTC."""
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Timestamps must include a timezone")
        return value.astimezone(UTC)


class Comment(Record):
    """One entry in a jot's comment collection."""

    id: int = 0
    jot_id: int
    author: str
    body: str = Field(min_length=1)
    kind: str = "comment"
    reply_to: int | None = None
    created_at: datetime = Field(default_factory=Clock.now)
    edited_at: datetime | None = None
    deleted_at: datetime | None = None


class Link(Record):
    """A directed, typed relation between two jots."""

    src: int
    dst: int
    rel: str


class Event(Record):
    """Append-only record of one committed change; also the plugin event feed."""

    id: int = 0
    ts: datetime = Field(default_factory=Clock.now)
    kind: str
    jot_id: int | None = None
    version: int | None = None
    actor: str
    origin: Origin
    correlation_id: str
    reason: str | None = None
    data: Metadata = Field(default_factory=dict)
