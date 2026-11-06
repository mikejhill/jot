"""Discover plugins, deliver the change feed to them, and run their jobs.

Delivery is ordered and durable. Writers serialize on ``BEGIN IMMEDIATE``, so
event ids commit in increasing order and a per-plugin cursor never skips an
event. Each event is handled inside the write transaction that advances the
cursor: a plugin's writes and its progress commit together, and a failed
handler leaves neither behind. Failures back off and, after ``max_failures``
tries, the event is recorded in ``plugin_failures`` and skipped.

Jobs run outside any transaction under a lease. A run that outlives its lease
can be reclaimed by another worker; only the run holding the current lease
can record an outcome.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError

from jot.app.session import Session
from jot.core.models import Event, Metadata, Record, WriteContext
from jot.exceptions import AppError, NotFoundError, PluginError
from jot.plugins.api import Job, JobStatus, Plugin
from jot.plugins.context import PluginContext
from jot.plugins.store import Failure, PluginRow, PluginStore

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from jot.config import JotHome

ENTRY_POINT_GROUP = "jot.plugins"
MAX_BACKOFF = 300.0
JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
log = logging.getLogger("jot.plugins")


@dataclass(frozen=True, slots=True)
class PluginEntry:
    """A discovered plugin class, or the reason it could not be loaded."""

    id: str
    source: str
    plugin: type[Plugin] | None = None
    error: str | None = None


class PluginCatalog:
    """The plugins this process can run, keyed by id."""

    def __init__(self, entries: Iterable[PluginEntry] = ()) -> None:
        self.entries: dict[str, PluginEntry] = {}
        for entry in entries:
            self.entries[entry.id] = (
                PluginEntry(
                    entry.id, entry.source, error=f"duplicate plugin id {entry.id!r}"
                )
                if entry.id in self.entries
                else entry
            )

    @classmethod
    def discover(cls) -> PluginCatalog:
        """Load every ``jot.plugins`` entry point, keeping failures as entries."""
        found: list[PluginEntry] = []
        for point in entry_points(group=ENTRY_POINT_GROUP):
            try:
                loaded = point.load()
            except Exception as err:  # noqa: BLE001 - isolate broken packages
                found.append(PluginEntry(point.name, point.value, error=repr(err)))
                continue
            found.append(cls.entry(loaded, point.value, point.name))
        return cls(found)

    @classmethod
    def of(cls, *plugins: type[Plugin]) -> PluginCatalog:
        """Build a catalog from classes directly (tests and embedding)."""
        return cls(cls.entry(plugin, plugin.__module__) for plugin in plugins)

    @staticmethod
    def entry(loaded: object, source: str, name: str | None = None) -> PluginEntry:
        """Validate a loaded object as a plugin class."""
        if not (isinstance(loaded, type) and issubclass(loaded, Plugin)):
            return PluginEntry(name or source, source, error="not a Plugin subclass")
        try:
            loaded.check()
        except ValueError as err:
            return PluginEntry(name or loaded.id or source, source, error=str(err))
        return PluginEntry(loaded.id, source, plugin=loaded)

    def get(self, plugin_id: str) -> PluginEntry:
        """Return an entry."""
        try:
            return self.entries[plugin_id]
        except KeyError:
            raise NotFoundError(f"Plugin {plugin_id!r} is not installed") from None

    def load(self, plugin_id: str) -> type[Plugin]:
        """Return a runnable plugin class."""
        entry = self.get(plugin_id)
        if entry.plugin is None:
            raise PluginError(f"Plugin {plugin_id!r} is broken: {entry.error}")
        return entry.plugin


class PluginStatus(Record):
    """A plugin's description and host state, as clients see it."""

    id: str
    name: str = ""
    version: str = ""
    description: str = ""
    source: str = ""
    error: str | None = None
    enabled: bool = False
    events: list[str] = Field(default_factory=list)
    settings: Metadata = Field(default_factory=dict)
    settings_schema: Metadata = Field(default_factory=dict)
    has_ui: bool = False
    provides: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    cursor: int = 0
    lag: int = 0
    failures: int = 0
    retry_at: float = 0
    last_error: str | None = None


class TickResult(Record):
    """What one pass of the host did."""

    delivered: int = 0
    jobs: int = 0


