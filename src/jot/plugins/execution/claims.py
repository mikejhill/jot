"""Claims, leases, and phases: the part of execution external agents use.

A claim is a lease recorded in ``metadata.execution`` and written in one
serialized transaction with a version check, so two agents can never hold the
same jot. The phase a jot is in is mirrored to labels (``needs-input``,
``awaiting-approval``, ``approved``, ``review``) and to the profile's status
category, so lists and boards work without the plugin.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import Field

from jot.app.commands import JotPatch
from jot.app.queries import JotFilter
from jot.core.models import Clock, Origin, Record, StatusCategory, WriteContext
from jot.exceptions import ConflictError, InvalidInputError
from jot.plugins.harness.api import obj, text_of

if TYPE_CHECKING:
    from collections.abc import Callable

    from jot.app.commands import Commands
    from jot.app.session import Session
    from jot.core.models import Jot, Metadata

PLUGIN = "execution"
DEFAULT_LEASE = 1800


class Phase(StrEnum):
    """Where a jot is in supervised work."""

    IDLE = "idle"
    PLANNING = "planning"
    NEEDS_INPUT = "needs_input"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    REVIEW = "review"
    DONE = "done"


LABELS: dict[Phase, str] = {
    Phase.NEEDS_INPUT: "needs-input",
    Phase.AWAITING_APPROVAL: "awaiting-approval",
    Phase.APPROVED: "approved",
    Phase.REVIEW: "review",
}
CATEGORY: dict[Phase, StatusCategory] = {
    Phase.IDLE: StatusCategory.OPEN,
    Phase.DONE: StatusCategory.DONE,
}
RUNNING = frozenset({Phase.PLANNING, Phase.EXECUTING})
WAITING = frozenset({Phase.NEEDS_INPUT, Phase.AWAITING_APPROVAL, Phase.REVIEW})
RELEASE_TO = frozenset(
    {Phase.IDLE, Phase.NEEDS_INPUT, Phase.AWAITING_APPROVAL, Phase.REVIEW, Phase.DONE}
)


class Claim(Record):
    """Who holds a jot, until when, and how many times it changed hands."""

    agent: str = Field(min_length=1)
    lease_until: datetime
    generation: int = Field(default=1, ge=1)


class WorkState(Record):
    """A jot's execution state, as stored in ``metadata.execution``."""

    jot_id: int
    title: str
    phase: Phase
    claim: Claim | None = None
    version: int

    @property
    def held(self) -> bool:
        """Whether a claim exists (live or not)."""
        return self.claim is not None


