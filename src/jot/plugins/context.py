"""What a plugin sees while it handles an event or runs a job."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, JsonValue

from jot.core.models import Metadata, Origin, WriteContext
from jot.exceptions import PluginError
from jot.plugins.api import Job

if TYPE_CHECKING:
    from collections.abc import Callable

    from jot.app.commands import Commands
    from jot.app.queries import Queries
    from jot.app.session import Session
    from jot.plugins.store import PluginStore

DEFAULT_ATTEMPTS = 3


class PluginState:
    """A plugin's private key-value storage (JSON values)."""

    def __init__(self, store: PluginStore, plugin_id: str) -> None:
        self.store = store
        self.plugin_id = plugin_id

    def get(self, key: str, default: JsonValue = None) -> JsonValue:
        """Return a value, or ``default`` when unset."""
        value = self.store.get_state(self.plugin_id, key)
        return default if value is None else value

    def set(self, key: str, value: JsonValue) -> None:
        """Store a value; None deletes it."""
        with self.store.db.write():
            self.store.set_state(self.plugin_id, key, value)

    def keys(self) -> list[str]:
        """Return every key, sorted."""
        return self.store.state_keys(self.plugin_id)


class PluginContext:
    """Attributed commands, queries, settings, storage, and job control."""

    def __init__(  # noqa: PLR0913 - everything a plugin may use
        self,
        session: Session,
        store: PluginStore,
        plugin_id: str,
        settings: BaseModel,
        *,
        correlation_id: str | None = None,
        clock: Callable[[], float],
        job: Job | None = None,
        lease: float = 0,
    ) -> None:
        self.session = session
        self.store = store
        self.plugin_id = plugin_id
        self.settings = settings
        self.correlation_id = correlation_id
        self.clock = clock
        self.job = job
        self.lease = lease
        self.state = PluginState(store, plugin_id)

    @property
    def actor(self) -> str:
        """Return the actor every write from this plugin carries."""
        return f"plugin:{self.plugin_id}"

    @property
    def queries(self) -> Queries:
        """Return read access to jots, comments, links, and events."""
        return self.session.queries

    def commands(self, reason: str | None = None) -> Commands:
        """Return commands attributed to this plugin, in the triggering correlation."""
        extra = {"correlation_id": self.correlation_id} if self.correlation_id else {}
        return self.session.commands(
            WriteContext(actor=self.actor, origin=Origin.PLUGIN, reason=reason, **extra)
        )

    def enqueue(  # noqa: PLR0913 - one keyword per job attribute
        self,
        kind: str,
        payload: Metadata | None = None,
        *,
        jot_id: int | None = None,
        delay: float = 0,
        dedupe_key: str | None = None,
        max_attempts: int = DEFAULT_ATTEMPTS,
    ) -> Job:
        """Queue a job for this plugin; a live job with the same key is reused."""
        job = Job(
            plugin=self.plugin_id,
            kind=kind,
            jot_id=jot_id,
            payload=payload or {},
            dedupe_key=dedupe_key,
            max_attempts=max_attempts,
            run_after=self.clock() + delay,
            correlation_id=self.correlation_id
            or self.commands().context.correlation_id,
        )
        with self.store.db.write():
            return self.store.enqueue(job)

    def heartbeat(self) -> None:
        """Extend the current job's lease during long work."""
        if self.job is None:
            raise PluginError("heartbeat() is only available while running a job")
        with self.store.db.write():
            self.store.heartbeat(
                self.job.id, self.job.attempts, self.clock() + self.lease
            )
