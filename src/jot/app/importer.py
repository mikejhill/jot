"""Import a Jot v0.1 task database into the current data home.

The source is opened read-only and never changed. Tasks become jots with the
same ids so existing references such as ``#26`` keep working. Workflow states
the target profile has no status for become labels, and every v0.1 detail
without a core equivalent is kept: small fields under the jot's ``v01``
metadata namespace, and the full event log and runs in the ``jot.imported``
event.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field, JsonValue, TypeAdapter

from jot.app.commands import ImportedComment, ImportedJot
from jot.core.models import NAME, SLUG, Clock, Metadata, Record
from jot.exceptions import ConflictError, InvalidInputError, RepositoryError

if TYPE_CHECKING:
    from jot.app.commands import Commands
    from jot.app.session import Session
    from jot.core.models import WriteContext

JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
NAMESPACE = "v01"

# v0.1 status -> (target status, extra label). Archived is also archived in v0.2.
STATUS_MAP: dict[str, tuple[str, str | None]] = {
    "inbox": ("todo", None),
    "ready": ("todo", None),
    "planning": ("doing", "planning"),
    "needs_input": ("doing", "needs-input"),
    "awaiting_approval": ("doing", "awaiting-approval"),
    "executing": ("doing", "executing"),
    "review": ("doing", "review"),
    "blocked": ("todo", "blocked"),
    "done": ("done", None),
    "wont_do": ("wont_do", None),
    "archived": ("todo", None),
}
# v0.1 event kinds whose text becomes a comment of this kind.
COMMENT_KINDS = {
    "comment": "comment",
    "question": "execution.question",
    "answer": "execution.answer",
    "plan": "execution.plan",
    "approval": "execution.approval",
    "result": "execution.result",
}
CRITICALITY = {"critical": 0, "high": 1, "medium": 2, "low": 3}
FINISHED = {"done", "wont_do", "archived"}
SMALL_FIELDS = (
    "status",
    "type",
    "criticality",
    "flow",
    "repo_path",
    "due_at",
    "source",
    "needs_enrichment",
    "completed_at",
    "claimed_by",
    "blocked_prior_status",
    "raw_input",
)


class ImportReport(Record):
    """What an import did, or would do in a dry run."""

    source: str
    dry_run: bool
    projects_created: list[str] = Field(default_factory=list)
    projects_existing: list[str] = Field(default_factory=list)
    jots: int = 0
    deleted: int = 0
    archived: int = 0
    comments: int = 0
    links: int = 0
    events: int = 0
    runs: int = 0
    statuses: dict[str, int] = Field(default_factory=dict)
    renamed_labels: dict[str, str] = Field(default_factory=dict)
    skipped: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class Snapshot(Record):
    """Rows read from a v0.1 database."""

    projects: list[Metadata]
    tasks: list[Metadata]
    labels: dict[int, list[str]]
    events: dict[int, list[Metadata]]
    runs: dict[int, list[Metadata]]
    cleanup_proposals: int


class V01Importer:
    """Read a v0.1 database and write its contents through ``Commands``."""

    def __init__(self, source: Path, *, profile: str = "task") -> None:
        self.source = source
        self.profile = profile

    # Reading ----------------------------------------------------------------

    def read(self) -> Snapshot:
        """Read every table needed from a private copy, never touching the source.

        Even a read-only SQLite connection creates ``-shm``/``-wal`` files beside a
        WAL-mode database, so the database (and its WAL, if any) is copied to a
        temporary folder first and read there.
        """
        if not self.source.is_file():
            raise InvalidInputError(f"No v0.1 database at {self.source}")
        with tempfile.TemporaryDirectory(prefix="jot-import-") as scratch:
            copy = Path(scratch) / "source.db"
            shutil.copyfile(self.source, copy)
            wal = self.source.with_name(f"{self.source.name}-wal")
            if wal.is_file():
                shutil.copyfile(wal, copy.with_name("source.db-wal"))
            connection = sqlite3.connect(copy)
            connection.row_factory = sqlite3.Row
            try:
                return self._snapshot(connection)
            except sqlite3.Error as err:
                raise RepositoryError(
                    f"Cannot read {self.source} as a Jot v0.1 database: {err}"
                ) from err
            finally:
                connection.close()

    def _snapshot(self, connection: sqlite3.Connection) -> Snapshot:
        """Collect rows into plain records."""
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "tasks" not in tables:
            raise InvalidInputError(f"{self.source} is not a Jot v0.1 database")

        def rows(sql: str) -> list[Metadata]:
            return [self._plain(dict(row)) for row in connection.execute(sql)]

        labels: dict[int, list[str]] = {}
        for row in connection.execute(
            "SELECT tl.task_id, l.name FROM task_labels tl"
            " JOIN labels l ON l.id = tl.label_id ORDER BY l.name"
        ):
            labels.setdefault(row[0], []).append(row[1])
        events: dict[int, list[Metadata]] = {}
        for event in rows("SELECT * FROM task_events ORDER BY id"):
            body = event.get("body")
            event["body"] = JSON.validate_json(body) if isinstance(body, str) else body
            events.setdefault(self._int(event["task_id"]), []).append(event)
        runs: dict[int, list[Metadata]] = {}
        if "runs" in tables:
            for run in rows("SELECT * FROM runs ORDER BY id"):
                runs.setdefault(self._int(run["task_id"]), []).append(run)
        cleanup = 0
        if "cleanup_proposals" in tables:
            cleanup = connection.execute(
                "SELECT count(*) FROM cleanup_proposals"
            ).fetchone()[0]
        return Snapshot(
            projects=rows("SELECT * FROM projects ORDER BY id"),
            tasks=rows("SELECT * FROM tasks ORDER BY id"),
            labels=labels,
            events=events,
            runs=runs,
            cleanup_proposals=cleanup,
        )

    @staticmethod
    def _plain(row: dict[str, object]) -> Metadata:
        """Keep JSON-compatible column values."""
        return {
            key: value if isinstance(value, str | int | float | None) else str(value)
            for key, value in row.items()
        }

    @staticmethod
    def _int(value: JsonValue) -> int:
        """Return an integer column."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise RepositoryError(f"Expected an integer id, got {value!r}")
        return value

    @staticmethod
    def _text(value: JsonValue) -> str:
        """Return a text column ('' for NULL)."""
        return "" if value is None else str(value)

    @staticmethod
    def _time(value: JsonValue, fallback: datetime) -> datetime:
        """Parse a stored timestamp, treating naive values as UTC."""
        if not isinstance(value, str) or not value:
            return fallback
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return fallback
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    # Mapping ----------------------------------------------------------------

    @staticmethod
    def label(name: str) -> str:
        """Turn a v0.1 label into a valid name (lowercase, dashes)."""
        cleaned = re.sub(r"[^a-z0-9_.-]+", "-", name.strip().lower()).strip("-")
        if not cleaned:
            return "unnamed"
        return cleaned if NAME.match(cleaned) else f"l-{cleaned}"

    def order(self, tasks: list[Metadata]) -> list[Metadata]:
        """Return tasks in their new rank order: open work by criticality, then age."""

        def key(task: Metadata) -> tuple[int, int, str, int]:
            status = self._text(task.get("status"))
            criticality = CRITICALITY.get(self._text(task.get("criticality")), 2)
            return (
                status in FINISHED,
                criticality,
                self._text(task.get("created_at")),
                self._int(task["id"]),
            )

        return sorted(tasks, key=key)

    def jot(
        self,
        task: Metadata,
        snapshot: Snapshot,
        slugs: dict[int, str],
        report: ImportReport,
    ) -> ImportedJot:
        """Map one task to an imported jot, recording what happened in the report."""
        task_id = self._int(task["id"])
        status = self._text(task.get("status"))
        target, extra = STATUS_MAP.get(status, ("todo", None))
        report.statuses[f"{status} -> {target}"] = (
            report.statuses.get(f"{status} -> {target}", 0) + 1
        )
        labels: set[str] = set()
        for name in snapshot.labels.get(task_id, []):
            mapped = self.label(name)
            if mapped != name:
                report.renamed_labels[name] = mapped
            labels.add(mapped)
        if extra:
            labels.add(extra)
        kind = self._text(task.get("type"))
        if kind and kind != "idea":
            labels.add(self.label(kind))
        if status == "inbox" and task.get("needs_enrichment") == 1:
            labels.add("triaging")
        created = self._time(task.get("created_at"), Clock.now())
        updated = self._time(task.get("updated_at"), created)
        events = snapshot.events.get(task_id, [])
        comments = [
            comment for event in events if (comment := self._comment(event, created))
        ]
        runs = snapshot.runs.get(task_id, [])
        project_id = task.get("project_id")
        archived = status == "archived"
        deleted = task.get("deleted_at") is not None
        report.jots += 1
        report.deleted += deleted
        report.archived += archived
        report.comments += len(comments)
        report.events += len(events)
        report.runs += len(runs)
        source = self._text(task.get("source")) or "cli"
        return ImportedJot(
            id=task_id,
            title=self._text(task.get("title")),
            body=self._text(task.get("description")),
            profile=self.profile,
            status=target,
            project=slugs.get(project_id) if isinstance(project_id, int) else None,
            labels=sorted(labels),
            metadata={
                NAMESPACE: {
                    "id": task_id,
                    **{
                        name: task.get(name)
                        for name in SMALL_FIELDS
                        if task.get(name) not in (None, "")
                    },
                }
            },
            created_by="user" if source in {"cli", "ui"} else source,
            created_at=created,
            updated_at=updated,
            archived_at=updated if archived else None,
            deleted_at=self._time(task.get("deleted_at"), updated) if deleted else None,
            comments=comments,
            data={"source": "jot v0.1", "task": task, "events": events, "runs": runs},
        )

    def _comment(self, event: Metadata, fallback: datetime) -> ImportedComment | None:
        """Turn a text-bearing v0.1 event into a comment."""
        kind = COMMENT_KINDS.get(self._text(event.get("kind")))
        body = event.get("body")
        text = body.get("text") if isinstance(body, dict) else None
        if kind is None or not isinstance(text, str) or not text.strip():
            return None
        return ImportedComment(
            author=self._text(event.get("actor")) or "user",
            body=text,
            kind=kind,
            created_at=self._time(event.get("ts"), fallback),
        )

    # Writing ----------------------------------------------------------------

    def run(
        self, session: Session, context: WriteContext, *, dry_run: bool = False
    ) -> ImportReport:
        """Import everything in one transaction (or only report, for a dry run)."""
        snapshot = self.read()
        report = ImportReport(source=str(self.source), dry_run=dry_run)
        if snapshot.cleanup_proposals:
            report.skipped["cleanup_proposals"] = snapshot.cleanup_proposals
        profile = session.profiles.get(self.profile)
        for target, _ in STATUS_MAP.values():
            profile.status(target)
        if session.db.connection.execute("SELECT 1 FROM jots LIMIT 1").fetchone():
            raise ConflictError(
                "The target data home already has jots; import into an empty home"
            )
        commands = None if dry_run else session.commands(context)
        with session.db.write():
            slugs = self._projects(session, commands, snapshot, report)
            for task in self.order(snapshot.tasks):
                record = self.jot(task, snapshot, slugs, report)
                if commands is not None:
                    commands.import_jot(record)
            self._links(commands, snapshot, report)
        return report

    def _projects(
        self,
        session: Session,
        commands: Commands | None,
        snapshot: Snapshot,
        report: ImportReport,
    ) -> dict[int, str]:
        """Create missing projects; return v0.1 project id -> slug."""
        existing = {
            project.slug for project in session.queries.projects(include_archived=True)
        }
        slugs: dict[int, str] = {}
        for project in snapshot.projects:
            slug = self._text(project.get("slug"))
            if not re.match(SLUG, slug):
                report.warnings.append(f"Skipped project with invalid slug {slug!r}")
                continue
            slugs[self._int(project["id"])] = slug
            if slug in existing:
                report.projects_existing.append(slug)
                continue
            report.projects_created.append(slug)
            if commands is None:
                continue
            commands.create_project(
                slug,
                self._text(project.get("name")) or slug,
                self._text(project.get("description")),
            )
            if project.get("archived") == 1:
                commands.update_project(slug, archived=True)
            commands.record(
                "project.imported", {"source": "jot v0.1", "project": project}
            )
        return slugs

    def _links(
        self, commands: Commands | None, snapshot: Snapshot, report: ImportReport
    ) -> None:
        """Turn parent ids into parent links between live jots."""
        live = {
            self._int(task["id"])
            for task in snapshot.tasks
            if task.get("deleted_at") is None
        }
        for task in snapshot.tasks:
            parent = task.get("parent_id")
            if parent is None:
                continue
            child = self._int(task["id"])
            if parent not in live or child not in live or parent == child:
                report.warnings.append(
                    f"Task {child}: parent link to {parent} skipped"
                    " (missing, deleted, or self)"
                )
                continue
            report.links += 1
            if commands is not None:
                commands.link(self._int(parent), child, "parent")
