"""Read-side queries shared by every interface."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import Field

from jot.core.models import (
    Comment,
    Event,
    Jot,
    Link,
    Project,
    Record,
    StatusCategory,
)
from jot.db.store import JOT_SELECT, Store

if TYPE_CHECKING:
    from jot.db.connection import Database

TOKEN = re.compile(r"\w+", re.UNICODE)


class Sort(StrEnum):
    """Result orderings."""

    RANK = "rank"
    CREATED = "created"
    UPDATED = "updated"


class JotFilter(Record):
    """Criteria for listing jots. Empty lists mean "any"."""

    profiles: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    categories: list[StatusCategory] = Field(default_factory=list)
    project: str | None = None
    labels: list[str] = Field(default_factory=list)
    text: str | None = None
    include_archived: bool = False
    include_deleted: bool = False
    sort: Sort = Sort.RANK
    limit: int | None = Field(default=None, ge=0)


class JotDetail(Record):
    """A jot with its comments, links, and history."""

    jot: Jot
    comments: list[Comment]
    links: list[Link]
    history: list[Event]


class Queries:
    """Read jots, comments, links, history, and projects."""

    def __init__(self, database: Database) -> None:
        self.store = Store(database)

    def jots(self, criteria: JotFilter) -> list[Jot]:
        """Return jots matching every given criterion."""
        clauses: list[str] = []
        args: list[object] = []
        for column, values in (
            ("j.profile", criteria.profiles),
            ("j.status", criteria.statuses),
            ("j.category", [c.value for c in criteria.categories]),
        ):
            if values:
                clauses.append(f"{column} IN ({', '.join('?' * len(values))})")
                args.extend(values)
        if criteria.project is not None:
            clauses.append("p.slug = ?")
            args.append(criteria.project)
        for label in criteria.labels:
            clauses.append(
                "EXISTS (SELECT 1 FROM jot_labels jl JOIN labels l ON l.id ="
                " jl.label_id WHERE jl.jot_id = j.id AND l.name = ?)"
            )
            args.append(label)
        if criteria.text:
            clauses.append(
                "j.id IN (SELECT rowid FROM jots_fts WHERE jots_fts MATCH ?)"
            )
            args.append(self.match(criteria.text))
        if not criteria.include_archived:
            clauses.append("j.archived_at IS NULL")
        if not criteria.include_deleted:
            clauses.append("j.deleted_at IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        order = {
            Sort.RANK: "j.rank, j.id",
            Sort.CREATED: "j.created_at DESC, j.id DESC",
            Sort.UPDATED: "j.updated_at DESC, j.id DESC",
        }[criteria.sort]
        sql = f"{JOT_SELECT}{where} ORDER BY {order}"
        if criteria.limit is not None:
            sql += " LIMIT ?"
            args.append(criteria.limit)
        rows = self.store.connection.execute(sql, args).fetchall()
        return [Store.jot(row) for row in rows]

    @staticmethod
    def match(text: str) -> str:
        """Turn free text into an FTS5 query: every word, prefix-matched."""
        return " ".join(f'"{token}"*' for token in TOKEN.findall(text)) or '""'

    def get(self, jot_id: int, *, include_deleted: bool = False) -> Jot:
        """Return one jot."""
        return self.store.get_jot(jot_id, include_deleted=include_deleted)

    def detail(self, jot_id: int) -> JotDetail:
        """Return a jot with comments, links (both directions), and history."""
        return JotDetail(
            jot=self.store.get_jot(jot_id, include_deleted=True),
            comments=self.comments(jot_id),
            links=self.links(jot_id),
            history=self.history(jot_id),
        )

    def comments(self, jot_id: int) -> list[Comment]:
        """Return a jot's live comments, oldest first."""
        rows = self.store.connection.execute(
            "SELECT * FROM comments WHERE jot_id = ? AND deleted_at IS NULL"
            " ORDER BY id",
            (jot_id,),
        )
        return [Store.comment(row) for row in rows]

    def links(self, jot_id: int) -> list[Link]:
        """Return links from or to a jot."""
        rows = self.store.connection.execute(
            "SELECT * FROM links WHERE src = ? OR dst = ? ORDER BY src, dst, rel",
            (jot_id, jot_id),
        )
        return [Store.link(row) for row in rows]

    def history(self, jot_id: int) -> list[Event]:
        """Return a jot's events, oldest first."""
        rows = self.store.connection.execute(
            "SELECT * FROM events WHERE jot_id = ? ORDER BY id", (jot_id,)
        )
        return [Store.event(row) for row in rows]

    def events(self, after: int = 0, limit: int = 100) -> list[Event]:
        """Return events after an id: the cursor feed plugins will consume."""
        rows = self.store.connection.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after, limit)
        )
        return [Store.event(row) for row in rows]

    def projects(self, *, include_archived: bool = False) -> list[Project]:
        """Return projects sorted by slug."""
        sql = "SELECT * FROM projects"
        if not include_archived:
            sql += " WHERE archived = 0"
        rows = self.store.connection.execute(sql + " ORDER BY slug")
        return [Store.project(row) for row in rows]

    def labels(self) -> list[tuple[str, int]]:
        """Return each label with its count of live jots."""
        rows = self.store.connection.execute(
            "SELECT l.name, count(j.id) FROM labels l"
            " LEFT JOIN jot_labels jl ON jl.label_id = l.id"
            " LEFT JOIN jots j ON j.id = jl.jot_id AND j.deleted_at IS NULL"
            " GROUP BY l.id ORDER BY l.name"
        )
        return [(row[0], row[1]) for row in rows]
