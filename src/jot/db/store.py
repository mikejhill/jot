"""Row mapping and low-level reads shared by commands and queries."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from pydantic import JsonValue, TypeAdapter

from jot.core.models import Comment, Event, Jot, Link, Metadata, Project
from jot.exceptions import NotFoundError

if TYPE_CHECKING:
    from jot.db.connection import Database

JOT_SELECT = """
SELECT j.*, p.slug AS project_slug,
    (SELECT json_group_array(name) FROM (
        SELECT l.name FROM jot_labels jl JOIN labels l ON l.id = jl.label_id
        WHERE jl.jot_id = j.id ORDER BY l.name)) AS label_names
FROM jots j LEFT JOIN projects p ON p.id = j.project_id
"""
METADATA = TypeAdapter(dict[str, JsonValue])
NAMES = TypeAdapter(list[str])


class Store:
    """Map SQLite rows to core records."""

    def __init__(self, database: Database) -> None:
        self.db = database

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the open connection."""
        return self.db.connection

    @staticmethod
    def dumps(value: Metadata) -> str:
        """Serialize JSON compactly with stable key order."""
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def jot(row: sqlite3.Row) -> Jot:
        """Build a jot from a ``JOT_SELECT`` row."""
        return Jot(
            id=row["id"],
            title=row["title"],
            body=row["body"],
            profile=row["profile"],
            status=row["status"],
            category=row["category"],
            project=row["project_slug"],
            labels=NAMES.validate_json(row["label_names"]),
            rank=row["rank"],
            metadata=METADATA.validate_json(row["metadata"]),
            version=row["version"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived_at=row["archived_at"],
            deleted_at=row["deleted_at"],
        )

    def get_jot(self, jot_id: int, *, include_deleted: bool = False) -> Jot:
        """Return one jot or raise ``NotFoundError``."""
        row = self.connection.execute(
            JOT_SELECT + " WHERE j.id = ?", (jot_id,)
        ).fetchone()
        if row is None or (row["deleted_at"] is not None and not include_deleted):
            raise NotFoundError(f"Jot {jot_id} not found")
        return self.jot(row)

    @staticmethod
    def project(row: sqlite3.Row) -> Project:
        """Build a project from a ``projects`` row."""
        return Project(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            description=row["description"],
            archived=bool(row["archived"]),
            created_at=row["created_at"],
        )

    def get_project(self, slug: str) -> Project:
        """Return a project by slug or raise ``NotFoundError``."""
        row = self.connection.execute(
            "SELECT * FROM projects WHERE slug = ?", (slug,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Project {slug!r} not found")
        return self.project(row)

    @staticmethod
    def comment(row: sqlite3.Row) -> Comment:
        """Build a comment from a ``comments`` row."""
        return Comment(
            id=row["id"],
            jot_id=row["jot_id"],
            author=row["author"],
            body=row["body"],
            kind=row["kind"],
            reply_to=row["reply_to"],
            created_at=row["created_at"],
            edited_at=row["edited_at"],
            deleted_at=row["deleted_at"],
        )

    def get_comment(self, comment_id: int) -> Comment:
        """Return a live comment or raise ``NotFoundError``."""
        row = self.connection.execute(
            "SELECT * FROM comments WHERE id = ? AND deleted_at IS NULL",
            (comment_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Comment {comment_id} not found")
        return self.comment(row)

    @staticmethod
    def event(row: sqlite3.Row) -> Event:
        """Build an event from an ``events`` row."""
        return Event(
            id=row["id"],
            ts=row["ts"],
            kind=row["kind"],
            jot_id=row["jot_id"],
            version=row["version"],
            actor=row["actor"],
            origin=row["origin"],
            correlation_id=row["correlation_id"],
            reason=row["reason"],
            data=METADATA.validate_json(row["data"]),
        )

    @staticmethod
    def link(row: sqlite3.Row) -> Link:
        """Build a link from a ``links`` row."""
        return Link(src=row["src"], dst=row["dst"], rel=row["rel"])
