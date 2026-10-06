"""Own the database, shared services, background worker, and SSE lifetime."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from contextlib import suppress

from jot.config import Config, JotHome
from jot.core.workflow import Workflow
from jot.db.connection import Database
from jot.db.repository import ProjectRepository, RunRepository, TaskRepository
from jot.services.bus import EventBus, Topic
from jot.services.cleanup import CleanupService
from jot.services.enrich import EnrichService
from jot.services.runner import RunService

logger = logging.getLogger(__name__)


class Runtime:
    """Collaborators sharing one event-loop-owned SQLite connection."""

    def __init__(self, db: Database, home: JotHome, config: Config) -> None:
        self.db = db
        self.home = home
        self.config = config
        self.bus = EventBus()
        self.tasks = TaskRepository(db)
        self.projects = ProjectRepository(db)
        self.runs = RunRepository(db)
        self.workflow = Workflow(db, config.drawdown.default_flow)
        self.enrich = EnrichService(db, home, config, self.bus)
        self.runner = RunService(db, home, config, self.bus)
        self.cleanup = CleanupService(db, home, config)

    def changed(self, task_id: int) -> None:
        """Notify all clients that a task must be refreshed."""
        self.bus.publish(Topic.TASK, {"id": task_id})

    async def tick(self) -> None:
        """Run independent maintenance steps, preserving future retries on errors."""
        try:
            for task_id in self.workflow.reap():
                self.changed(task_id)
        except Exception:
            logger.exception("Lease reaping failed")
        try:
            for task in await self.enrich.enrich_pending():
                self.changed(task.id)
        except Exception:
            logger.exception("Pending enrichment failed")

    async def work(self) -> None:
        """Repeat maintenance until lifespan cancellation."""
        while True:
            await asyncio.sleep(3)
            await self.tick()


class EventStream:
    """Relay bus messages without cancelling subscriptions on heartbeat timeout."""

    def __init__(self, bus: EventBus, keepalive: float = 15) -> None:
        self._bus = bus
        self._keepalive = keepalive

    async def messages(self) -> AsyncGenerator[str]:
        """Yield SSE events and keepalive comments, releasing subscribers on close."""
        subscription = self._bus.subscribe()
        pending = asyncio.ensure_future(anext(subscription))
        try:
            yield ": connected\n\n"
            while True:
                done, _ = await asyncio.wait({pending}, timeout=self._keepalive)
                if not done:
                    yield ": keepalive\n\n"
                    continue
                try:
                    message = pending.result()
                except StopAsyncIteration:
                    return  # bus closed: the server is shutting down
                pending = asyncio.ensure_future(anext(subscription))
                yield (
                    f"event: {message.topic}\n"
                    f"data: {json.dumps(message.payload, ensure_ascii=False)}\n\n"
                )
        finally:
            pending.cancel()
            # A closed bus leaves pending finished with StopAsyncIteration.
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
            if isinstance(subscription, AsyncGenerator):
                await subscription.aclose()


class RuntimeAccess:
    """Explicit typed runtime slot populated by application lifespan."""

    def __init__(self) -> None:
        self.current: Runtime | None = None

    @property
    def runtime(self) -> Runtime:
        """Return initialized services or report incorrect application use."""
        if self.current is None:
            raise RuntimeError("Application lifespan has not started")
        return self.current
