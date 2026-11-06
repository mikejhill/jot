"""The contract between Jot and its plugins.

A plugin is a class that subclasses ``Plugin`` and is published under the
``jot.plugins`` entry-point group. The host hands it events from the change
feed and runs the jobs it queues, always through a ``PluginContext`` whose
writes are attributed to ``plugin:<id>`` with origin ``plugin``.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from jot.core.models import NAME, Clock, Metadata, Record
from jot.exceptions import NotFoundError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractContextManager

    import typer

    from jot.core.models import Event, Jot, WriteContext
    from jot.plugins.context import PluginContext
    from jot.plugins.host import PluginHost

API_VERSION = 1
# A service is a dotted name and a major version, e.g. ``harness@1``.
SERVICE = re.compile(r"^[a-z][a-z0-9_.-]*@[1-9][0-9]*$")


class NoSettings(BaseModel):
    """Settings model for plugins that have none."""

    model_config = ConfigDict(extra="forbid")


class JobStatus(StrEnum):
    """Lifecycle of a queued job."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(Record):
    """One unit of background work owned by a plugin."""

    id: int = 0
    plugin: str
    kind: str
    jot_id: int | None = None
    payload: Metadata = Field(default_factory=dict)
    dedupe_key: str | None = None
    status: JobStatus = JobStatus.QUEUED
    attempts: int = 0
    max_attempts: int = 3
    run_after: float = 0
    lease_until: float | None = None
    result: JsonValue = None
    error: str | None = None
    correlation_id: str
    created_at: datetime = Field(default_factory=Clock.now)
    updated_at: datetime = Field(default_factory=Clock.now)


class PluginCli(Protocol):
    """What the command line lends a plugin's own commands."""

    def host(self, *, json: bool) -> AbstractContextManager[PluginHost]:
        """Open the data home and bind a plugin host (errors become exit codes)."""
        ...

    def emit(self, value: BaseModel | Sequence[BaseModel], text: str) -> None:
        """Write ``value`` as JSON under ``--json``, else ``text``."""
        ...


class Plugin:
    """Base class for plugins. Override the hooks you need.

    Class attributes describe the plugin. ``events`` limits which event kinds
    are delivered (empty means all); ``listens = False`` skips the change feed
    entirely, for plugins that only provide services. ``settings_model``
    validates settings; its JSON schema is shown to clients. ``handle`` runs
    inside the write transaction that advances the plugin's cursor, so its
    writes and the cursor commit together: keep it fast and queue slow work
    as jobs.
    """

    id: ClassVar[str] = ""
    name: ClassVar[str] = ""
    version: ClassVar[str] = "0"
    description: ClassVar[str] = ""
    api: ClassVar[int] = API_VERSION
    events: ClassVar[frozenset[str]] = frozenset[str]()
    skip_own: ClassVar[bool] = True
    listens: ClassVar[bool] = True
    command: ClassVar[str] = ""  # name of the CLI group; empty = the plugin id
    settings_model: ClassVar[type[BaseModel]] = NoSettings
    provides: ClassVar[frozenset[str]] = frozenset[str]()
    requires: ClassVar[frozenset[str]] = frozenset[str]()

    def handle(self, event: Event, context: PluginContext) -> None:
        """React to one event from the change feed."""

    def run(self, job: Job, context: PluginContext) -> JsonValue:
        """Run one job; the return value is stored as the job's result."""
        raise NotImplementedError(f"{self.id} has no job kind {job.kind!r}")

    def applies_to(self, jot: Jot) -> bool:
        """Return whether this plugin acts on a jot; the host also checks profiles."""
        return jot.deleted_at is None

    def action(
        self, name: str, body: Metadata, host: PluginHost, context: WriteContext
    ) -> JsonValue:
        """Handle ``POST /api/plugins/<id>/actions/<name>`` for clients.

        ``context`` attributes the request (actor, origin). Raise ``AppError``
        subclasses for bad input; the host maps them to HTTP statuses.
        """
        del body, host, context
        raise NotFoundError(f"Plugin {self.id} has no action {name!r}")

    def service(self, name: str, context: PluginContext) -> object:
        """Return the object behind a service named in ``provides``."""
        raise NotImplementedError(f"{self.id} does not provide {name!r}")

    @classmethod
    def commands(cls, cli: PluginCli) -> typer.Typer | None:
        """Return a Typer app mounted as ``jot <command or id> ...``, if any."""
        del cli
        return None

    def ui_module(self) -> str | None:
        """Return the source of an ES module for the web UI, if any.

        The module may ``export default function setup(jot)``; ``jot`` is the
        UI's extension API (``register``, ``html``, ``api``).
        """
        return None

    @classmethod
    def check(cls) -> None:
        """Raise ``ValueError`` when the class does not meet the contract."""
        if not NAME.match(cls.id):
            raise ValueError(f"invalid plugin id {cls.id!r}")
        for name in cls.provides | cls.requires:
            if not SERVICE.match(name):
                raise ValueError(f"plugin {cls.id}: invalid service name {name!r}")
        if cls.api != API_VERSION:
            raise ValueError(
                f"plugin {cls.id} targets API {cls.api}, host provides {API_VERSION}"
            )
