"""Human and JSON rendering for CLI results."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from jot.app.queries import JotDetail
    from jot.core.models import Comment, Event, Jot, Project
    from jot.core.profiles import Profile

INLINE_LIMIT = 24

type Plain = Mapping[str, Plain] | Sequence[Plain] | str | int | float | bool | None
type Emittable = BaseModel | Sequence[BaseModel] | Plain


class Output:
    """Render records as JSON or compact text."""

    @staticmethod
    def json(value: Emittable) -> str:
        """Serialize models (or plain values) as indented JSON."""
        return json.dumps(Output.plain(value), indent=2, ensure_ascii=False) + "\n"

    @staticmethod
    def plain(value: Emittable) -> object:
        """Convert models, recursively, into JSON-ready values."""
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, Mapping):
            return {key: Output.plain(item) for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, str):
            return [Output.plain(item) for item in value]
        return value

    @staticmethod
    def line(jot: Jot) -> str:
        """One list row: id, profile:status, title, labels, project."""
        tags = "".join(f" #{label}" for label in jot.labels)
        project = f" @{jot.project}" if jot.project else ""
        archived = " (archived)" if jot.archived_at else ""
        return (
            f"{jot.id:>5}  {jot.profile}:{jot.status:<10} "
            f"{jot.display_title}{tags}{project}{archived}\n"
        )

    @classmethod
    def lines(cls, jots: list[Jot]) -> str:
        """Render a list, or a note that nothing matched."""
        return "".join(cls.line(jot) for jot in jots) or "No jots\n"

    @staticmethod
    def jot(jot: Jot) -> str:
        """Render one jot's attributes and body."""
        rows = [
            f"#{jot.id} {jot.display_title}",
            f"profile: {jot.profile}  status: {jot.status} ({jot.category.value})"
            f"  version: {jot.version}",
        ]
        if jot.project:
            rows.append(f"project: {jot.project}")
        if jot.labels:
            rows.append(f"labels: {', '.join(jot.labels)}")
        if jot.metadata:
            rows.append(f"metadata: {json.dumps(jot.metadata, sort_keys=True)}")
        if jot.archived_at:
            rows.append(f"archived: {jot.archived_at.isoformat()}")
        if jot.deleted_at:
            rows.append(f"deleted: {jot.deleted_at.isoformat()}")
        if jot.body and jot.body.strip() != jot.display_title:
            rows.extend(("", jot.body.rstrip()))
        return "\n".join(rows) + "\n"

    @staticmethod
    def comment(comment: Comment) -> str:
        """Render one comment."""
        kind = "" if comment.kind == "comment" else f" [{comment.kind}]"
        edited = " (edited)" if comment.edited_at else ""
        return (
            f"  [{comment.id}] {comment.author}{kind}"
            f" {comment.created_at:%Y-%m-%d %H:%M}{edited}: {comment.body}\n"
        )

    @staticmethod
    def event(event: Event) -> str:
        """Render one history entry."""
        reason = f" ({event.reason})" if event.reason else ""
        version = f" v{event.version}" if event.version else ""
        changes = event.data.get("changes")
        summary = ""
        if isinstance(changes, dict):
            summary = ": " + ", ".join(
                Output.change(key, value) for key, value in changes.items()
            )
        return (
            f"  {event.ts:%Y-%m-%d %H:%M} {event.kind}{version} by {event.actor}"
            f" via {event.origin.value}{reason}{summary}\n"
        )

    @staticmethod
    def change(key: str, value: object) -> str:
        """Summarize one field change; short scalar values are shown inline."""
        if isinstance(value, dict):
            old, new = value.get("from"), value.get("to")
            if all(
                isinstance(v, str | int | bool | None) and len(str(v)) <= INLINE_LIMIT
                for v in (old, new)
            ):
                return f"{key} {old} -> {new}"
        return key

    @classmethod
    def detail(cls, detail: JotDetail, sections: set[str]) -> str:
        """Render a jot plus the requested sections."""
        text = cls.jot(detail.jot)
        if "links" in sections and detail.links:
            text += "\nLinks:\n" + "".join(
                f"  {link.src} {link.rel} {link.dst}\n" for link in detail.links
            )
        if "comments" in sections and detail.comments:
            text += "\nComments:\n" + "".join(cls.comment(c) for c in detail.comments)
        if "history" in sections:
            text += "\nHistory:\n" + "".join(cls.event(e) for e in detail.history)
        return text

    @staticmethod
    def project(project: Project) -> str:
        """Render one project row."""
        archived = " (archived)" if project.archived else ""
        description = f" - {project.description}" if project.description else ""
        return f"{project.slug:<20} {project.name}{description}{archived}\n"

    @staticmethod
    def profile(profile: Profile) -> str:
        """Render a profile's statuses and fields."""
        statuses = ", ".join(
            f"{s.id}{'*' if s.id == profile.initial_status else ''}"
            f" ({s.category.value})"
            for s in profile.statuses
        )
        rows = [f"{profile.id}: {profile.label}", f"  statuses: {statuses}"]
        if profile.transitions:
            rows.append(
                "  transitions: "
                + ", ".join(f"{a}->{b}" for a, b in profile.transitions)
            )
        for name, spec in profile.fields.items():
            options = f" [{'|'.join(spec.options)}]" if spec.options else ""
            required = " required" if spec.required else ""
            rows.append(f"  field {name}: {spec.type.value}{options}{required}")
        if profile.plugins:
            rows.append(f"  plugins: {', '.join(profile.plugins)}")
        return "\n".join(rows) + "\n"
