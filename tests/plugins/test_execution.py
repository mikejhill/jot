"""Execution slice 8a: claims, leases, phases, and ``jot work``."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from tests.cli.test_cli import data, jot

from jot.app.session import Session
from jot.core.profiles import Profile
from jot.exceptions import ConflictError, InvalidInputError
from jot.plugins.execution import Claims, Phase

if TYPE_CHECKING:
    from jot.app.commands import Commands
    from jot.config import JotHome


class Clock:
    """A settable clock."""

    def __init__(self) -> None:
        self.at = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        """Now."""
        return self.at

    def advance(self, seconds: int) -> None:
        """Move forward."""
        self.at += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    """A fresh clock."""
    return Clock()


@pytest.fixture
def claims(session: Session, clock: Clock) -> Claims:
    """Claims over the test session."""
    return Claims(session, now=clock)


@pytest.fixture
def task(commands: Commands) -> int:
    """One task jot."""
    return commands.create(title="Fix the build", profile="task").id


class TestClaims:
    """Claim, renew, take over, heartbeat."""

    def test_claim_moves_to_planning(
        self, claims: Claims, task: int, session: Session, clock: Clock
    ) -> None:
        """A claim records the holder, lease, and running phase."""
        state = claims.claim(task, "agent-a", lease=600)
        assert state.phase == Phase.PLANNING
        assert state.claim is not None
        assert (state.claim.agent, state.claim.generation) == ("agent-a", 1)
        assert state.claim.lease_until == clock.at + timedelta(seconds=600)
        jot_ = session.queries.get(task)
        assert jot_.status == "doing"
        last = session.queries.history(task)[-1]
        assert (last.actor, last.reason) == ("agent-a", "execution: planning")

    def test_others_are_refused_until_expiry(
        self, claims: Claims, task: int, clock: Clock
    ) -> None:
        """A live claim blocks others; an expired one can be taken over."""
        claims.claim(task, "agent-a", lease=600)
        with pytest.raises(ConflictError, match="claimed by agent-a"):
            claims.claim(task, "agent-b")
        clock.advance(601)
        state = claims.claim(task, "agent-b")
        assert state.claim is not None
        assert (state.claim.agent, state.claim.generation) == ("agent-b", 2)

    def test_renew_keeps_generation(
        self, claims: Claims, task: int, clock: Clock
    ) -> None:
        """Claiming again as the holder renews the lease."""
        claims.claim(task, "agent-a", lease=600)
        clock.advance(300)
        state = claims.claim(task, "agent-a", lease=600)
        assert state.claim is not None
        assert state.claim.generation == 1
        assert state.claim.lease_until == clock.at + timedelta(seconds=600)

    def test_heartbeat(self, claims: Claims, task: int, clock: Clock) -> None:
        """Only the live holder may extend."""
        claims.claim(task, "agent-a", lease=600)
        clock.advance(500)
        state = claims.heartbeat(task, "agent-a", lease=600)
        assert state.claim is not None
        assert state.claim.lease_until == clock.at + timedelta(seconds=600)
        with pytest.raises(ConflictError, match="held by agent-a, not agent-b"):
            claims.heartbeat(task, "agent-b")
        clock.advance(601)
        with pytest.raises(ConflictError, match="expired"):
            claims.heartbeat(task, "agent-a")

    def test_direct_claim_executes(self, claims: Claims, task: int) -> None:
        """--direct skips planning."""
        assert claims.claim(task, "a", direct=True).phase == Phase.EXECUTING

    def test_race_has_one_winner(self, home: JotHome, task: int) -> None:
        """Two processes claiming at once: exactly one wins."""
        barrier = threading.Barrier(2)
        results: list[str] = []

        def attempt(agent: str) -> None:
            with Session.open(home) as own:
                barrier.wait()
                try:
                    Claims(own).claim(task, agent)
                    results.append("won")
                except ConflictError:
                    results.append("lost")

        threads = [threading.Thread(target=attempt, args=(n,)) for n in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results) == ["lost", "won"]


class TestPhases:
    """Release, approval, and what each phase looks like."""

    def test_planned_flow(self, claims: Claims, task: int, session: Session) -> None:
        """Plan, wait for approval, approve, execute, review, done."""
        claims.claim(task, "a")
        state = claims.release(
            task, "a", Phase.AWAITING_APPROVAL, note="1. edit\n2. test"
        )
        assert (state.phase, state.claim) == (Phase.AWAITING_APPROVAL, None)
        jot_ = session.queries.get(task)
        assert (jot_.status, jot_.labels) == ("doing", ["awaiting-approval"])
        assert session.queries.comments(task)[0].kind == "execution.note"
        assert claims.approve(task, "mike").phase == Phase.APPROVED
        assert session.queries.get(task).labels == ["approved"]
        assert session.queries.comments(task)[-1].kind == "execution.approval"
        assert claims.claim(task, "a").phase == Phase.EXECUTING
        assert session.queries.get(task).labels == []
        claims.release(task, "a", Phase.REVIEW)
        assert session.queries.get(task).labels == ["review"]
        with pytest.raises(InvalidInputError, match="review, not ready"):
            claims.claim(task, "b")

    def test_release_to_done_and_idle(
        self, claims: Claims, commands: Commands, session: Session
    ) -> None:
        """Done and idle map to the profile's categories."""
        one = commands.create(title="one", profile="task").id
        two = commands.create(title="two", profile="task").id
        claims.claim(one, "a")
        claims.release(one, "a", Phase.DONE)
        assert session.queries.get(one).status == "done"
        claims.claim(two, "a")
        claims.release(two, "a", Phase.IDLE)
        assert session.queries.get(two).status == "todo"
        with pytest.raises(InvalidInputError, match="not ready"):
            claims.claim(one, "a")

    def test_release_rules(self, claims: Claims, task: int) -> None:
        """Only the holder releases, and only into hand-back phases."""
        claims.claim(task, "a")
        with pytest.raises(InvalidInputError, match="Release to one of"):
            claims.release(task, "a", Phase.EXECUTING)
        with pytest.raises(ConflictError):
            claims.release(task, "b", Phase.REVIEW)
        with pytest.raises(InvalidInputError, match="not awaiting approval"):
            claims.approve(task, "mike")

    def test_imported_labels_set_the_phase(
        self, claims: Claims, commands: Commands
    ) -> None:
        """v0.1 jots imported with a review label are in review."""
        jot_id = commands.create(title="old", profile="task", labels=["review"]).id
        assert claims.state(jot_id).phase == Phase.REVIEW
        assert claims.next() == []

    def test_profiles_without_active_status(
        self, claims: Claims, commands: Commands, session: Session
    ) -> None:
        """A note has no active status, so its status stays open."""
        note = commands.create(title="note").id
        claims.claim(note, "a")
        assert session.queries.get(note).status == "open"

    def test_profile_exclusion(
        self, claims: Claims, commands: Commands, session: Session
    ) -> None:
        """Profiles that list other plugins exclude execution."""
        task = session.profiles.get("task")
        session.profiles.register(task.model_copy(update={"plugins": ("rules",)}))
        jot_id = commands.create(title="x", profile="task").id
        with pytest.raises(InvalidInputError, match="excludes execution"):
            claims.claim(jot_id, "a")
        assert claims.next() == []
        assert isinstance(task, Profile)


