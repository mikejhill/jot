"""Plugin discovery, administration, durable delivery, and jobs."""

from __future__ import annotations

import threading
from importlib.metadata import EntryPoint
from typing import TYPE_CHECKING

import pytest
from tests.plugins.conftest import USER, FakeClock, Queuer, Recorder

from jot.app.commands import JotPatch
from jot.app.session import Session
from jot.core.models import Origin
from jot.exceptions import ConflictError, NotFoundError, PluginError
from jot.plugins.api import JobStatus, Plugin
from jot.plugins.context import PluginContext
from jot.plugins.host import (
    MAX_BACKOFF,
    PluginCatalog,
    PluginHost,
    Worker,
)

if TYPE_CHECKING:
    from jot.app.commands import Commands
    from jot.config import JotHome


def kinds(host: PluginHost, plugin_id: str = "recorder") -> object:
    """Return the event kinds a plugin recorded in its state."""
    return host.store.get_state(plugin_id, "kinds")


class TestCatalog:
    """Entry-point discovery and validation."""

    def test_discover_builtin_rules(self) -> None:
        """The packaged rules plugin is found through its entry point."""
        catalog = PluginCatalog.discover()
        assert catalog.load("rules").id == "rules"

    def test_discover_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Import errors, non-plugins, and bad contracts become broken entries."""
        points = [
            EntryPoint("missing", "jot_no_such_module:Thing", "jot.plugins"),
            EntryPoint("notplugin", "jot.exceptions:AppError", "jot.plugins"),
            EntryPoint("recorder", "tests.plugins.conftest:Recorder", "jot.plugins"),
            EntryPoint("broken", "tests.plugins.conftest:Broken", "jot.plugins"),
        ]
        monkeypatch.setattr("jot.plugins.host.entry_points", lambda **_kw: list(points))
        catalog = PluginCatalog.discover()
        assert "ModuleNotFoundError" in (catalog.get("missing").error or "")
        assert catalog.get("notplugin").error == "not a Plugin subclass"
        assert "targets API 99" in (catalog.get("broken").error or "")
        assert catalog.load("recorder") is Recorder
        with pytest.raises(PluginError, match="broken"):
            catalog.load("broken")
        with pytest.raises(NotFoundError, match="not installed"):
            catalog.get("nope")

    def test_duplicates_and_bad_ids(self) -> None:
        """Duplicate ids and invalid ids are rejected."""

        class Bad(Plugin):
            id = "Bad Id"

        catalog = PluginCatalog.of(Recorder, Recorder, Bad)
        assert "duplicate" in (catalog.get("recorder").error or "")
        assert "invalid plugin id" in (catalog.get("Bad Id").error or "")


class TestAdmin:
    """Enable, disable, configure, and status."""

    def test_status_of_installed(self, host: PluginHost) -> None:
        """Statuses list every plugin, broken ones with their error."""
        statuses = {s.id: s for s in host.statuses()}
        assert set(statuses) == {"broken", "queuer", "recorder"}
        recorder = statuses["recorder"]
        assert (recorder.name, recorder.version, recorder.enabled) == (
            "Recorder",
            "2",
            False,
        )
        assert recorder.events == ["comment.added", "jot.created", "jot.updated"]
        properties = recorder.settings_schema["properties"]
        assert isinstance(properties, dict)
        assert "label" in properties
        assert not recorder.has_ui
        assert statuses["queuer"].has_ui
        assert statuses["broken"].error

    def test_enable_starts_after_existing_events(
        self, host: PluginHost, commands: Commands
    ) -> None:
        """Enabling skips history by default and records an event."""
        commands.create(title="old")
        status = host.enable("recorder", USER)
        assert status.enabled
        assert status.cursor == 1
        assert status.lag == 1  # the plugin.enabled event itself
        events = host.session.queries.events(0, 10)
        assert events[-1].kind == "plugin.enabled"
        assert events[-1].jot_id is None
        host.deliver()
        assert kinds(host) is None
        assert host.session.queries.get(1).labels == []

    def test_enable_from_start_replays(
        self, host: PluginHost, commands: Commands
    ) -> None:
        """--from-start delivers every past event."""
        commands.create(title="old")
        host.enable("recorder", USER, from_start=True)
        assert host.deliver() == 3  # created, enabled, and its own label write
        assert kinds(host) == ["jot.created"]
        assert host.session.queries.get(1).labels == ["seen"]

    def test_enable_twice_keeps_cursor(
        self, host: PluginHost, commands: Commands
    ) -> None:
        """Re-enabling an enabled plugin is a no-op."""
        host.enable("recorder", USER)
        commands.create(title="a")
        host.enable("recorder", USER)
        assert host.deliver() == 3

    def test_disable_stops_delivery(self, host: PluginHost, commands: Commands) -> None:
        """A disabled plugin receives nothing; disabling twice is a no-op."""
        host.enable("recorder", USER)
        host.disable("recorder", USER)
        host.disable("recorder", USER)
        commands.create(title="a")
        assert host.deliver() == 0
        assert host.status("recorder").lag == 0
        assert [e.kind for e in host.session.queries.events(0, 10)].count(
            "plugin.disabled"
        ) == 1

    def test_configure(self, host: PluginHost, commands: Commands) -> None:
        """Settings are validated, stored, and used."""
        status = host.configure("recorder", {"label": "heard"}, USER)
        assert status.settings == {"label": "heard", "fail_on": None}
        host.enable("recorder", USER)
        commands.create(title="a")
        host.deliver()
        assert host.session.queries.get(1).labels == ["heard"]
        with pytest.raises(PluginError, match="Invalid settings"):
            host.configure("recorder", {"nope": 1}, USER)

    def test_broken_cannot_be_enabled(self, host: PluginHost) -> None:
        """A broken plugin cannot be enabled or configured."""
        with pytest.raises(PluginError):
            host.enable("broken", USER)
        with pytest.raises(PluginError):
            host.configure("broken", {}, USER)
        with pytest.raises(NotFoundError):
            host.disable("nope", USER)

    def test_uninstalled_rows_are_ignored(
        self, session: Session, host: PluginHost, clock: FakeClock
    ) -> None:
        """An enabled plugin that is no longer installed is skipped."""
        host.enable("recorder", USER)
        other = PluginHost(session, PluginCatalog.of(Queuer), clock=clock)
        assert other.enabled() == []
        assert other.deliver() == 0


class TestDelivery:
    """Ordering, filtering, atomicity, and failures."""

    def test_in_order_with_filters(self, host: PluginHost, commands: Commands) -> None:
        """Subscribed kinds arrive in order; other kinds just advance the cursor."""
        host.enable("recorder", USER)
        jot = commands.create(title="a")
        commands.set_status(jot.id, "done")
        commands.comment(jot.id, "hi")
        delivered = host.deliver()
        assert delivered == 5  # plugin.enabled, three user events, its own label
        assert kinds(host) == ["jot.created", "comment.added"]
        assert host.status("recorder").lag == 0

    def test_own_events_are_skipped(self, host: PluginHost, commands: Commands) -> None:
        """The plugin's label write does not come back to it as an update."""
        host.enable("recorder", USER)
        commands.create(title="a")
        host.deliver()
        host.deliver()
        history = host.session.queries.history(1)
        assert [e.actor for e in history] == ["user", "plugin:recorder"]
        assert history[1].origin == Origin.PLUGIN
        assert history[1].reason == "recorded"
        assert history[1].correlation_id == history[0].correlation_id
        assert kinds(host) == ["jot.created"]

    def test_applies_to_and_profiles(
        self, session: Session, host: PluginHost, commands: Commands
    ) -> None:
        """Plugin and profile applicability filter events."""
        session.profiles.register(
            session.profiles.get("task").model_copy(update={"plugins": ("queuer",)})
        )
        host.enable("recorder", USER)
        commands.create(title="skip")
        commands.create(title="task", profile="task")
        commands.create(title="note")
        host.deliver()
        labels = [session.queries.get(i).labels for i in (1, 2, 3)]
        assert labels == [[], [], ["seen"]]

    def test_deleted_jots_are_skipped(
        self, host: PluginHost, commands: Commands
    ) -> None:
        """Events for deleted jots do not reach plugins by default."""
        jot = commands.create(title="a")
        commands.delete(jot.id)
        host.enable("recorder", USER, from_start=True)
        host.deliver()
        assert kinds(host) is None

    def test_failure_rolls_back_and_backs_off(
        self, host: PluginHost, commands: Commands, clock: FakeClock
    ) -> None:
        """A failing handler leaves no writes, keeps its cursor, and retries later."""
        host.configure("recorder", {"fail_on": "jot.created"}, USER)
        host.enable("recorder", USER)
        commands.create(title="a")
        assert host.deliver() == 2  # plugin.enabled, then the failing jot.created
        status = host.status("recorder")
        assert status.failures == 1
        assert "boom" in (status.last_error or "")
        assert status.retry_at == clock.now + 2
        assert host.session.queries.get(1).labels == []
        assert kinds(host) is None
        assert host.deliver() == 0  # backing off
        clock.advance(2)
        assert host.deliver() == 1
        assert host.status("recorder").failures == 2

    def test_gives_up_after_max_failures(
        self, host: PluginHost, commands: Commands, clock: FakeClock
    ) -> None:
        """After max failures the event is recorded and skipped."""
        host.configure("recorder", {"fail_on": "jot.created"}, USER)
        host.enable("recorder", USER)
        commands.create(title="a")
        commands.update(1, JotPatch(body="b"))
        for _ in range(3):
            host.deliver()
            clock.advance(MAX_BACKOFF)
        failures = host.failures("recorder")
        assert [(f.event_id, f.attempts) for f in failures] == [(3, 3)]
        assert host.failures() == failures
        host.deliver()
        status = host.status("recorder")
        assert (status.failures, status.lag) == (0, 0)
        assert host.session.queries.get(1).labels == ["seen"]  # from jot.updated

    def test_configure_resets_backoff(
        self, host: PluginHost, commands: Commands
    ) -> None:
        """Fixing settings clears the backoff so delivery resumes at once."""
        host.configure("recorder", {"fail_on": "jot.created"}, USER)
        host.enable("recorder", USER)
        commands.create(title="a")
        host.deliver()
        host.configure("recorder", {}, USER)
        assert host.deliver() >= 1
        assert host.session.queries.get(1).labels == ["seen"]

    def test_backoff_caps(self) -> None:
        """Backoff doubles and caps."""
        assert [PluginHost.backoff(n) for n in (1, 2, 3)] == [2, 4, 8]
        assert PluginHost.backoff(30) == MAX_BACKOFF

    def test_concurrent_workers_deliver_once(
        self, home: JotHome, catalog: PluginCatalog, commands: Commands
    ) -> None:
        """Parallel hosts never deliver an event twice."""
        with Session.open(home) as session:
            PluginHost(session, catalog).enable("recorder", USER)
        for index in range(20):
            commands.create(title=f"j{index}")

        def drain() -> None:
            with Session.open(home) as session:
                PluginHost(session, catalog).deliver(limit=1000)

        threads = [threading.Thread(target=drain) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        with Session.open(home) as session:
            seen = PluginHost(session, catalog).store.get_state("recorder", "kinds")
        assert seen == ["jot.created"] * 20


class TestJobs:
    """Queueing, leases, retries, and manual control."""

    def test_enqueue_and_run(self, host: PluginHost, commands: Commands) -> None:
        """A handler's job runs once; dedupe keys collapse duplicates."""
        host.enable("queuer", USER)
        commands.create(title="a")
        host.deliver()
        jobs = host.jobs()
        assert [(j.kind, j.status, j.jot_id) for j in jobs] == [
            ("work", JobStatus.QUEUED, 1)
        ]
        context = PluginContext(
            host.session,
            host.store,
            "queuer",
            host.settings("queuer"),
            clock=host.clock,
        )
        again = context.enqueue("work", jot_id=1, dedupe_key="jot-1")
        assert again.id == jobs[0].id
        assert host.run_jobs() == 1
        done = host.store.get_job(jobs[0].id)
        assert (done.status, done.result, done.attempts) == (
            JobStatus.DONE,
            {"attempt": 1},
            1,
        )
        comment = host.session.queries.comments(1)[0]
        assert comment.author == "plugin:queuer"
        assert host.run_jobs() == 0

    def enqueue(self, host: PluginHost, mode: str, **extra: object) -> int:
        """Queue a job directly and return its id."""
        host.enable("queuer", USER)
        context = PluginContext(
            host.session,
            host.store,
            "queuer",
            host.settings("queuer"),
            clock=host.clock,
        )
        return context.enqueue("work", {"mode": mode}, **extra).id  # ty: ignore[invalid-argument-type]

    def test_retries_then_fails(self, host: PluginHost, clock: FakeClock) -> None:
        """Failures retry with backoff until max_attempts."""
        job_id = self.enqueue(host, "fail", max_attempts=2)
        assert host.run_jobs() == 1
        job = host.store.get_job(job_id)
        assert (job.status, job.attempts) == (JobStatus.QUEUED, 1)
        assert "job failed" in (job.error or "")
        assert host.run_jobs() == 0
        clock.advance(2)
        assert host.run_jobs() == 1
        job = host.store.get_job(job_id)
        assert job.status == JobStatus.FAILED
        assert host.jobs(statuses=[JobStatus.FAILED], plugin_id="queuer") == [job]

    def test_bad_result_fails(self, host: PluginHost) -> None:
        """A non-JSON result counts as a failure."""
        job_id = self.enqueue(host, "bad", max_attempts=1)
        host.run_jobs()
        assert host.store.get_job(job_id).status == JobStatus.FAILED

    def test_delay(self, host: PluginHost, clock: FakeClock) -> None:
        """Delayed jobs wait for their time."""
        self.enqueue(host, "ok", delay=10)
        assert host.run_jobs() == 0
        clock.advance(10)
        assert host.run_jobs() == 1

    def test_disabled_plugin_jobs_wait(self, host: PluginHost) -> None:
        """Jobs of a disabled plugin are not claimed."""
        self.enqueue(host, "ok")
        host.disable("queuer", USER)
        assert host.run_jobs() == 0

    def test_expired_lease_is_reclaimed(
        self, host: PluginHost, clock: FakeClock
    ) -> None:
        """A run that outlives its lease loses the job to the next claim."""
        job_id = self.enqueue(host, "ok")
        with host.session.db.write():
            stale = host.store.claim(["queuer"], clock(), 30)
        assert stale is not None
        assert host.run_jobs() == 0  # still leased
        clock.advance(31)
        assert host.run_jobs() == 1
        assert host.store.get_job(job_id).attempts == 2
        with host.session.db.write():
            assert not host.store.finish(stale, JobStatus.DONE)
        with pytest.raises(ConflictError), host.session.db.write():
            host.store.heartbeat(job_id, stale.attempts, clock() + 30)

    def test_heartbeat(self, host: PluginHost) -> None:
        """Heartbeats extend the lease; outside a job they are refused."""
        job_id = self.enqueue(host, "beat")
        host.run_jobs()
        assert host.store.get_job(job_id).status == JobStatus.DONE
        context = PluginContext(
            host.session,
            host.store,
            "queuer",
            host.settings("queuer"),
            clock=host.clock,
        )
        with pytest.raises(PluginError, match="heartbeat"):
            context.heartbeat()

    def test_cancel_and_retry(self, host: PluginHost) -> None:
        """Queued jobs can be cancelled; finished ones requeued."""
        job_id = self.enqueue(host, "ok")
        with pytest.raises(ConflictError, match="not finished"):
            host.retry(job_id)
        assert host.cancel(job_id).status == JobStatus.CANCELLED
        with pytest.raises(ConflictError, match="already finished"):
            host.cancel(job_id)
        retried = host.retry(job_id)
        assert (retried.status, retried.attempts, retried.error) == (
            JobStatus.QUEUED,
            0,
            None,
        )
        assert host.run_jobs() == 1
        with pytest.raises(NotFoundError):
            host.cancel(999)

    def test_retry_with_live_duplicate(self, host: PluginHost) -> None:
        """Requeueing is refused while another job holds the same dedupe key."""
        first = self.enqueue(host, "ok", dedupe_key="k")
        host.cancel(first)
        self.enqueue(host, "ok", dedupe_key="k")
        with pytest.raises(ConflictError, match="live duplicate"):
            host.retry(first)

    def test_job_without_runner(self, host: PluginHost, session: Session) -> None:
        """A plugin without run() fails its jobs."""
        host.enable("recorder", USER)
        context = PluginContext(
            session, host.store, "recorder", host.settings("recorder"), clock=host.clock
        )
        job = context.enqueue("x", max_attempts=1)
        host.run_jobs()
        assert "NotImplementedError" in (host.store.get_job(job.id).error or "")


class TestState:
    """Private plugin storage."""

    def test_get_set_delete(self, host: PluginHost) -> None:
        """Values round-trip; None deletes."""
        context = PluginContext(
            host.session,
            host.store,
            "recorder",
            host.settings("recorder"),
            clock=host.clock,
        )
        assert context.state.get("a", 5) == 5
        context.state.set("a", {"x": [1, 2]})
        context.state.set("b", "text")
        assert context.state.get("a") == {"x": [1, 2]}
        assert context.state.keys() == ["a", "b"]
        context.state.set("a", None)
        assert context.state.keys() == ["b"]
        assert context.actor == "plugin:recorder"
        assert context.queries is host.session.queries
        assert context.commands().context.origin == Origin.PLUGIN


class TestWorker:
    """The background loop."""

    def test_tick_and_thread(
        self, home: JotHome, catalog: PluginCatalog, commands: Commands
    ) -> None:
        """A worker delivers and runs jobs, in the foreground or a thread."""
        with Session.open(home) as session:
            PluginHost(session, catalog).enable("queuer", USER)
        commands.create(title="a")
        worker = Worker(home, catalog, interval=0.01)
        result = worker.run(once=True)
        assert (result.delivered, result.jobs) == (2, 1)
        commands.create(title="b")
        worker.start()
        try:
            for _ in range(500):
                if len(Queuer.runs) == 2:
                    break
                threading.Event().wait(0.01)
        finally:
            worker.stop()
        assert len(Queuer.runs) == 2
        worker.stop()

    def test_tick_survives_errors(
        self, home: JotHome, catalog: PluginCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing pass is logged and returns an empty result."""

        def explode(_self: PluginHost) -> None:
            raise PluginError("nope")

        monkeypatch.setattr(PluginHost, "tick", explode)
        assert Worker(home, catalog).tick().delivered == 0
