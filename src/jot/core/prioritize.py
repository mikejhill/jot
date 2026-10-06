"""Deterministic impact, project, age, and deadline ordering."""

from __future__ import annotations

from datetime import UTC, datetime

from jot.core.models import Clock, Criticality, Task


class Prioritizer:
    """Rank tasks against a single captured clock value."""

    def __init__(
        self, priorities: dict[int, float], now: datetime | None = None
    ) -> None:
        self._priorities = priorities
        self._now = now or Clock.now()

    def score(self, task: Task) -> float:
        """Multiply impact by project priority and one plus age in thirty-day units."""
        weights = {
            Criticality.LOW: 1,
            Criticality.MEDIUM: 2,
            Criticality.HIGH: 4,
            Criticality.CRITICAL: 8,
        }
        age_days = max(0, (self._now - task.created_at).total_seconds() / 86400)
        priority = self._priorities.get(task.project_id or 0, 1)
        return weights[task.criticality] * priority * (1 + age_days / 30)

    def rank(self, tasks: list[Task]) -> list[Task]:
        """Sort score descending, deadline ascending, then id ascending."""
        return sorted(tasks, key=self._key)

    def _key(self, task: Task) -> tuple[float, datetime, int]:
        """Build a stable ordering key, putting missing deadlines last."""
        return (
            -self.score(task),
            task.due_at or datetime.max.replace(tzinfo=UTC),
            task.id,
        )
