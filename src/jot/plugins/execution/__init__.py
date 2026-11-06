"""Execution: supervised agent work on jots.

Slice 8a (this module) gives external agents safe claims: a lease recorded in
``metadata.execution`` and written in one serialized transaction with a
version check, so two agents can never hold the same jot. The phase an agent
releases a jot into is mirrored to labels (``needs-input``,
``awaiting-approval``, ``approved``, ``review``) and to the profile's status
category, so lists and boards work without the plugin.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, override

import typer
from pydantic import Field

from jot.app.commands import JotPatch
from jot.app.queries import JotFilter
from jot.core.models import Clock, Origin, Record, StatusCategory, WriteContext
from jot.exceptions import ConflictError, InvalidInputError
from jot.plugins.api import Plugin
from jot.plugins.harness.api import obj, text_of

if TYPE_CHECKING:
    from collections.abc import Callable

    from jot.app.commands import Commands
    from jot.app.session import Session
    from jot.core.models import Jot, Metadata
    from jot.plugins.api import PluginCli
    from jot.plugins.host import PluginHost

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


class ExecutionSettings(Record):
    """Execution settings (slice 8a: claims only)."""

    lease_seconds: int = Field(default=DEFAULT_LEASE, ge=30, le=86_400)


class Claims:
    """Claim, heartbeat, release, and approve jots for agents."""

    def __init__(
        self,
        session: Session,
        *,
        now: Callable[[], datetime] = Clock.now,
        correlation_id: str | None = None,
    ) -> None:
        self.session = session
        self.now = now
        self.correlation_id = correlation_id

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
        self, jot_id: int, agent: str, phase: Phase, *, note: str | None = None
    ) -> WorkState:
        """Give the jot back in an owner-facing or finished phase."""
        if phase not in RELEASE_TO:
            choices = ", ".join(sorted(p.value for p in RELEASE_TO))
            raise InvalidInputError(f"Release to one of: {choices}")
        with self.session.db.write():
            jot = self.session.queries.get(jot_id)
            self._held_by(jot, agent)
            state = self._move(jot, phase, None, actor=agent)
            if note:
                self._commands(agent, "release").comment(
                    jot_id, note, kind="execution.note"
                )
            return state

    def approve(self, jot_id: int, actor: str) -> WorkState:
        """Approve a plan: an agent may now claim the jot for execution."""
        with self.session.db.write():
            jot = self.session.queries.get(jot_id)
            phase = self.phase_of(jot)
            if phase != Phase.AWAITING_APPROVAL:
                raise InvalidInputError(
                    f"Jot {jot_id} is {self.words(phase)}, not awaiting approval"
                )
            state = self._move(jot, Phase.APPROVED, None, actor=actor)
            self._commands(actor, "approve").comment(
                jot_id, "Approved.", kind="execution.approval"
            )
            return state

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
        self, jot: Jot, phase: Phase, claim: Claim | None, *, actor: str
    ) -> WorkState:
        """Write phase, claim, labels, and status in the open transaction."""
        commands = self._commands(actor, f"execution: {phase.value}")
        record: Metadata = {"phase": phase.value, "claim": None}
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
            WriteContext(actor=actor, origin=Origin.CLI, reason=reason, **extra)
        )


class ExecutionPlugin(Plugin):
    """Claims and phases for agent work (the runner arrives in slice 8b)."""

    id = PLUGIN
    name = "Execution"
    version = "1"
    description = "Claims with leases so agents can work jots safely."
    listens = False
    command = "work"
    settings_model = ExecutionSettings

    @classmethod
    @override
    def commands(cls, cli: PluginCli) -> typer.Typer:
        """``jot work ...``."""
        return WorkCommands(cli).app()


Json = Annotated[bool, typer.Option("--json", help="Emit JSON")]
Agent = Annotated[str, typer.Option("--agent", "-a", help="Your stable agent name")]
JotId = Annotated[int, typer.Argument(help="Jot id")]


class WorkCommands:
    """``jot work``: the queue for agents."""

    def __init__(self, cli: PluginCli) -> None:
        self.cli = cli

    def app(self) -> typer.Typer:
        """Build the command group."""
        app = typer.Typer(
            no_args_is_help=True, help="Claim and hand back jots (agents)"
        )
        app.command("next")(self.next)
        app.command("claim")(self.claim)
        app.command("heartbeat")(self.heartbeat)
        app.command("release")(self.release)
        app.command("approve")(self.approve)
        app.command("state")(self.state)
        return app

    def claims(self, host: PluginHost) -> tuple[Claims, int]:
        """A claims service, refusing when the plugin is off; plus the lease."""
        if not host.status(PLUGIN).enabled:
            raise InvalidInputError(
                "Execution is off; run `jot plugin enable execution`"
            )
        settings = host.settings(PLUGIN)
        lease = (
            settings.lease_seconds
            if isinstance(settings, ExecutionSettings)
            else DEFAULT_LEASE
        )
        correlation = os.environ.get("JOT_CORRELATION_ID")
        return Claims(host.session, correlation_id=correlation), lease

    @staticmethod
    def line(state: WorkState) -> str:
        """One jot with its phase and holder."""
        holder = ""
        if state.claim is not None:
            until = f"{state.claim.lease_until:%Y-%m-%d %H:%M}"
            holder = f"  [{state.claim.agent} until {until}Z]"
        return f"{state.jot_id:>5}  {state.phase.value:<17} {state.title}{holder}\n"

    def next(
        self,
        *,
        limit: Annotated[int, typer.Option("-n", help="How many")] = 5,
        project: Annotated[str | None, typer.Option("--project", "-P")] = None,
        json: Json = False,
    ) -> None:
        """Ready jots in rank order: unclaimed, unblocked, not waiting on the owner."""
        with self.cli.host(json=json) as host:
            states = self.claims(host)[0].next(limit, project)
        self.cli.emit(states, "".join(map(self.line, states)) or "Nothing ready\n")

    def claim(
        self,
        jot_id: JotId,
        *,
        agent: Agent,
        lease: Annotated[int | None, typer.Option("--lease", help="Seconds")] = None,
        direct: Annotated[
            bool,
            typer.Option("--direct", help="Skip planning: go straight to executing"),
        ] = False,
        json: Json = False,
    ) -> None:
        """Claim a jot (exit 3 if someone else holds it)."""
        with self.cli.host(json=json) as host:
            claims, default = self.claims(host)
            state = claims.claim(jot_id, agent, lease=lease or default, direct=direct)
        self.cli.emit(state, self.line(state))

    def heartbeat(
        self,
        jot_id: JotId,
        *,
        agent: Agent,
        lease: Annotated[int | None, typer.Option("--lease", help="Seconds")] = None,
        json: Json = False,
    ) -> None:
        """Extend your lease while you work."""
        with self.cli.host(json=json) as host:
            claims, default = self.claims(host)
            state = claims.heartbeat(jot_id, agent, lease=lease or default)
        self.cli.emit(state, self.line(state))

    def release(
        self,
        jot_id: JotId,
        *,
        agent: Agent,
        phase: Annotated[
            Phase,
            typer.Option(
                "--phase", help="review, awaiting_approval, needs_input, idle, done"
            ),
        ],
        note: Annotated[
            str | None, typer.Option("--note", help="Comment to add")
        ] = None,
        json: Json = False,
    ) -> None:
        """Hand the jot back in a phase for the owner (or finished)."""
        with self.cli.host(json=json) as host:
            state = self.claims(host)[0].release(jot_id, agent, phase, note=note)
        self.cli.emit(state, self.line(state))

    def approve(
        self,
        jot_id: JotId,
        *,
        actor: Annotated[str | None, typer.Option("--actor")] = None,
        json: Json = False,
    ) -> None:
        """Approve a plan so an agent may execute it."""
        who = actor or os.environ.get("JOT_ACTOR") or "user"
        with self.cli.host(json=json) as host:
            state = self.claims(host)[0].approve(jot_id, who)
        self.cli.emit(state, self.line(state))

    def state(self, jot_id: JotId, *, json: Json = False) -> None:
        """Show a jot's phase and claim."""
        with self.cli.host(json=json) as host:
            state = self.claims(host)[0].state(jot_id)
        self.cli.emit(state, self.line(state))
