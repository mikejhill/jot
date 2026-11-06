"""The autonomy contract: what a run may spend, and what counts as done.

``Budget`` enforces the per-attempt cost and time caps and the daily spend
cap. ``Keepalive`` extends the job lease and the jot claim from a background
thread, so long silent tool calls (a test suite, a build) do not lose the
claim. ``Evidence`` decides whether a result proves its work.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jot.exceptions import AppError, ConflictError
from jot.plugins.harness.api import EventKind, number

if TYPE_CHECKING:
    from collections.abc import Callable

    from jot.plugins.context import PluginState
    from jot.plugins.harness.api import HarnessEvent

SPEND_KEY = "spend:"
KEEPALIVE_SECONDS = 60.0


class CapError(AppError):
    """A run hit a spend or time cap; it is stopped and not retried."""


def today() -> str:
    """The UTC day, for daily spend."""
    return datetime.now(UTC).date().isoformat()


class Budget:
    """Cost and time accounting for one attempt."""

    def __init__(  # noqa: PLR0913 - one keyword per cap
        self,
        state: PluginState,
        *,
        max_cost: float,
        max_minutes: float,
        daily_cost: float,
        monotonic: Callable[[], float],
        day: Callable[[], str] = today,
    ) -> None:
        self.state = state
        self.max_cost = max_cost
        self.max_minutes = max_minutes
        self.daily_cost = daily_cost
        self.monotonic = monotonic
        self.day = day
        self.cost = 0.0
        self.started = monotonic()
        self.settled = False

    def spent_today(self) -> float:
        """Dollars recorded today before this attempt."""
        return number(self.state.get(SPEND_KEY + self.day())) or 0.0

    def start(self) -> None:
        """Refuse to start once the daily cap is used up."""
        spent = self.spent_today()
        if self.daily_cost and spent >= self.daily_cost:
            raise CapError(
                f"Daily spend cap ${self.daily_cost:.2f} reached "
                f"(${spent:.2f} spent today)"
            )

    def observe(self, event: HarnessEvent) -> None:
        """Add reported cost; stop before the agent does more past a cap.

        Runtimes often report cost only at the end, so the cost cap stops a
        run at its next step after the cap is passed, never a finished one.
        """
        if event.kind == EventKind.USAGE:
            if event.usage is not None and event.usage.cost_usd is not None:
                self.cost += event.usage.cost_usd
            return
        if event.kind == EventKind.RESULT:
            return
        if self.max_cost and self.cost > self.max_cost:
            raise CapError(
                f"Stopped at ${self.cost:.2f}, over the ${self.max_cost:.2f} "
                "cap per attempt"
            )
        if self.daily_cost and self.spent_today() + self.cost > self.daily_cost:
            raise CapError(f"Stopped: the daily ${self.daily_cost:.2f} cap is used up")
        minutes = (self.monotonic() - self.started) / 60
        if self.max_minutes and minutes > self.max_minutes:
            raise CapError(
                f"Stopped after {minutes:.0f} minutes, over the "
                f"{self.max_minutes:g}-minute cap per attempt"
            )

    def settle(self) -> float:
        """Record this attempt's cost in today's total (once)."""
        if not self.settled and self.cost:
            key = SPEND_KEY + self.day()
            self.state.set(key, round(self.spent_today() + self.cost, 6))
        self.settled = True
        return self.cost


class Keepalive:
    """Call ``beat`` every ``interval`` seconds on a thread until stopped."""

    def __init__(
        self, beat: Callable[[], None], interval: float = KEEPALIVE_SECONDS
    ) -> None:
        self.beat = beat
        self.interval = interval
        self.error: ConflictError | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        """Beat until stopped or the lease is lost."""
        while not self._stop.wait(self.interval):
            try:
                self.beat()
            except ConflictError as err:
                self.error = err
                return
            except AppError:  # pragma: no cover - a busy database; retry next beat
                continue

    def check(self) -> None:
        """Raise when a beat found the lease lost."""
        if self.error is not None:
            raise self.error

    def __enter__(self) -> Keepalive:
        """Start beating."""
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        """Stop beating."""
        self._stop.set()
        self._thread.join(timeout=self.interval + 5)


class Evidence:
    """Whether a result names how it was verified."""

    VAGUE = frozenset({"", "none", "n/a", "na", "no", "none reported", "-"})

    @classmethod
    def outcome(cls, outcome: str, verification: str) -> str:
        """A completed result without verification needs more work."""
        if outcome == "completed" and verification.strip().lower() in cls.VAGUE:
            return "needs_more_work"
        return outcome
