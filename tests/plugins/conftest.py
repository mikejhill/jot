"""Test plugins, a controllable clock, and host fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, override

import pytest
from pydantic import BaseModel, ConfigDict, JsonValue

from jot.core.models import Origin, WriteContext
from jot.plugins.api import Job, Plugin
from jot.plugins.host import PluginCatalog, PluginHost

if TYPE_CHECKING:
    from jot.app.session import Session
    from jot.core.models import Event, Jot
    from jot.plugins.context import PluginContext

USER = WriteContext(actor="user", origin=Origin.CLI)


class FakeClock:
    """A clock tests move by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        """Return the current fake time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move time forward."""
        self.now += seconds


class RecorderSettings(BaseModel):
    """Settings for the recorder."""

    model_config = ConfigDict(extra="forbid")
    label: str = "seen"
    fail_on: str | None = None


class Recorder(Plugin):
    """Labels every jot it hears about and logs event kinds in its state."""

    id = "recorder"
    name = "Recorder"
    version = "2"
    description = "Test plugin"
    events = frozenset({"jot.created", "jot.updated", "comment.added"})
    settings_model = RecorderSettings

    @override
    def handle(self, event: Event, context: PluginContext) -> None:
        """Record the event, label the jot, and maybe fail afterwards."""
        settings = context.settings
        assert isinstance(settings, RecorderSettings)
        seen = context.state.get("kinds", [])
        assert isinstance(seen, list)
        context.state.set("kinds", [*seen, event.kind])
        if event.jot_id is not None:
            context.commands("recorded").label(event.jot_id, add=[settings.label])
        if settings.fail_on and event.kind == settings.fail_on:
            raise RuntimeError("boom")

    @override
    def applies_to(self, jot: Jot) -> bool:
        """Ignore jots titled 'skip'."""
        return super().applies_to(jot) and jot.title != "skip"


class Queuer(Plugin):
    """Queues a job per created jot and runs it according to the payload."""

    id = "queuer"
    events = frozenset({"jot.created"})
    runs: ClassVar[list[int]] = []

    @override
    def handle(self, event: Event, context: PluginContext) -> None:
        """Queue one deduplicated job for the jot."""
        context.enqueue(
            "work",
            {"mode": "ok"},
            jot_id=event.jot_id,
            dedupe_key=f"jot-{event.jot_id}",
        )

    @override
    def run(self, job: Job, context: PluginContext) -> JsonValue:
        """Succeed, fail, comment, or return bad data, per the payload."""
        Queuer.runs.append(job.id)
        mode = job.payload.get("mode")
        if mode == "fail":
            raise RuntimeError("job failed")
        if mode == "bad":
            return {"not": object()}  # ty: ignore[invalid-return-type]
        if mode == "beat":
            context.heartbeat()
        if job.jot_id is not None:
            context.commands("job").comment(job.jot_id, f"job {job.id}")
        return {"attempt": job.attempts}

    @override
    def ui_module(self) -> str | None:
        """Provide a trivial UI module."""
        return "export default function setup() {}"


class Broken(Plugin):
    """Targets an API version the host does not provide."""

    id = "broken"
    api = 99


@pytest.fixture
def clock() -> FakeClock:
    """A fresh fake clock."""
    return FakeClock()


@pytest.fixture
def catalog() -> PluginCatalog:
    """The test plugins."""
    Queuer.runs.clear()
    return PluginCatalog.of(Recorder, Queuer, Broken)


@pytest.fixture
def host(session: Session, catalog: PluginCatalog, clock: FakeClock) -> PluginHost:
    """A host over the test plugins with a fake clock."""
    return PluginHost(session, catalog, clock=clock, lease=30, max_failures=3)