class PluginHost:
    """Plugin administration, event delivery, and job execution for one session."""

    def __init__(
        self,
        session: Session,
        catalog: PluginCatalog,
        *,
        clock: Callable[[], float] = time.time,
        lease: float = 60.0,
        max_failures: int = 5,
    ) -> None:
        self.session = session
        self.catalog = catalog
        self.store = PluginStore(session.db)
        self.clock = clock
        self.lease = lease
        self.max_failures = max_failures
        self._instances: dict[str, Plugin] = {}

    # Administration -------------------------------------------------------------

    def instance(self, plugin_id: str) -> Plugin:
        """Return the (cached) plugin object."""
        if plugin_id not in self._instances:
            self._instances[plugin_id] = self.catalog.load(plugin_id)()
        return self._instances[plugin_id]

    @staticmethod
    def _listens(entry: PluginEntry) -> bool:
        """Return whether a plugin reads the change feed."""
        return entry.plugin is None or entry.plugin.listens

    def statuses(self) -> list[PluginStatus]:
        """Return every installed plugin, sorted by id."""
        return [self.status(plugin_id) for plugin_id in sorted(self.catalog.entries)]

    def status(self, plugin_id: str) -> PluginStatus:
        """Return one plugin's description and state."""
        entry = self.catalog.get(plugin_id)
        row = self.store.row(plugin_id)
        lag = self.store.connection.execute(
            "SELECT count(*) FROM events WHERE id > ?", (row.cursor,)
        ).fetchone()[0]
        status = PluginStatus(
            id=plugin_id,
            source=entry.source,
            error=entry.error,
            enabled=row.enabled,
            settings=row.settings,
            cursor=row.cursor,
            lag=lag if row.enabled and self._listens(entry) else 0,
            failures=row.failures,
            retry_at=row.retry_at,
            last_error=row.last_error,
        )
        if entry.plugin is None:
            return status
        plugin = entry.plugin
        return status.model_copy(
            update={
                "name": plugin.name or plugin.id,
                "version": plugin.version,
                "description": plugin.description,
                "events": sorted(plugin.events),
                "settings_schema": plugin.settings_model.model_json_schema(),
                "has_ui": self.instance(plugin_id).ui_module() is not None,
                "provides": sorted(plugin.provides),
                "requires": sorted(plugin.requires),
                "missing": self.missing(plugin),
            }
        )

    def enable(
        self, plugin_id: str, context: WriteContext, *, from_start: bool = False
    ) -> PluginStatus:
        """Enable a plugin. Delivery starts after the newest event unless from_start."""
        plugin = self.catalog.load(plugin_id)
        with self.session.db.write():
            row = self.store.row(plugin_id)
            self._validated(plugin, row.settings)
            if missing := self.missing(plugin):
                raise PluginError(self._missing_message(plugin_id, missing))
            if not row.enabled:
                cursor = 0 if from_start else self.store.latest_event()
                self.store.save(
                    row.model_copy(
                        update={
                            "enabled": True,
                            "cursor": cursor,
                            "failures": 0,
                            "retry_at": 0,
                            "last_error": None,
                        }
                    )
                )
                self.session.commands(context).record(
                    "plugin.enabled", {"plugin": plugin_id, "cursor": cursor}
                )
        return self.status(plugin_id)

    def disable(self, plugin_id: str, context: WriteContext) -> PluginStatus:
        """Stop delivering events and running jobs for a plugin."""
        self.catalog.get(plugin_id)
        with self.session.db.write():
            row = self.store.row(plugin_id)
            if dependents := self.dependents(plugin_id):
                raise PluginError(
                    f"Disable {', '.join(dependents)} first: they need {plugin_id}"
                )
            if row.enabled:
                self.store.save(row.model_copy(update={"enabled": False}))
                self.session.commands(context).record(
                    "plugin.disabled", {"plugin": plugin_id}
                )
        return self.status(plugin_id)

    def configure(
        self, plugin_id: str, values: Metadata, context: WriteContext
    ) -> PluginStatus:
        """Replace a plugin's settings after validating them."""
        plugin = self.catalog.load(plugin_id)
        settings = self._validated(plugin, values).model_dump(mode="json")
        with self.session.db.write():
            row = self.store.row(plugin_id)
            self.store.save(
                row.model_copy(
                    update={"settings": settings, "failures": 0, "retry_at": 0}
                )
            )
            self.session.commands(context).record(
                "plugin.configured", {"plugin": plugin_id, "settings": settings}
            )
        return self.status(plugin_id)

    def settings(self, plugin_id: str) -> BaseModel:
        """Return a plugin's validated settings."""
        plugin = self.catalog.load(plugin_id)
        return self._validated(plugin, self.store.row(plugin_id).settings)

    @staticmethod
    def _validated(plugin: type[Plugin], values: Metadata) -> BaseModel:
        """Validate settings against the plugin's model."""
        try:
            return plugin.settings_model.model_validate(values)
        except ValidationError as err:
            raise PluginError(f"Invalid settings for {plugin.id}: {err}") from err

    def failures(self, plugin_id: str | None = None) -> list[Failure]:
        """Return events plugins skipped after repeated errors."""
        return self.store.failures(plugin_id)

    # Services -------------------------------------------------------------------

    def providers(self, name: str, *, exclude: str | None = None) -> list[str]:
        """Return enabled plugins that provide a service, sorted by id."""
        return [
            plugin_id
            for plugin_id in self.enabled()
            if plugin_id != exclude and name in self.catalog.load(plugin_id).provides
        ]

    def services(self, name: str) -> list[object]:
        """Return the service objects of every enabled provider."""
        return [
            self.instance(plugin_id).service(name, self.context_for(plugin_id))
            for plugin_id in self.providers(name)
        ]

    def missing(self, plugin: type[Plugin]) -> list[str]:
        """Return required services no enabled plugin (other than this) provides."""
        return sorted(
            name
            for name in plugin.requires
            if not self.providers(name, exclude=plugin.id)
        )

    def dependents(self, plugin_id: str) -> list[str]:
        """Return enabled plugins that would lose a required service."""
        provided = (
            self.catalog.load(plugin_id).provides
            if (self.catalog.get(plugin_id).plugin is not None)
            else frozenset[str]()
        )
        found: list[str] = []
        for other in self.enabled():
            if other == plugin_id:
                continue
            needs = self.catalog.load(other).requires & provided
            if any(not self.providers(n, exclude=plugin_id) for n in needs):
                found.append(other)
        return found

    def _missing_message(self, plugin_id: str, missing: list[str]) -> str:
        """Explain which plugins could satisfy missing services."""
        parts = []
        for name in missing:
            candidates = sorted(
                other
                for other, entry in self.catalog.entries.items()
                if entry.plugin is not None and name in entry.plugin.provides
            )
            hint = f" (enable {' or '.join(candidates)})" if candidates else ""
            parts.append(f"{name}{hint}")
        return f"{plugin_id} requires {', '.join(parts)}"

    def action(
        self, plugin_id: str, name: str, body: Metadata, context: WriteContext
    ) -> JsonValue:
        """Run a client action on an enabled plugin."""
        if not self.status(plugin_id).enabled:
            raise PluginError(f"Plugin {plugin_id} is not enabled")
        result = self.instance(plugin_id).action(name, body, self, context)
        return JSON.validate_python(result)

    def context_for(
        self,
        plugin_id: str,
        *,
        correlation_id: str | None = None,
        job: Job | None = None,
    ) -> PluginContext:
        """Build the context a plugin's code runs with."""
        return PluginContext(
            self.session,
            self.store,
            plugin_id,
            self.settings(plugin_id),
            correlation_id=correlation_id,
            clock=self.clock,
            job=job,
            lease=self.lease,
            resolver=self.services,
        )

    def enabled(self) -> list[str]:
        """Return the ids of enabled plugins that can run."""
        rows = self.store.connection.execute(
            "SELECT id FROM plugins WHERE enabled = 1 ORDER BY id"
        )
        return [
            row["id"]
            for row in rows
            if row["id"] in self.catalog.entries
            and self.catalog.entries[row["id"]].plugin is not None
        ]

    # Delivery -------------------------------------------------------------------

    def deliver(self, limit: int = 100) -> int:
        """Process up to ``limit`` events per enabled plugin; return the count."""
        total = 0
        for plugin_id in self.enabled():
            if not self.catalog.load(plugin_id).listens:
                continue
            for _ in range(limit):
                if not self._deliver_one(plugin_id):
                    break
                total += 1
        return total

    def _deliver_one(self, plugin_id: str) -> bool:
        """Handle the next event for one plugin. Return False when there is none."""
        with self.session.db.write() as conn:
            row = self.store.row(plugin_id)
            if not row.enabled or row.retry_at > self.clock():
                return False
            events = self.session.queries.events(row.cursor, 1)
            if not events:
                return False
            event = events[0]
            conn.execute("SAVEPOINT plugin_event")
            try:
                self._handle(plugin_id, event)
            except Exception as err:  # noqa: BLE001 - plugin code is isolated
                conn.execute("ROLLBACK TO plugin_event")
                conn.execute("RELEASE plugin_event")
                self._failed(row, event, err)
                return True
            conn.execute("RELEASE plugin_event")
            self.store.save(
                row.model_copy(
                    update={
                        "cursor": event.id,
                        "failures": 0,
                        "retry_at": 0,
                        "last_error": None,
                    }
                )
            )
            return True

    def _handle(self, plugin_id: str, event: Event) -> None:
        """Filter an event and pass it to the plugin."""
        plugin = self.instance(plugin_id)
        if plugin.events and event.kind not in plugin.events:
            return
        context = self.context_for(plugin_id, correlation_id=event.correlation_id)
        if plugin.skip_own and event.actor == context.actor:
            return
        if event.jot_id is not None:
            try:
                jot = self.session.queries.get(event.jot_id, include_deleted=True)
            except NotFoundError:
                return
            profile = self.session.profiles.get(jot.profile)
            if profile.plugins and plugin_id not in profile.plugins:
                return
            if not plugin.applies_to(jot):
                return
        plugin.handle(event, context)

    def _failed(self, row: PluginRow, event: Event, err: Exception) -> None:
        """Back off, or skip the event once it has failed too often."""
        failures = row.failures + 1
        message = f"event {event.id}: {err!r}"
        log.warning("plugin %s failed on %s", row.id, message)
        if failures >= self.max_failures:
            self.store.add_failure(row.id, event.id, failures, repr(err))
            update: dict[str, object] = {
                "cursor": event.id,
                "failures": 0,
                "retry_at": 0,
                "last_error": message,
            }
        else:
            update = {
                "failures": failures,
                "retry_at": self.clock() + self.backoff(failures),
                "last_error": message,
            }
        self.store.save(row.model_copy(update=update))

    @staticmethod
    def backoff(attempt: int) -> float:
        """Return the retry delay after ``attempt`` failures."""
        return min(2.0**attempt, MAX_BACKOFF)

    # Jobs -----------------------------------------------------------------------

    def jobs(
        self,
        *,
        plugin_id: str | None = None,
        statuses: list[JobStatus] | None = None,
        limit: int = 100,
    ) -> list[Job]:
        """Return jobs, newest first."""
        return self.store.jobs(plugin_id=plugin_id, statuses=statuses, limit=limit)

    def cancel(self, job_id: int) -> Job:
        """Cancel a queued or running job."""
        with self.session.db.write():
            return self.store.set_job_status(job_id, JobStatus.CANCELLED, self.clock())

    def retry(self, job_id: int) -> Job:
        """Requeue a finished job with fresh attempts."""
        with self.session.db.write():
            return self.store.set_job_status(job_id, JobStatus.QUEUED, self.clock())

    def run_jobs(self, limit: int = 10) -> int:
        """Run up to ``limit`` ready jobs; return how many ran."""
        ran = 0
        while ran < limit:
            enabled = self.enabled()
            with self.session.db.write():
                job = self.store.claim(enabled, self.clock(), self.lease)
            if job is None:
                break
            self._run(job)
            ran += 1
        return ran

    def _run(self, job: Job) -> None:
        """Run one claimed job and record its outcome."""
        try:
            context = self.context_for(
                job.plugin, correlation_id=job.correlation_id, job=job
            )
            result = JSON.validate_python(self.instance(job.plugin).run(job, context))
        except Exception as err:  # noqa: BLE001 - plugin code is isolated
            log.warning("job %s (%s) failed: %r", job.id, job.kind, err)
            with self.session.db.write():
                if job.attempts >= job.max_attempts:
                    self.store.finish(job, JobStatus.FAILED, error=repr(err))
                else:
                    self.store.finish(
                        job,
                        JobStatus.QUEUED,
                        error=repr(err),
                        run_after=self.clock() + self.backoff(job.attempts),
                    )
            return
        with self.session.db.write():
            self.store.finish(job, JobStatus.DONE, result=result)

    def tick(self) -> TickResult:
        """Deliver pending events, then run ready jobs."""
        return TickResult(delivered=self.deliver(), jobs=self.run_jobs())


@dataclass
class Worker:
    """Run host ticks on a background thread until stopped."""

    home: JotHome
    catalog: PluginCatalog
    interval: float = 1.0
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def tick(self) -> TickResult:
        """Run one pass in a fresh session; errors are logged, not raised."""
        try:
            with Session.open(self.home) as session:
                return PluginHost(session, self.catalog).tick()
        except (AppError, sqlite3.Error) as err:
            log.warning("plugin worker pass failed: %s", err)
            return TickResult()

    def run(self, *, once: bool = False) -> TickResult:
        """Tick until stopped (or once); return the last pass's result."""
        result = self.tick()
        while not once and not self._stop.wait(self.interval):
            result = self.tick()
        return result

    def start(self) -> None:
        """Start ticking in a daemon thread."""
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run, name="jot-plugins", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Ask the loop to stop and wait for the current pass."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
