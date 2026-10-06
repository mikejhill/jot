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


class TestShutdown:
    """Closing the bus ends live streams so the server can exit promptly."""

    def test_close_ends_streams_and_future_subscriptions(self) -> None:
        """Open streams finish, a full queue still receives the end marker."""

        async def scenario() -> list[str]:
            bus = EventBus()
            stream = EventStream(bus, keepalive=30).messages()
            first = await anext(stream)
            subscription = bus.subscribe()
            waiting = asyncio.ensure_future(anext(subscription))
            await asyncio.sleep(0)
            for index in range(1001):  # overflow the queue before closing
                bus.publish(Topic.TASK, {"id": index})
            bus.close()
            chunks = [first, *[chunk async for chunk in stream]]
            drained = [message async for message in subscription]
            assert (await waiting).topic is Topic.TASK
            assert 0 < len(drained) < 1001  # the end marker displaced one message
            assert [message async for message in bus.subscribe()] == []
            return chunks

        chunks = asyncio.run(scenario())
        assert chunks[0] == ": connected\n\n"
        assert all(chunk.startswith("event: task") for chunk in chunks[1:])
