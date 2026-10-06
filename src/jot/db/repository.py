"""Typed repositories with atomic auditing and label maintenance."""

from __future__ import annotations

import builtins
import json
import sqlite3
from collections.abc import Iterable
from typing import ClassVar, override

from jot.core.models import (
    Clock,
    EventBody,
    EventKind,
    Project,
    Record,
    Run,
    Status,
    Task,
    TaskEvent,
)
from jot.db.connection import Database
from jot.db.query import TASK_SELECT, QueryBuilder, TaskQuery
from jot.exceptions import NotFoundError, RepositoryError

type SqlValue = str | int | float | None


class Repository[T: Record]:
    """Validated CRUD shared by fixed-schema repositories."""

    table: ClassVar[str]
    json_fields: ClassVar[tuple[str, ...]] = ()

    def __init__(self, database: Database, model: type[T]) -> None:
        self.db = database
        self._model = model

    def _decode(self, row: sqlite3.Row) -> T:
        """Parse SQLite data into a validated record."""
        payload = dict(row)
        for name in self.json_fields:
            payload[name] = json.loads(payload[name])
        return self._model.model_validate(payload)

    @staticmethod
    def _value(value: object) -> SqlValue:
        """Encode JSON structures and scalar values for SQLite."""
        if value is None or isinstance(value, (str, int, float)):
            return value
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        raise RepositoryError(f"Unsupported database value: {value!r}")

    def create(self, record: T) -> T:
        """Insert a validated record and return its assigned id."""
        payload = record.model_dump(mode="json", exclude={"id", "labels"})
        columns = ",".join(payload)
        placeholders = ",".join("?" for _ in payload)
        # Identifiers come only from fixed tables and validated model fields.
        sql = f"INSERT INTO {self.table} ({columns}) VALUES ({placeholders})"  # noqa: S608 - fixed schema identifiers
        with self.db.write() as connection:
            cursor = connection.execute(sql, [self._value(v) for v in payload.values()])
            if cursor.lastrowid is None:
                raise RepositoryError("Insert returned no id")
            return self.get(cursor.lastrowid)

    def get(self, record_id: int) -> T:
        """Return an existing record or raise NotFoundError."""
        sql = f"SELECT * FROM {self.table} WHERE id=?"  # noqa: S608 - fixed schema table
        row = self.db.connection.execute(sql, (record_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"{self.table} id {record_id} not found")
        return self._decode(row)

    def list(self) -> builtins.list[T]:
        """Return records in insertion order."""
        sql = f"SELECT * FROM {self.table} ORDER BY id"  # noqa: S608 - fixed schema table
        return [self._decode(row) for row in self.db.connection.execute(sql)]

    def update(self, record_id: int, changes: dict[str, object]) -> T:
        """Validate and persist changed fields; ids are immutable."""
        if "id" in changes:
            raise RepositoryError("id is immutable")
        current = self.get(record_id)
        record = self._model.model_validate(current.model_dump() | changes)
        payload = record.model_dump(mode="json", exclude={"id", "labels"})
        assignments = ",".join(name + "=?" for name in payload)
        sql = f"UPDATE {self.table} SET {assignments} WHERE id=?"  # noqa: S608 - model fields and fixed table
        with self.db.write() as connection:
            connection.execute(
                sql, [self._value(v) for v in payload.values()] + [record_id]
            )
        return self.get(record_id)

    def delete(self, record_id: int) -> None:
        """Delete an existing record, respecting foreign keys."""
        self.get(record_id)
        sql = f"DELETE FROM {self.table} WHERE id=?"  # noqa: S608 - fixed schema table
        with self.db.write() as connection:
            connection.execute(sql, (record_id,))


class ProjectRepository(Repository[Project]):
    """Project CRUD and slug resolution."""

    table = "projects"
    json_fields = ("aliases",)

    def __init__(self, database: Database) -> None:
        super().__init__(database, Project)

    def by_slug(self, slug: str) -> Project:
        """Resolve a project slug or fail explicitly."""
        row = self.db.connection.execute(
            "SELECT * FROM projects WHERE slug=?", (slug,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Project {slug!r} not found")
        return self._decode(row)


class EventRepository(Repository[TaskEvent]):
    """Task audit history; mutations are intentionally forbidden."""

    table = "task_events"
    json_fields = ("body",)

    def __init__(self, database: Database) -> None:
        super().__init__(database, TaskEvent)

    def for_task(self, task_id: int) -> builtins.list[TaskEvent]:
        """Return a task's chronological history."""
        return [
            self._decode(row)
            for row in self.db.connection.execute(
                "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,)
            )
        ]

    @override
    def update(self, record_id: int, changes: dict[str, object]) -> TaskEvent:
        """Reject editing append-only history."""
        raise RepositoryError(
            f"Event {record_id} is immutable; append {len(changes)} fields instead"
        )

    @override
    def delete(self, record_id: int) -> None:
        """Reject deleting append-only history."""
        raise RepositoryError(f"Event {record_id} is immutable")


class RunRepository(Repository[Run]):
    """CRUD for run metadata."""

    table = "runs"

    def __init__(self, database: Database) -> None:
        super().__init__(database, Run)

    @override
    def create(self, record: Run) -> Run:
        """Create run metadata and append its initial status to task history."""
        with self.db.write():
            run = super().create(record)
            self._audit(run, "created")
        return run

    @override
    def update(self, record_id: int, changes: dict[str, object]) -> Run:
        """Audit updates to run metadata in the same transaction."""
        if "task_id" in changes:
            raise RepositoryError("A run's task_id is immutable")
        with self.db.write():
            run = super().update(record_id, changes)
            self._audit(run, "updated")
        return run

    @override
    def delete(self, record_id: int) -> None:
        """Delete run metadata while preserving the task's audit history."""
        with self.db.write():
            run = self.get(record_id)
            super().delete(record_id)
            self._audit(run, "deleted")

    def _audit(self, run: Run, action: str) -> None:
        """Append the run's status and mutation reason."""
        EventRepository(self.db).create(
            TaskEvent(
                task_id=run.task_id,
                actor="cli",
                kind=EventKind.RUN_LOG,
                body={"run_id": run.id, "status": run.status, "action": action},
            )
        )


class TaskRepository(Repository[Task]):
    """Task CRUD, query filtering, and label auditing."""

    table = "tasks"
    json_fields = ("labels",)

    def __init__(self, database: Database) -> None:
        super().__init__(database, Task)
        self.events = EventRepository(database)

    @override
    def get(self, record_id: int) -> Task:
        """Return a task including its sorted labels."""
        row = self.db.connection.execute(
            TASK_SELECT + " WHERE t.id=?", (record_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Task {record_id} not found")
        return self._decode(row)

    @override
    def create(self, record: Task, *, prefilled: Iterable[str] = ()) -> Task:
        """Capture an inbox task and atomically append its creation event.

        ``prefilled`` names fields the owner set explicitly at capture time
        (e.g. ``criticality``); enrichment keeps them instead of overwriting.
        """
        if record.status != Status.INBOX or record.claimed_by is not None:
            raise RepositoryError("New tasks must be unclaimed inbox captures")
        body: EventBody = {"source": record.source}
        if fields := ",".join(sorted(set(prefilled))):
            body["prefilled"] = fields
        with self.db.write():
            task = super().create(record)
            self.audit(task.id, EventKind.CREATED, body)
            for label in record.labels:
                self.label(task.id, label)
        return self.get(task.id)

    def query(self, query: TaskQuery | None = None) -> builtins.list[Task]:
        """Return tasks matching validated, parameterized filters."""
        sql, values = QueryBuilder.build(query or TaskQuery())
        try:
            return [
                self._decode(row) for row in self.db.connection.execute(sql, values)
            ]
        except sqlite3.OperationalError as err:
            raise RepositoryError(f"Invalid task query: {err}") from err

    @override
    def list(self) -> builtins.list[Task]:
        """Return non-deleted tasks in id order."""
        return self.query()

    @override
    def update(self, record_id: int, changes: dict[str, object]) -> Task:
        """Edit content without bypassing the workflow or lease authority."""
        allowed = {
            "title",
            "description",
            "raw_input",
            "project_id",
            "type",
            "criticality",
            "flow",
            "repo_path",
            "due_at",
            "parent_id",
            "needs_enrichment",
        }
        if changes.keys() - allowed:
            raise RepositoryError(
                "Only content fields are editable; use workflow for state"
            )
        with self.db.write():
            task = self.get(record_id)
            if task.claimed_by is not None:
                raise RepositoryError("Release the task before editing")
            result = super().update(record_id, changes | {"updated_at": Clock.stamp()})
            self.audit(
                record_id,
                EventKind.ENRICHED,
                {"action": "edit", "fields": ",".join(sorted(changes))},
            )
        return result

    @override
    def delete(self, record_id: int) -> None:
        """Soft-delete a task and retain its audit trail."""
        with self.db.write():
            if self.get(record_id).claimed_by is not None:
                raise RepositoryError("Release the task before deleting")
            now = Clock.stamp()
            self.db.connection.execute(
                "UPDATE tasks SET deleted_at=?,updated_at=? WHERE id=?",
                (now, now, record_id),
            )
            self.audit(record_id, EventKind.STATUS, {"action": "soft_delete"})

    def label(self, task_id: int, name: str, *, remove: bool = False) -> Task:
        """Add or remove a label, refreshing FTS through triggers."""
        name = name.strip().lower()
        if not name:
            raise RepositoryError("Label cannot be empty")
        with self.db.write() as connection:
            self.get(task_id)
            if remove:
                connection.execute(
                    "DELETE FROM task_labels WHERE task_id=? AND label_id IN "
                    "(SELECT id FROM labels WHERE name=?)",
                    (task_id, name),
                )
            else:
                connection.execute(
                    "INSERT INTO labels(name) VALUES(?) ON CONFLICT DO NOTHING", (name,)
                )
                connection.execute(
                    "INSERT INTO task_labels(task_id,label_id) "
                    "SELECT ?,id FROM labels WHERE name=? ON CONFLICT DO NOTHING",
                    (task_id, name),
                )
            connection.execute(
                "UPDATE tasks SET updated_at=? WHERE id=?", (Clock.stamp(), task_id)
            )
            self.audit(
                task_id,
                EventKind.ENRICHED,
                {"action": "label_remove" if remove else "label_add", "label": name},
            )
        return self.get(task_id)

    def audit(
        self,
        task_id: int,
        kind: EventKind,
        body: EventBody,
        *,
        actor: str = "cli",
    ) -> TaskEvent:
        """Append an event within the caller's transaction."""
        return self.events.create(
            TaskEvent(task_id=task_id, actor=actor, kind=kind, body=body)
        )