class Claims:
    """Claim, heartbeat, release, and approve jots for agents."""

    def __init__(
        self,
        session: Session,
        *,
        now: Callable[[], datetime] = Clock.now,
        correlation_id: str | None = None,
        origin: Origin = Origin.CLI,
    ) -> None:
        self.session = session
        self.now = now
        self.correlation_id = correlation_id
        self.origin = origin

    # Reading ------------------------------------------------------------------

    @staticmethod
    def phase_of(jot: Jot) -> Phase:
        """The stored phase, else one inferred from labels and status."""
        stored = text_of(obj(jot.metadata.get(PLUGIN)).get("phase"))
        if stored in Phase:
            return Phase(stored)
        for phase, label in LABELS.items():
            if label in jot.labels:
                return phase
        if jot.category == StatusCategory.DONE:
            return Phase.DONE
        return Phase.IDLE

    @staticmethod
    def words(phase: Phase) -> str:
        """A phase as words."""
        return phase.value.replace("_", " ")

    @staticmethod
    def record_of(jot: Jot) -> Metadata:
        """The jot's whole ``metadata.execution`` namespace."""
        return obj(jot.metadata.get(PLUGIN))

    @staticmethod
    def claim_of(jot: Jot) -> Claim | None:
        """The stored claim, if any."""
        raw = obj(jot.metadata.get(PLUGIN)).get("claim")
        return Claim.model_validate(raw) if isinstance(raw, dict) else None

    def state(self, jot_id: int) -> WorkState:
        """A jot's phase and claim."""
        return self._state(self.session.queries.get(jot_id))

    def _state(self, jot: Jot) -> WorkState:
        """Build the state record for a jot."""
        return WorkState(
            jot_id=jot.id,
            title=jot.title,
            phase=self.phase_of(jot),
            claim=self.claim_of(jot),
            version=jot.version,
        )

    def live(self, claim: Claim | None) -> bool:
        """Whether a claim's lease has not expired."""
        return claim is not None and claim.lease_until > self.now()

    def blocked(self, jot_id: int) -> bool:
        """Whether an open jot blocks this one."""
        queries = self.session.queries
        for link in queries.links(jot_id):
            if link.rel == "blocks" and link.dst == jot_id:
                blocker = queries.get(link.src, include_deleted=True)
                if blocker.deleted_at is None and blocker.category in {
                    StatusCategory.OPEN,
                    StatusCategory.ACTIVE,
                }:
                    return True
        return False

    def ready(self, jot: Jot) -> bool:
        """Whether an agent may pick this jot up now (stale running claims count)."""
        phase = self.phase_of(jot)
        return (
            phase in {Phase.IDLE, Phase.APPROVED, *RUNNING}
            and jot.category in {StatusCategory.OPEN, StatusCategory.ACTIVE}
            and self.allowed(jot)
            and not self.live(self.claim_of(jot))
            and not self.blocked(jot.id)
        )

    def allowed(self, jot: Jot) -> bool:
        """Whether the jot's profile lets the execution plugin act."""
        plugins = self.session.profiles.get(jot.profile).plugins
        return not plugins or PLUGIN in plugins

    def next(self, limit: int = 5, project: str | None = None) -> list[WorkState]:
        """Ready jots in rank order."""
        criteria = JotFilter(
            categories=[StatusCategory.OPEN, StatusCategory.ACTIVE], project=project
        )
        ready = [
            self._state(j) for j in self.session.queries.jots(criteria) if self.ready(j)
        ]
        return ready[:limit]

    # Writing ------------------------------------------------------------------

    def claim(
        self,
        jot_id: int,
        agent: str,
        *,
        lease: int = DEFAULT_LEASE,
        direct: bool = False,
    ) -> WorkState:
        """Take a jot (or renew your own claim) and move it into a running phase."""
        with self.session.db.write():
            jot = self.session.queries.get(jot_id)
            current = self.claim_of(jot)
            if not self.allowed(jot):
                raise InvalidInputError(
                    f"Jot {jot_id}'s profile {jot.profile} excludes execution"
                )
            if current is not None and current.agent == agent and self.live(current):
                return self._write(jot, agent, self.phase_of(jot), current, lease)
            if self.live(current):
                assert current is not None  # noqa: S101 - live() checked it
                raise ConflictError(
                    f"Jot {jot_id} is claimed by {current.agent} until "
                    f"{current.lease_until.isoformat()}"
                )
            phase = self.phase_of(jot)
            if (
                phase in WAITING
                or phase == Phase.DONE
                or jot.category
                not in {
                    StatusCategory.OPEN,
                    StatusCategory.ACTIVE,
                }
            ):
                raise InvalidInputError(
                    f"Jot {jot_id} is {self.words(phase)}, not ready to claim"
                )
            if self.blocked(jot_id):
                raise InvalidInputError(f"Jot {jot_id} is blocked by an open jot")
            target = (
                Phase.EXECUTING if direct or phase == Phase.APPROVED else Phase.PLANNING
            )
            return self._write(jot, agent, target, current, lease, take=True)

    def heartbeat(
        self, jot_id: int, agent: str, *, lease: int = DEFAULT_LEASE
    ) -> WorkState:
        """Extend your lease."""
        with self.session.db.write():
            jot = self.session.queries.get(jot_id)
            current = self._held_by(jot, agent)
            return self._write(jot, agent, self.phase_of(jot), current, lease)

    def release(
        self,
        jot_id: int,
        agent: str,
        phase: Phase,
        *,
        note: str | None = None,
        extra: Metadata | None = None,
    ) -> WorkState:
        """Give the jot back in an owner-facing or finished phase."""
        if phase not in RELEASE_TO:
            choices = ", ".join(sorted(p.value for p in RELEASE_TO))
            raise InvalidInputError(f"Release to one of: {choices}")
        with self.session.db.write():
            jot = self.session.queries.get(jot_id)
            self._held_by(jot, agent)
            state = self._move(jot, phase, None, actor=agent, extra=extra)
            if note:
                self._commands(agent, "release").comment(
                    jot_id, note, kind="execution.note"
                )
            return state

    def approve(
        self,
        jot_id: int,
        actor: str,
        text: str = "Approved.",
        extra: Metadata | None = None,
    ) -> WorkState:
        """Approve a plan: an agent may now claim the jot for execution."""
        with self.session.db.write():
            jot = self.session.queries.get(jot_id)
            phase = self.phase_of(jot)
            if phase != Phase.AWAITING_APPROVAL:
                raise InvalidInputError(
                    f"Jot {jot_id} is {self.words(phase)}, not awaiting approval"
                )
            state = self._move(jot, Phase.APPROVED, None, actor=actor, extra=extra)
            self._commands(actor, "approve").comment(
                jot_id, text, kind="execution.approval"
            )
            return state

    def annotate(self, jot_id: int, agent: str, extra: Metadata) -> WorkState:
        """Record more execution metadata while holding the jot."""
        with self.session.db.write():
            jot = self.session.queries.get(jot_id)
            current = self._held_by(jot, agent)
            return self._move(
                jot, self.phase_of(jot), current, actor=agent, extra=extra
            )

    def reopen(
        self,
        jot_id: int,
        actor: str,
        phase: Phase,
        extra: Metadata | None = None,
    ) -> WorkState:
        """Owner: move a jot waiting on them back to idle or approved."""
        if phase not in {Phase.IDLE, Phase.APPROVED}:
            raise InvalidInputError("Reopen to idle or approved")
        with self.session.db.write():
            jot = self.session.queries.get(jot_id)
            current = self.phase_of(jot)
            if current not in WAITING:
                raise InvalidInputError(
                    f"Jot {jot_id} is {self.words(current)}, not waiting on you"
                )
            return self._move(jot, phase, None, actor=actor, extra=extra)

    def reset(self, jot_id: int, actor: str, phase: Phase) -> WorkState:
        """Owner: drop any claim and put the jot in ``phase`` (used by cancel)."""
        with self.session.db.write():
            jot = self.session.queries.get(jot_id)
            return self._move(jot, phase, None, actor=actor)

    def _held_by(self, jot: Jot, agent: str) -> Claim:
        """Return the agent's live claim, or refuse."""
        current = self.claim_of(jot)
        if current is None or current.agent != agent:
            holder = current.agent if current else "nobody"
            raise ConflictError(f"Jot {jot.id} is held by {holder}, not {agent}")
        if not self.live(current):
            raise ConflictError(f"{agent}'s lease on jot {jot.id} expired; claim again")
        return current

    def _write(  # noqa: PLR0913 - one keyword per claim attribute
        self,
        jot: Jot,
        agent: str,
        phase: Phase,
        current: Claim | None,
        lease: int,
        *,
        take: bool = False,
    ) -> WorkState:
        """Store a claim (new generation when taken over) and the phase."""
        generation = (current.generation if current else 0) + (1 if take else 0)
        claim = Claim(
            agent=agent,
            lease_until=self.now() + timedelta(seconds=lease),
            generation=max(generation, 1),
        )
        return self._move(jot, phase, claim, actor=agent)

    def _move(
        self,
        jot: Jot,
        phase: Phase,
        claim: Claim | None,
        *,
        actor: str,
        extra: Metadata | None = None,
    ) -> WorkState:
        """Write phase, claim, labels, and status in the open transaction."""
        commands = self._commands(actor, f"execution: {phase.value}")
        record: Metadata = self.record_of(jot) | (extra or {})
        record |= {"phase": phase.value, "claim": None}
        if claim is not None:
            record["claim"] = claim.model_dump(mode="json")
        updated = commands.update(
            jot.id, JotPatch(metadata={PLUGIN: record}), expected_version=jot.version
        )
        wanted = LABELS.get(phase)
        stale = [label for label in LABELS.values() if label != wanted]
        updated = commands.label(jot.id, add=[wanted] if wanted else [], remove=stale)
        status = self._status_for(updated, CATEGORY.get(phase, StatusCategory.ACTIVE))
        if status is not None and status != updated.status:
            updated = commands.set_status(jot.id, status)
        return self._state(updated)

    def _status_for(self, jot: Jot, category: StatusCategory) -> str | None:
        """The jot's status if it already fits, else the profile's first match."""
        if jot.category == category:
            return jot.status
        profile = self.session.profiles.get(jot.profile)
        return next((s.id for s in profile.statuses if s.category == category), None)

    def _commands(self, actor: str, reason: str) -> Commands:
        """Commands attributed to the acting agent or owner."""
        extra = {"correlation_id": self.correlation_id} if self.correlation_id else {}
        return self.session.commands(
            WriteContext(actor=actor, origin=self.origin, reason=reason, **extra)
        )
