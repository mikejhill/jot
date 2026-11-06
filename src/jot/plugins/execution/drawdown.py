"""Drawdown: work the list unattended, one jot at a time, in rank order.

With ``drawdown`` on, a ``drawdown`` job picks the top-ranked ready jot (idle
or approved, unclaimed, unblocked, with a mapped repository whose other jots
are not running) and queues its plan or execute job. The job is queued again
whenever a run ends and whenever the owner changes something, so the list
drains while every owner checkpoint (questions, approval, review) still
waits for the owner. ``Inbox`` lists what waits on the owner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import JsonValue

from jot.app.queries import JotFilter
from jot.core.models import Record, StatusCategory
from jot.exceptions import InvalidInputError
from jot.plugins.api import JobStatus
from jot.plugins.execution.claims import WAITING, Claims, Phase
from jot.plugins.execution.contract import CapError
from jot.plugins.execution.runner import ExecutionSettings, Runner

if TYPE_CHECKING:
    from pathlib import Path

    from jot.app.session import Session
    from jot.core.models import Jot
    from jot.plugins.context import PluginContext

DRAWDOWN = "drawdown"
RUNS = frozenset({"plan", "execute"})
LIVE = [JobStatus.QUEUED, JobStatus.RUNNING]
SCAN = 200
REASONS = {
    Phase.NEEDS_INPUT: "answer questions",
    Phase.AWAITING_APPROVAL: "approve a plan",
    Phase.REVIEW: "review a result",
}


class Drawdown:
    """Pick the next jot to run and queue it."""

    def __init__(self, context: PluginContext, settings: ExecutionSettings) -> None:
        self.context = context
        self.settings = settings
        self.runner = Runner(context, settings, name=DRAWDOWN)

    @staticmethod
    def kick(context: PluginContext, settings: ExecutionSettings) -> None:
        """Queue a drawdown pass (deduplicated) when drawdown is on."""
        if settings.drawdown:
            context.enqueue(DRAWDOWN, dedupe_key=DRAWDOWN, max_attempts=1)

    def live_runs(self) -> list[int]:
        """Jot ids of queued or running plan and execute jobs."""
        jobs = self.context.store.jobs(plugin_id=self.context.plugin_id, statuses=LIVE)
        return [j.jot_id for j in jobs if j.kind in RUNS and j.jot_id is not None]

    def repo(self, jot: Jot) -> Path | None:
        """The jot's repository, or None when it has none."""
        try:
            return self.runner.repo_for(jot).resolve()
        except InvalidInputError:
            return None

    def eligible(self, jot: Jot, busy: set[Path]) -> bool:
        """Whether drawdown may start this ready jot now."""
        projects = self.settings.drawdown_projects
        if projects and jot.project not in projects:
            return False
        if Claims.phase_of(jot) not in {Phase.IDLE, Phase.APPROVED}:
            return False
        repo = self.repo(jot)
        return repo is not None and repo not in busy

    def pick(self) -> Jot | None:
        """The top-ranked jot drawdown may start, if any."""
        queries = self.context.queries
        running = self.live_runs()
        busy = {
            repo
            for jot_id in running
            if (repo := self.repo(queries.get(jot_id, include_deleted=True)))
        }
        claims = self.runner.claims
        for state in claims.next(SCAN):
            jot = queries.get(state.jot_id)
            if jot.id not in running and self.eligible(jot, busy):
                return jot
        return None

    def run(self) -> JsonValue:
        """One pass: start at most one jot, within the concurrency limit."""
        if not self.settings.drawdown:
            return {"skipped": "drawdown is off"}
        if len(self.live_runs()) >= self.settings.concurrency:
            return {"skipped": "busy"}
        try:
            self.runner.budget().start()
        except CapError as err:
            return {"skipped": str(err)}
        jot = self.pick()
        if jot is None:
            return {"skipped": "nothing ready"}
        phase = Claims.phase_of(jot)
        settings = self.settings
        direct = (jot.project in settings.direct_projects) or bool(
            settings.direct_label and settings.direct_label in jot.labels
        )
        kind = "execute" if direct or phase == Phase.APPROVED else "plan"
        self.context.enqueue(
            kind, {"direct": direct}, jot_id=jot.id, dedupe_key=f"jot-{jot.id}"
        )
        return {"started": jot.id, "kind": kind}


class Waiting(Record):
    """A jot waiting on the owner, and what for."""

    jot_id: int
    title: str
    phase: Phase
    reason: str
    project: str | None = None


class Inbox:
    """Jots that wait on the owner, in rank order."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def waiting(self, limit: int = 50) -> list[Waiting]:
        """Questions, plans, and results waiting for the owner."""
        found: list[Waiting] = []
        claims = Claims(self.session)
        criteria = JotFilter(categories=[StatusCategory.ACTIVE])
        for jot in self.session.queries.jots(criteria):
            if len(found) >= limit:
                break
            phase = claims.phase_of(jot)
            if phase not in WAITING or not claims.allowed(jot):
                continue
            reason = REASONS[phase]
            outcome = Claims.record_of(jot).get("outcome")
            if phase == Phase.NEEDS_INPUT and outcome in {"failed", "capped"}:
                reason = f"run {outcome}: retry or edit"
            found.append(
                Waiting(
                    jot_id=jot.id,
                    title=jot.title,
                    phase=phase,
                    reason=reason,
                    project=jot.project,
                )
            )
        return found
