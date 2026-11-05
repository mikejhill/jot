"""Persistence for plugin settings, cursors, failures, private state, and jobs."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from pydantic import Field, JsonValue, TypeAdapter

from jot.core.models import Clock, Metadata, Record
from jot.db.store import METADATA, Store
from jot.exceptions import ConflictError, NotFoundError
from jot.plugins.api import Job, JobStatus

if TYPE_CHECKING:
    from jot.db.connection import Database

JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
FINISHED = (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED)


class PluginRow(Record):
    """Stored host state for one plugin."""

    id: str
    enabled: bool = False
    settings: Metadata = Field(default_factory=dict)
    cursor: int = 0
    failures: int = 0
    retry_at: float = 0
    last_error: str | None = None


class Failure(Record):
    """An event a plugin gave up on after repeated errors."""

    id: int
    plugin: str
    event_id: int
    attempts: int
    error: str
    ts: str


class PluginStore:
    """SQL for the plugin tables. Callers own the transactions."""

    def __init__(self, database: Database) -> None:
        self.db = database

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the open connection."""
        return self.db.connection

    # Plugin rows -------------------------------------------------------------

    def row(self, plugin_id: str) -> PluginRow:
        """Return a plugin's stored state, or defaults if it was never touched."""
        found = self.connection.execute(
            "SELECT * FROM plugins WHERE id = ?", (plugin_id,)
        ).fetchone()
        if found is None:
            return PluginRow(id=plugin_id)
        return PluginRow(
            id=found["id"],
            enabled=bool(found["enabled"]),
            settings=METADATA.validate_json(found["settings"]),
            cursor=found["cursor"],
            failures=found["failures"],
            retry_at=found["retry_at"],
            last_error=found["last_error"],
        )

    def save(self, row: PluginRow) -> None:
        """Insert or replace a plugin's state."""
        self.connection.execute(
            "INSERT INTO plugins (id, enabled, settings, cursor, failures, retry_at,"
            " last_error, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET enabled = excluded.enabled,"
            " settings = excluded.settings, cursor = excluded.cursor,"
            " failures = excluded.failures, retry_at = excluded.retry_at,"
            " last_error = excluded.last_error, updated_at = excluded.updated_at",
            (
                row.id,
                int(row.enabled),
                Store.dumps(row.settings),
                row.cursor,
                row.failures,
                row.retry_at,
                row.last_error,
                Clock.stamp(),
            ),
        )

    def latest_event(self) -> int:
        """Return the newest event id (0 when there are none)."""
        found = self.connection.execute("SELECT max(id) FROM events").fetchone()
        return int(found[0] or 0)

    def add_failure(
        self, plugin_id: str, event_id: int, attempts: int, error: str
    ) -> None:
        """Record an event a plugin skipped after repeated errors."""
        self.connection.execute(
            "INSERT INTO plugin_failures (plugin, event_id, attempts, error, ts)"
            " VALUES (?, ?, ?, ?, ?)",
            (plugin_id, event_id, attempts, error, Clock.stamp()),
        )

    def failures(self, plugin_id: str | None = None, limit: int = 50) -> list[Failure]:
        """Return skipped events, newest first."""
        sql = "SELECT * FROM plugin_failures"
        args: list[object] = []
        if plugin_id is not None:
            sql += " WHERE plugin = ?"
            args.append(plugin_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [
            Failure.model_validate(dict(found))
            for found in self.connection.execute(sql, args)
        ]

    # Private state -------------------------------------------------------------

    def get_state(self, plugin_id: str, key: str) -> JsonValue:
        """Return a stored value, or None."""
        found = self.connection.execute(
            "SELECT value FROM plugin_state WHERE plugin = ? AND key = ?",
            (plugin_id, key),
        ).fetchone()
        return None if found is None else JSON.validate_json(found["value"])

    def set_state(self, plugin_id: str, key: str, value: JsonValue) -> None:
        """Store a value; None deletes the key."""
        if value is None:
            self.connection.execute(
                "DELETE FROM plugin_state WHERE plugin = ? AND key = ?",
                (plugin_id, key),
            )
            return
        self.connection.execute(
            "INSERT INTO plugin_state (plugin, key, value, updated_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(plugin, key) DO UPDATE SET"
            " value = excluded.value, updated_at = excluded.updated_at",
            (plugin_id, key, json.dumps(value, sort_keys=True), Clock.stamp()),
        )

    def state_keys(self, plugin_id: str) -> list[str]:
        """Return a plugin's keys, sorted."""
        return [
            found["key"]
            for found in self.connection.execute(
                "SELECT key FROM plugin_state WHERE plugin = ? ORDER BY key",
                (plugin_id,),
            )
        ]

    # Jobs ----------------------------------------------------------------------

    @staticmethod
    def job(found: sqlite3.Row) -> Job:
        """Build a job from a row."""
        values = dict(found)
        values["payload"] = METADATA.validate_json(values["payload"])
        values["result"] = (
            None if values["result"] is None else JSON.validate_json(values["result"])
        )
        return Job.model_validate(values)

    def get_job(self, job_id: int) -> Job:
        """Return one job."""
        found = self.connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if found is None:
            raise NotFoundError(f"Job {job_id} not found")
        return self.job(found)

    def jobs(
        self,
        *,
        plugin_id: str | None = None,
        statuses: list[JobStatus] | None = None,
        limit: int = 100,
    ) -> list[Job]:
        """Return jobs, newest first."""
        clauses: list[str] = []
        args: list[object] = []
        if plugin_id is not None:
            clauses.append("plugin = ?")
            args.append(plugin_id)
        if statuses:
            clauses.append(f"status IN ({', '.join('?' * len(statuses))})")
            args.extend(status.value for status in statuses)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(limit)
        rows = self.connection.execute(
            f"SELECT * FROM jobs{where} ORDER BY id DESC LIMIT ?",  # noqa: S608 - fixed clauses
            args,
        )
        return [self.job(found) for found in rows]

    def enqueue(self, job: Job) -> Job:
        """Insert a queued job, or return the live job with the same dedupe key."""
        if job.dedupe_key is not None:
            found = self.connection.execute(
                "SELECT * FROM jobs WHERE plugin = ? AND dedupe_key = ?"
                " AND status IN ('queued', 'running')",
                (job.plugin, job.dedupe_key),
            ).fetchone()
            if found is not None:
                return self.job(found)
        now = Clock.stamp()
        cursor = self.connection.execute(
            "INSERT INTO jobs (plugin, kind, jot_id, payload, dedupe_key, status,"
            " attempts, max_attempts, run_after, correlation_id, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?)",
            (
                job.plugin,
                job.kind,
                job.jot_id,
                Store.dumps(job.payload),
                job.dedupe_key,
                job.max_attempts,
                job.run_after,
                job.correlation_id,
                now,
                now,
            ),
        )
        return self.get_job(int(cursor.lastrowid or 0))

    def claim(self, plugin_ids: list[str], now: float, lease: float) -> Job | None:
        """Lease the oldest ready job (queued, or running with an expired lease)."""
        if not plugin_ids:
            return None
        marks = ", ".join("?" * len(plugin_ids))
        found = self.connection.execute(
            f"SELECT * FROM jobs WHERE plugin IN ({marks}) AND"  # noqa: S608 - placeholders only
            " ((status = 'queued' AND run_after <= ?) OR"
            " (status = 'running' AND lease_until < ?))"
            " ORDER BY run_after, id LIMIT 1",
            [*plugin_ids, now, now],
        ).fetchone()
        if found is None:
            return None
        self.connection.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1,"
            " lease_until = ?, updated_at = ? WHERE id = ?",
            (now + lease, Clock.stamp(), found["id"]),
        )
        return self.get_job(found["id"])

    def heartbeat(self, job_id: int, attempts: int, lease_until: float) -> None:
        """Extend a lease, refusing when the job was reclaimed or finished."""
        cursor = self.connection.execute(
            "UPDATE jobs SET lease_until = ?, updated_at = ? WHERE id = ?"
            " AND status = 'running' AND attempts = ?",
            (lease_until, Clock.stamp(), job_id, attempts),
        )
        if cursor.rowcount == 0:
            raise ConflictError(f"Job {job_id} is no longer leased by this run")

    def finish(
        self,
        job: Job,
        status: JobStatus,
        *,
        result: JsonValue = None,
        error: str | None = None,
        run_after: float | None = None,
    ) -> bool:
        """Record a run's outcome if this run still holds the job's lease."""
        cursor = self.connection.execute(
            "UPDATE jobs SET status = ?, result = ?, error = ?, lease_until = NULL,"
            " run_after = coalesce(?, run_after), updated_at = ?"
            " WHERE id = ? AND status = 'running' AND attempts = ?",
            (
                status.value,
                None if result is None else json.dumps(result, sort_keys=True),
                error,
                run_after,
                Clock.stamp(),
                job.id,
                job.attempts,
            ),
        )
        return cursor.rowcount == 1

    def set_job_status(self, job_id: int, status: JobStatus, run_after: float) -> Job:
        """Cancel or requeue a job by hand."""
        job = self.get_job(job_id)
        if status == JobStatus.CANCELLED and job.status in FINISHED:
            raise ConflictError(f"Job {job_id} already finished ({job.status})")
        if status == JobStatus.QUEUED and job.status not in FINISHED:
            raise ConflictError(f"Job {job_id} is {job.status}, not finished")
        try:
            self._set_status(job_id, status, run_after)
        except sqlite3.IntegrityError as err:
            raise ConflictError(
                f"Job {job_id} has a live duplicate ({job.dedupe_key})"
            ) from err
        return self.get_job(job_id)

    def _set_status(self, job_id: int, status: JobStatus, run_after: float) -> None:
        """Write a manual status change."""
        self.connection.execute(
            "UPDATE jobs SET status = ?, run_after = ?, lease_until = NULL,"
            " attempts = CASE WHEN ? = 'queued' THEN 0 ELSE attempts END,"
            " error = CASE WHEN ? = 'queued' THEN NULL ELSE error END,"
            " updated_at = ? WHERE id = ?",
            (
                status.value,
                run_after,
                status.value,
                status.value,
                Clock.stamp(),
                job_id,
            ),
        )
