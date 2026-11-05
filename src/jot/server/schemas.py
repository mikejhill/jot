"""Request bodies for the HTTP API. Responses reuse the core records."""

from __future__ import annotations

from pydantic import Field, JsonValue

from jot.app.commands import JotPatch, Placement
from jot.core.models import Metadata, Record


class CreateJot(Record):
    """Capture a jot."""

    title: str = ""
    body: str = ""
    profile: str | None = None
    status: str | None = None
    project: str | None = None
    labels: list[str] = Field(default_factory=list)
    fields: dict[str, JsonValue] = Field(default_factory=dict)
    metadata: Metadata = Field(default_factory=dict)


class UpdateJot(JotPatch):
    """A patch plus optional version checks."""

    expected_version: int | None = None
    base_version: int | None = None

    def patch(self) -> JotPatch:
        """Return only the patch fields the client actually sent."""
        names = set(JotPatch.model_fields) & self.model_fields_set
        return JotPatch.model_validate({name: getattr(self, name) for name in names})


class SetStatus(Record):
    """Move to a status."""

    status: str
    expected_version: int | None = None


class ChangeLabels(Record):
    """Add and remove labels."""

    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)
    expected_version: int | None = None


class MoveRank(Record):
    """Reorder a jot."""

    placement: Placement
    anchor: int | None = None


class AddComment(Record):
    """Add a comment."""

    body: str
    kind: str = "comment"
    reply_to: int | None = None


class EditComment(Record):
    """Replace a comment's body."""

    body: str


class LinkBody(Record):
    """Create or remove a link."""

    src: int
    dst: int
    rel: str = "relates"


class CreateProject(Record):
    """Create a project."""

    slug: str
    name: str
    description: str = ""


class UpdateProject(Record):
    """Change a project."""

    name: str | None = None
    description: str | None = None
    archived: bool | None = None
