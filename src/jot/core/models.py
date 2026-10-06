"""Typed records and bounded task vocabulary."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Status(StrEnum):
    """Task lifecycle states."""

    INBOX = "inbox"
    READY = "ready"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    REVIEW = "review"
    DONE = "done"
    BLOCKED = "blocked"
    WONT_DO = "wont_do"
    ARCHIVED = "archived"


class Criticality(StrEnum):
    """Impact levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TaskType(StrEnum):
    """Kinds of captured work."""

    FEATURE = "feature"
    BUG = "bug"
    CHORE = "chore"
    RESEARCH = "research"
    IDEA = "idea"


class Flow(StrEnum):
    """Planning policies."""

    PLANNED = "planned"
    DIRECT = "direct"


class EventKind(StrEnum):
    """Audit event categories."""

    CREATED = "created"
    ENRICHED = "enriched"
    STATUS = "status"
    COMMENT = "comment"
    PLAN = "plan"
    QUESTION = "question"
    ANSWER = "answer"
    APPROVAL = "approval"
    RUN_LOG = "run_log"
    RESULT = "result"


class Clock:
    """UTC timestamp formatting shared by persistence and leases."""

    @staticmethod
    def now() -> datetime:
        """Return an aware UTC timestamp."""
        return datetime.now(UTC)

    @staticmethod
    def stamp(value: datetime | None = None) -> str:
        """Return a lexically sortable UTC timestamp."""
        return (value or Clock.now()).astimezone(UTC).isoformat()


class Record(BaseModel):
    """Strict input fields for persisted records."""

    model_config = ConfigDict(extra="forbid")


class Project(Record):
    """Project defaults and repository context."""

    id: int = 0
    slug: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1)
    description: str = ""
    repo_path: str | None = None
    aliases: list[str] = Field(default_factory=list)
    priority: float = Field(default=1, gt=0)
    archived: bool = False
    default_flow: Flow | None = None


class Task(Record):
    """Task capture, lifecycle, and lease state."""

    id: int = 0
    title: str = Field(min_length=1)
    description: str = ""
    raw_input: str = ""
    project_id: int | None = None
    type: TaskType = TaskType.IDEA
    criticality: Criticality = Criticality.MEDIUM
    status: Status = Status.INBOX
    flow: Flow | None = None
    repo_path: str | None = None
    due_at: datetime | None = None
    source: str = Field(default="cli", pattern="^(cli|ui|agent)$")
    parent_id: int | None = None
    needs_enrichment: bool = True
    claimed_by: str | None = None
    lease_expires_at: datetime | None = None
    lease_prior_status: Status | None = None
    blocked_prior_status: Status | None = None
    created_at: datetime = Field(default_factory=Clock.now)
    updated_at: datetime = Field(default_factory=Clock.now)
    completed_at: datetime | None = None
    deleted_at: datetime | None = None
    labels: list[str] = Field(default_factory=list)

    @field_validator(
        "due_at",
        "lease_expires_at",
        "created_at",
        "updated_at",
        "completed_at",
        "deleted_at",
    )
    @classmethod
    def aware_timestamp(cls, value: datetime | None) -> datetime | None:
        """Reject ambiguous naive timestamps and normalize aware timestamps to UTC."""
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Timestamps must include a timezone")
        return value.astimezone(UTC)


class TaskEvent(Record):
    """Append-only task history."""

    id: int = 0
    task_id: int
    ts: datetime = Field(default_factory=Clock.now)
    actor: str
    kind: EventKind
    body: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class Run(Record):
    """Provider run metadata without execution behaviour."""

    id: int = 0
    task_id: int
    backend: str
    model: str | None = None
    phase: str = Field(pattern="^(plan|execute)$")
    status: str = "pending"
    session_id: str | None = None
    worktree: str | None = None
    branch: str | None = None
    summary: str | None = None
    cost: float | None = None
    started_at: datetime = Field(default_factory=Clock.now)
    ended_at: datetime | None = None
