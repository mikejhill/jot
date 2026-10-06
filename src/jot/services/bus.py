"""In-process publish/subscribe bus feeding live updates (SSE) to the UI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from jot.agents.base import JsonObject

QUEUE_SIZE = 1000


class Topic(StrEnum):
    """Message topics: task row changed, run state changed, run log line."""

    TASK = "task"
    RUN = "run"
    RUN_LOG = "run_log"


@dataclass(frozen=True, slots=True)
class BusMessage:
    """One published message."""

    topic: Topic
    payload: JsonObject


@dataclass(slots=True)
class EventBus:
    """Fan out messages to every live subscriber; slow subscribers drop messages."""

    _queues: set[asyncio.Queue[BusMessage | None]] = field(default_factory=set)
    _closed: bool = False

    def publish(self, topic: Topic, payload: JsonObject) -> None:
        """Deliver a message to all current subscribers without blocking."""
        message = BusMessage(topic, payload)
        for queue in tuple(self._queues):
            if not queue.full():
                queue.put_nowait(message)

    def close(self) -> None:
        """End all current and future subscriptions (server shutdown)."""
        self._closed = True
        for queue in tuple(self._queues):
            if queue.full():
                queue.get_nowait()  # make room for the end-of-stream marker
            queue.put_nowait(None)

    async def subscribe(self) -> AsyncIterator[BusMessage]:
        """Yield messages published after subscribing until closed or stopped."""
        if self._closed:
            return
        queue: asyncio.Queue[BusMessage | None] = asyncio.Queue(QUEUE_SIZE)
        self._queues.add(queue)
        try:
            while (message := await queue.get()) is not None:
                yield message
        finally:
            self._queues.discard(queue)
