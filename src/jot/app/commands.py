"""Validated write commands shared by the CLI, HTTP API, UI, and plugins.

Every command runs in one transaction that writes the change and its event
together, carries a ``WriteContext`` (actor, origin, correlation, reason), and
bumps the jot's version. Callers may pass ``expected_version`` (reject on any
intervening change) or ``base_version`` (reject only when a field they are
writing changed since that version).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import Field, JsonValue

from jot.core.models import (
    CORE_LINK_RELATIONS,
    NAME,
    Clock,
    Comment,
    Jot,
    Link,
    Metadata,
    Project,
    Record,
    WriteContext,
)
from jot.core.rank import Rank
from jot.db.store import METADATA, Store
from jot.exceptions import ConflictError, InvalidInputError, NotFoundError

if TYPE_CHECKING:
    from jot.core.profiles import Profile, ProfileRegistry
    from jot.db.connection import Database

FIELDS = "fields"
type Changes = dict[str, tuple[JsonValue, JsonValue]]


class Placement(StrEnum):
    """Where a jot goes in the global rank order."""

    TOP = "top"
    BOTTOM = "bottom"
    BEFORE = "before"
    AFTER = "after"


class JotPatch(Record):
    """Fields to change on a jot. Unset fields are left alone.

    ``fields`` and ``metadata`` merge key by key; a ``None`` value removes the
    key. ``project`` set to ``None`` clears it.
    """

    title: str | None = None
    body: str | None = None
    project: str | None = None
    profile: str | None = None
    fields: dict[str, JsonValue] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class Commands:
    """Apply validated changes for one writer."""

    def __init__(
        self, database: Database, profiles: ProfileRegistry, context: WriteContext
    ) -> None:
        self.db = database
        self.store = Store(database)
        self.profiles = profiles
        self.context = context

    # Jots -----------------------------------------------------------------

    def create(  # noqa: PLR0913 - one keyword per core attribute
        self,
        *,
        body: str = "",
        title: str = "",
        profile: str | None = None,
        status: str | None = None,
        project: str | None = None,
        labels: Iterable[str] = (),
        fields: dict[str, JsonValue] | None = None,
        metadata: Metadata | None = None,
    ) -> Jot:
        """Record a new jot at the bottom of the rank order."""
        if not (title.strip() or body.strip()):
            raise InvalidInputError("A jot needs a title or a body")
        spec = self.profiles.get(profile)
        state = spec.status(status or spec.initial_status)
        merged = self._merge_metadata({}, metadata or {})
        values = spec.validate_fields(fields or {})
        if values:
            merged[FIELDS] = values
        now = Clock.stamp()
        with self.db.write() as conn:
            project_id = self._project_id(conn, project)
            cursor = conn.execute(
                "INSERT INTO jots (title, body, profile, status, category, project_id,"
                " rank, metadata, created_by, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    title.strip(),
                    body,
                    spec.id,
                    state.id,
                    state.category.value,
                    project_id,
                    self._rank(conn, Placement.BOTTOM, None),
                    Store.dumps(merged),
                    self.context.actor,
                    now,
                    now,
                ),
            )
            jot_id = int(cursor.lastrowid or 0)
            names: list[JsonValue] = [
                *self._set_labels(conn, jot_id, add=labels, remove=())
            ]
            jot = self.store.get_jot(jot_id)
            self._event(
                conn,
                "jot.created",
                jot_id,
                1,
                {"profile": spec.id, "status": state.id, "labels": names},
            )
        return jot

    def update(
        self,
        jot_id: int,
        patch: JotPatch,
        *,
        expected_version: int | None = None,
        base_version: int | None = None,
    ) -> Jot:
        """Change title, body, project, profile, fields, or metadata."""
        given = patch.model_fields_set
        with self.db.write() as conn:
            jot = self.store.get_jot(jot_id)
            columns: dict[str, JsonValue] = {}
            changes: Changes = {}
            for name in ("title", "body"):
                value = getattr(patch, name)
                if name in given and value is not None and value != getattr(jot, name):
                    columns[name] = value
                    changes[name] = (getattr(jot, name), value)
            if "project" in given and (patch.project or None) != jot.project:
                columns["project_id"] = self._project_id(conn, patch.project or None)
                changes["project"] = (jot.project, patch.project or None)
            spec = self.profiles.get(jot.profile)
            if patch.profile and patch.profile != jot.profile:
                target = self.profiles.get(patch.profile)
                status = target.map_status(spec, jot.status)
                columns |= {
                    "profile": target.id,
                    "status": status,
                    "category": target.status(status).category.value,
                }
                changes["profile"] = (jot.profile, target.id)
                if status != jot.status:
                    changes["status"] = (jot.status, status)
                spec = target
            metadata = self._patched_metadata(jot, patch, spec)
            for key in sorted(set(metadata) | set(jot.metadata)):
                if metadata.get(key) != jot.metadata.get(key):
                    changes[f"metadata.{key}"] = (
                        jot.metadata.get(key),
                        metadata.get(key),
                    )
            if any(key.startswith("metadata.") for key in changes):
                columns["metadata"] = Store.dumps(metadata)
            return self._commit(
                conn,
                jot,
                columns,
                changes,
                expected_version=expected_version,
                base_version=base_version,
            )

    def set_status(
        self, jot_id: int, status: str, *, expected_version: int | None = None
    ) -> Jot:
        """Move a jot to another status of its profile."""
        with self.db.write() as conn:
            jot = self.store.get_jot(jot_id)
            spec = self.profiles.get(jot.profile)
            spec.check_transition(jot.status, status)
            if status == jot.status:
                return jot
            category = spec.status(status).category.value
            return self._commit(
                conn,
                jot,
                {"status": status, "category": category},
                {"status": (jot.status, status)},
                expected_version=expected_version,
                kind="jot.status_changed",
            )

    def label(
        self,
        jot_id: int,
        *,
        add: Iterable[str] = (),
        remove: Iterable[str] = (),
        expected_version: int | None = None,
    ) -> Jot:
        """Add and remove labels."""
        with self.db.write() as conn:
            jot = self.store.get_jot(jot_id)
            self._check_version(jot, expected_version)
            before: list[JsonValue] = [*jot.labels]
            after: list[JsonValue] = [
                *self._set_labels(conn, jot_id, add=add, remove=remove)
            ]
            if after == before:
                return jot
            return self._commit(conn, jot, {}, {"labels": (before, after)})

    def move_rank(
        self, jot_id: int, placement: Placement, anchor: int | None = None
    ) -> Jot:
        """Reorder a jot globally: top, bottom, or next to another jot."""
        if (anchor is None) != (placement in {Placement.TOP, Placement.BOTTOM}):
            raise InvalidInputError("before/after need an anchor jot; top/bottom don't")
        if anchor == jot_id:
            raise InvalidInputError("A jot cannot be ranked relative to itself")
        with self.db.write() as conn:
            jot = self.store.get_jot(jot_id)
            rank = self._rank(conn, placement, anchor, exclude=jot_id)
            if rank == jot.rank:
                return jot
            return self._commit(
                conn,
                jot,
                {"rank": rank},
                {"rank": (jot.rank, rank)},
                kind="jot.ranked",
            )

    def archive(self, jot_id: int, *, archived: bool = True) -> Jot:
        """Hide a jot from default views, or bring it back."""
        with self.db.write() as conn:
            jot = self.store.get_jot(jot_id)
            if (jot.archived_at is not None) == archived:
                return jot
            stamp = Clock.stamp() if archived else None
            return self._commit(
                conn,
                jot,
                {"archived_at": stamp},
                {"archived": (not archived, archived)},
                kind="jot.archived" if archived else "jot.unarchived",
            )

    def delete(self, jot_id: int, *, deleted: bool = True) -> Jot:
        """Soft-delete a jot (history is kept), or restore it."""
        with self.db.write() as conn:
            jot = self.store.get_jot(jot_id, include_deleted=True)
            if (jot.deleted_at is not None) == deleted:
                if deleted:
                    raise NotFoundError(f"Jot {jot_id} not found")
                return jot
            stamp = Clock.stamp() if deleted else None
            return self._commit(
                conn,
                jot,
                {"deleted_at": stamp},
                {"deleted": (not deleted, deleted)},
                kind="jot.deleted" if deleted else "jot.restored",
            )

    # Comments -------------------------------------------------------------

    def comment(
        self,
        jot_id: int,
        body: str,
        *,
        kind: str = "comment",
        reply_to: int | None = None,
    ) -> Comment:
        """Add a comment to a jot."""
        if not body.strip():
            raise InvalidInputError("A comment needs a body")
        self._check_name(kind, "comment kind")
        with self.db.write() as conn:
            jot = self.store.get_jot(jot_id)
            if (
                reply_to is not None
                and self.store.get_comment(reply_to).jot_id != jot_id
            ):
                raise InvalidInputError("A reply must be on the same jot")
            cursor = conn.execute(
                "INSERT INTO comments"
                " (jot_id, author, body, kind, reply_to, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (jot_id, self.context.actor, body, kind, reply_to, Clock.stamp()),
            )
            comment = self.store.get_comment(int(cursor.lastrowid or 0))
            self._event(
                conn,
                "comment.added",
                jot_id,
                jot.version,
                {"comment_id": comment.id, "kind": kind, "body": body},
            )
        return comment

    def edit_comment(self, comment_id: int, body: str) -> Comment:
        """Replace a comment's body; the previous text stays in history."""
        if not body.strip():
            raise InvalidInputError("A comment needs a body")
        with self.db.write() as conn:
            comment = self.store.get_comment(comment_id)
            if comment.body == body:
                return comment
            conn.execute(
                "UPDATE comments SET body = ?, edited_at = ? WHERE id = ?",
                (body, Clock.stamp(), comment_id),
            )
            self._event(
                conn,
                "comment.edited",
                comment.jot_id,
                None,
                {"comment_id": comment_id, "from": comment.body, "to": body},
            )
            return self.store.get_comment(comment_id)

    def delete_comment(self, comment_id: int) -> None:
        """Soft-delete a comment; its text stays in history."""
        with self.db.write() as conn:
            comment = self.store.get_comment(comment_id)
            conn.execute(
                "UPDATE comments SET deleted_at = ? WHERE id = ?",
                (Clock.stamp(), comment_id),
            )
            self._event(
                conn,
                "comment.deleted",
                comment.jot_id,
                None,
                {"comment_id": comment_id, "body": comment.body},
            )

    # Links ----------------------------------------------------------------

    def link(self, src: int, dst: int, rel: str = "relates") -> Link:
        """Relate two jots. Plugin relations use a dotted name (``exec.blocks``)."""
        if rel not in CORE_LINK_RELATIONS and "." not in rel:
            raise InvalidInputError(
                f"Unknown relation {rel!r}: use one of "
                f"{', '.join(sorted(CORE_LINK_RELATIONS))} or a dotted plugin name"
            )
        self._check_name(rel, "relation")
        if src == dst:
            raise InvalidInputError("A jot cannot link to itself")
        with self.db.write() as conn:
            self.store.get_jot(src)
            self.store.get_jot(dst)
            try:
                conn.execute(
                    "INSERT INTO links (src, dst, rel, created_at) VALUES (?, ?, ?, ?)",
                    (src, dst, rel, Clock.stamp()),
                )
            except sqlite3.IntegrityError:
                return Link(src=src, dst=dst, rel=rel)
            self._event(conn, "link.added", src, None, {"dst": dst, "rel": rel})
        return Link(src=src, dst=dst, rel=rel)

    def unlink(self, src: int, dst: int, rel: str = "relates") -> None:
        """Remove a relation."""
        with self.db.write() as conn:
            deleted = conn.execute(
                "DELETE FROM links WHERE src = ? AND dst = ? AND rel = ?",
                (src, dst, rel),
            ).rowcount
            if not deleted:
                raise NotFoundError(f"No {rel} link from {src} to {dst}")
            self._event(conn, "link.removed", src, None, {"dst": dst, "rel": rel})

    # Projects -------------------------------------------------------------

    def create_project(self, slug: str, name: str, description: str = "") -> Project:
        """Create a project."""
        project = Project(slug=slug, name=name, description=description)
        with self.db.write() as conn:
            try:
                conn.execute(
                    "INSERT INTO projects (slug, name, description, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (slug, name, description, Clock.stamp(project.created_at)),
                )
            except sqlite3.IntegrityError:
                raise InvalidInputError(f"Project {slug!r} already exists") from None
            self._event(conn, "project.created", None, None, {"slug": slug})
            return self.store.get_project(slug)

    def update_project(
        self,
        slug: str,
        *,
        name: str | None = None,
        description: str | None = None,
        archived: bool | None = None,
    ) -> Project:
        """Rename, describe, archive, or restore a project."""
        with self.db.write() as conn:
            project = self.store.get_project(slug)
            values = {
                key: value
                for key, value in (
                    ("name", name),
                    ("description", description),
                    ("archived", archived),
                )
                if value is not None and value != getattr(project, key)
            }
            if not values:
                return project
            assignments = ", ".join(f"{key} = ?" for key in values)
            conn.execute(
                f"UPDATE projects SET {assignments} WHERE id = ?",  # noqa: S608 - fixed keys
                (*values.values(), project.id),
            )
            self._event(conn, "project.updated", None, None, {"slug": slug, **values})
            return self.store.get_project(slug)

    # Internals --------------------------------------------------------------

    def _commit(  # noqa: PLR0913 - every jot write funnels through here
        self,
        conn: sqlite3.Connection,
        jot: Jot,
        columns: dict[str, JsonValue],
        changes: Changes,
        *,
        expected_version: int | None = None,
        base_version: int | None = None,
        kind: str = "jot.updated",
    ) -> Jot:
        """Check versions, write columns, bump the version, and record the event."""
        self._check_version(jot, expected_version)
        if base_version is not None:
            self._check_merge(conn, jot, base_version, set(changes))
        if not changes:
            return jot
        version = jot.version + 1
        columns |= {"version": version, "updated_at": Clock.stamp()}
        assignments = ", ".join(f"{key} = ?" for key in columns)
        conn.execute(
            f"UPDATE jots SET {assignments} WHERE id = ?",  # noqa: S608 - fixed keys
            (*columns.values(), jot.id),
        )
        self._event(
            conn,
            kind,
            jot.id,
            version,
            {"changes": {key: {"from": a, "to": b} for key, (a, b) in changes.items()}},
        )
        return self.store.get_jot(jot.id, include_deleted=True)

    @staticmethod
    def _check_version(jot: Jot, expected: int | None) -> None:
        """Reject a write based on a stale version."""
        if expected is not None and expected != jot.version:
            raise ConflictError(
                f"Jot {jot.id} is at version {jot.version}, not {expected}"
            )

    @staticmethod
    def _check_merge(
        conn: sqlite3.Connection, jot: Jot, base: int, writing: set[str]
    ) -> None:
        """Reject only when a field being written changed after ``base``."""
        changed: set[str] = set()
        for (data,) in conn.execute(
            "SELECT data FROM events WHERE jot_id = ? AND version > ?", (jot.id, base)
        ):
            recorded = METADATA.validate_json(data).get("changes")
            if isinstance(recorded, dict):
                changed |= set(recorded)
        overlap = sorted(changed & writing)
        if overlap:
            raise ConflictError(
                f"Jot {jot.id} changed {', '.join(overlap)} after version {base}"
            )

    def _patched_metadata(self, jot: Jot, patch: JotPatch, spec: Profile) -> Metadata:
        """Merge metadata namespaces and profile fields from a patch."""
        metadata = self._merge_metadata(jot.metadata, patch.metadata)
        current = metadata.get(FIELDS)
        values = dict(current) if isinstance(current, dict) else {}
        values = spec.validate_fields(values | patch.fields)
        if values:
            metadata[FIELDS] = values
        else:
            metadata.pop(FIELDS, None)
        return metadata

    def _merge_metadata(self, current: Metadata, patch: Metadata) -> Metadata:
        """Merge namespaces; ``None`` removes one. ``fields`` is profile-owned."""
        merged = dict(current)
        for key, value in patch.items():
            self._check_name(key, "metadata namespace")
            if key == FIELDS:
                raise InvalidInputError(
                    "Set profile fields with `fields`, not metadata"
                )
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _check_name(value: str, what: str) -> None:
        """Require a lowercase dotted identifier."""
        if not NAME.match(value):
            raise InvalidInputError(f"Invalid {what} {value!r}")

    def _project_id(self, conn: sqlite3.Connection, slug: str | None) -> int | None:
        """Resolve a project slug inside the current transaction."""
        if slug is None:
            return None
        row = conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            raise NotFoundError(f"Project {slug!r} not found")
        return int(row["id"])

    def _set_labels(
        self,
        conn: sqlite3.Connection,
        jot_id: int,
        *,
        add: Iterable[str],
        remove: Iterable[str],
    ) -> list[str]:
        """Apply label changes and return the jot's sorted label names."""
        for name in add:
            self._check_name(name, "label")
            conn.execute("INSERT OR IGNORE INTO labels (name) VALUES (?)", (name,))
            conn.execute(
                "INSERT OR IGNORE INTO jot_labels (jot_id, label_id)"
                " SELECT ?, id FROM labels WHERE name = ?",
                (jot_id, name),
            )
        for name in remove:
            conn.execute(
                "DELETE FROM jot_labels WHERE jot_id = ? AND label_id ="
                " (SELECT id FROM labels WHERE name = ?)",
                (jot_id, name),
            )
        return [
            row[0]
            for row in conn.execute(
                "SELECT l.name FROM jot_labels jl JOIN labels l ON l.id = jl.label_id"
                " WHERE jl.jot_id = ? ORDER BY l.name",
                (jot_id,),
            )
        ]

    def _rank(
        self,
        conn: sqlite3.Connection,
        placement: Placement,
        anchor: int | None,
        *,
        exclude: int = 0,
    ) -> str:
        """Compute a rank key for the placement among all other jots."""

        def edge(sql: str, *args: object) -> str | None:
            row = conn.execute(sql, (exclude, *args)).fetchone()
            return None if row is None else str(row[0])

        if placement == Placement.TOP:
            return Rank.between(
                None, edge("SELECT min(rank) FROM jots WHERE id != ? HAVING count(*)")
            )
        if placement == Placement.BOTTOM:
            return Rank.between(
                edge("SELECT max(rank) FROM jots WHERE id != ? HAVING count(*)"), None
            )
        pivot = self.store.get_jot(int(anchor or 0)).rank
        if placement == Placement.BEFORE:
            low = edge(
                "SELECT max(rank) FROM jots WHERE id != ? AND rank < ? HAVING count(*)",
                pivot,
            )
            return Rank.between(low, pivot)
        high = edge(
            "SELECT min(rank) FROM jots WHERE id != ? AND rank > ? HAVING count(*)",
            pivot,
        )
        return Rank.between(pivot, high)

    def record(self, kind: str, data: Metadata) -> None:
        """Append an event that is not about one jot (for example plugin admin)."""
        with self.db.write() as conn:
            self._event(conn, kind, None, None, data)

    def _event(
        self,
        conn: sqlite3.Connection,
        kind: str,
        jot_id: int | None,
        version: int | None,
        data: Metadata,
    ) -> None:
        """Append one event in the caller's transaction."""
        context = self.context
        conn.execute(
            "INSERT INTO events (ts, kind, jot_id, version, actor, origin,"
            " correlation_id, reason, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                Clock.stamp(),
                kind,
                jot_id,
                version,
                context.actor,
                context.origin.value,
                context.correlation_id,
                context.reason,
                Store.dumps(data),
            ),
        )
