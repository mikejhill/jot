"""Validated HTTP request bodies and query parameters."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from jot.core.models import Criticality, Flow, Record, Status, TaskType

type Backend = Literal["claude", "codex", "copilot"]


class TaskFields(Record):
    """Editable task content; omitted fields remain unchanged."""

    title: str | None = Field(default=None, min_length=1)
    description: str | None = None
    project: str | None = None
    project_id: int | None = None
    labels: list[str] | None = None
    criticality: Criticality | None = None
    type: TaskType | None = None
    flow: Flow | None = None
    repo_path: str | None = None
    due_at: datetime | None = None


class Capture(TaskFields):
    """Raw quick capture with optional prefilled content."""

    text: str = Field(min_length=1)


class TaskFilters(Record):
    """Every repository filter plus type and priority ordering."""

    project: str | None = None
    labels: list[str] = Field(default_factory=list)
    statuses: list[Status] = Field(default_factory=list)
    status: Status | None = None
    criticality: Criticality | None = None
    type: TaskType | None = None
    older_than: int | None = Field(default=None, ge=0)
    age_field: Literal["updated_at", "created_at"] = "updated_at"
    q: str | None = None
    include_deleted: bool = False
    limit: int | None = Field(default=None, ge=0)
    order: str = "id"
    sort: Literal["priority", "updated", "created"] | None = None


class Move(Record):
    """Requested workflow transition."""

    status: Status


class Comment(Record):
    """Human feedback for comments or send-back actions."""

    comment: str = Field(min_length=1)


class RunRequest(Record):
    """Optional run policy and backend override."""

    flow: Flow | None = None
    backend: Backend | None = None
    model: str | None = Field(default=None, max_length=200)


class Approval(Record):
    """Explicit approval with an optional note and backend."""

    note: str | None = None
    backend: Backend | None = None
    model: str | None = Field(default=None, max_length=200)


class Answer(Record):
    """The owner's reply to one question event; blank defers to the agent."""

    question_id: int
    text: str = Field(default="", max_length=20000)


class Answers(Record):
    """Answers to a needs_input task's questions, sent back to the agent."""

    answers: list[Answer] = Field(default_factory=list)
    backend: Backend | None = None
    model: str | None = Field(default=None, max_length=200)


class Enrichment(Record):
    """Optional enrichment backend override."""

    backend: Backend | None = None
    model: str | None = Field(default=None, max_length=200)


class Markdown(Record):
    """Plain Markdown instruction content."""

    content: str


class Scan(Record):
    """Whether cleanup may consult an agent."""

    use_agent: bool = False
    backend: Backend | None = None
    model: str | None = Field(default=None, max_length=200)


class ApplyCleanup(Record):
    """Only these proposal item indexes are approved."""

    approved_indexes: list[int]
