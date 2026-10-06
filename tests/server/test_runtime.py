"""SSE delivery, heartbeat continuity, and background-worker failure isolation."""

from __future__ import annotations

import asyncio

import pytest

from jot.config import JotHome
from jot.core.models import Task
from jot.core.workflow import Workflow
from jot.db.connection import Database
from jot.exceptions import NotImplementedAppError
from jot.server.runtime import EventStream, Runtime
from jot.services.bus import EventBus, Topic
from jot.services.enrich import EnrichService


class MaintenanceBoundary:
    """Predictable maintenance collaborators without provider work."""

    @staticmethod
    def reap(_workflow: Workflow) -> list[int]:
        """Report one reclaimed task."""
        return [11]

    @staticmethod
    async def enrich(_service: EnrichService, limit: int = 10) -> list[Task]:
        """Report one enriched task."""
        del limit
        return [Task(id=12, title="Enriched")]

    @staticmethod
    async def unavailable(_service: EnrichService, limit: int = 10) -> list[Task]:
        """Simulate an unimplemented service during background maintenance."""
        del limit
        raise NotImplementedAppError("enrichment pending")


class TestRuntime:
    """Test the SSE generator directly to avoid infinite TestClient buffering."""

    def test_sse_heartbeat_and_messages(self) -> None:
        """A heartbeat leaves the subscription live and subsequent events arrive."""
        asyncio.run(self._stream())

    async def _stream(self) -> None:
        """Drive subscription registration, heartbeat, all topics, and closure."""
        bus = EventBus()
        stream = EventStream(bus, keepalive=0).messages()
        assert await anext(stream) == ": connected\n\n"
        assert await anext(stream) == ": keepalive\n\n"
        for topic in Topic:
            bus.publish(topic, {"id": 7, "text": "hello\nworld"})
            chunk = await anext(stream)
            assert f"event: {topic}\n" in chunk
            assert '"id": 7' in chunk
            assert "hello\\nworld" in chunk
        await stream.aclose()

    def test_worker_publishes_changes(
        self, db: Database, home: JotHome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both reaping and enrichment notify the shared bus."""
        monkeypatch.setattr(Workflow, "reap", MaintenanceBoundary.reap)
        monkeypatch.setattr(EnrichService, "enrich_pending", MaintenanceBoundary.enrich)
        asyncio.run(self._tick(Runtime(db, home, home.initialize())))

    async def _tick(self, runtime: Runtime) -> None:
        """Observe the worker's invalidations from a live subscription."""
        stream = EventStream(runtime.bus, keepalive=0).messages()
        await anext(stream)
        await anext(stream)
        await runtime.tick()
        assert '"id": 11' in await anext(stream)
        assert '"id": 12' in await anext(stream)
        await stream.aclose()

    def test_worker_continues_after_service_error(
        self,
        db: Database,
        home: JotHome,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Expected service failures are logged without terminating future ticks."""
        monkeypatch.setattr(
            EnrichService, "enrich_pending", MaintenanceBoundary.unavailable
        )
        runtime = Runtime(db, home, home.initialize())
        asyncio.run(runtime.tick())
        asyncio.run(runtime.tick())
        assert caplog.text.count("Pending enrichment failed") == 2