class TestNext:
    """What an agent should pick up."""

    def test_order_and_filters(
        self, claims: Claims, commands: Commands, clock: Clock
    ) -> None:
        """Rank order; claimed, waiting, blocked, and finished jots are skipped."""
        commands.create_project("web", "Web")
        first = commands.create(title="first", profile="task").id
        held = commands.create(title="held", profile="task").id
        waiting = commands.create(title="waiting", profile="task").id
        blocker = commands.create(title="blocker", profile="task").id
        blocked = commands.create(title="blocked", profile="task").id
        web = commands.create(title="web", profile="task", project="web").id
        finished = commands.create(title="finished", profile="task", status="done").id
        commands.link(blocker, blocked, "blocks")
        claims.claim(held, "a", lease=60)
        claims.claim(waiting, "a")
        claims.release(waiting, "a", Phase.NEEDS_INPUT)
        assert [s.jot_id for s in claims.next(10)] == [first, blocker, web]
        assert [s.jot_id for s in claims.next(1)] == [first]
        assert [s.jot_id for s in claims.next(10, "web")] == [web]
        with pytest.raises(InvalidInputError, match="blocked"):
            claims.claim(blocked, "a")
        clock.advance(61)
        assert held in [s.jot_id for s in claims.next(10)]
        commands.set_status(blocker, "done")
        assert blocked in [s.jot_id for s in claims.next(10)]
        assert finished not in [s.jot_id for s in claims.next(10)]

    def test_deleted_blocker_does_not_block(
        self, claims: Claims, commands: Commands
    ) -> None:
        """A deleted blocker is ignored."""
        blocker = commands.create(title="b", profile="task").id
        blocked = commands.create(title="x", profile="task").id
        commands.link(blocker, blocked, "blocks")
        commands.delete(blocker)
        assert [s.jot_id for s in claims.next()] == [blocked]


@pytest.mark.usefixtures("home")
class TestWorkCommands:
    """``jot work`` from the command line."""

    def test_agent_flow(self) -> None:
        """Claim, heartbeat, release, approve, state, next."""
        off = data("work", "next", code=1)
        assert "Execution is off" in off["error"]  # ty: ignore[not-subscriptable]
        jot("plugin", "enable", "execution")
        jot("plugin", "config", "execution", "--set", '{"lease_seconds": 900}')
        jot("add", "-p", "task", "Ship it")
        assert jot("work", "next").startswith("    1  idle")
        claimed = data("work", "claim", "1", "--agent", "bot")
        assert claimed["phase"] == "planning"  # ty: ignore[not-subscriptable]
        assert "[bot until" in jot("work", "heartbeat", "1", "-a", "bot")
        taken = data("work", "claim", "1", "--agent", "other", code=3)
        assert "claimed by bot" in taken["error"]  # ty: ignore[not-subscriptable]
        released = jot(
            "work",
            "release",
            "1",
            "-a",
            "bot",
            "--phase",
            "awaiting_approval",
            "--note",
            "plan",
        )
        assert "awaiting_approval" in released
        assert "approved" in jot("work", "approve", "1")
        assert "executing" in jot("work", "claim", "1", "-a", "bot", "--lease", "60")
        assert "executing" in jot("work", "state", "1")
        assert jot("work", "next") == "Nothing ready\n"
        assert "review" in jot("work", "release", "1", "-a", "bot", "--phase", "review")
