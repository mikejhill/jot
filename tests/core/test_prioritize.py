"""Deterministic priority scoring and deadline tie breaks."""

from __future__ import annotations

from datetime import timedelta

from jot.core.models import Clock, Criticality, Task
from jot.core.prioritize import Prioritizer


class TestPrioritizer:
    """Impact, project priority, age, and deadlines determine ordering."""

    def test_score_and_ties(self) -> None:
        """Equal scores sort by deadline then id with missing deadlines last."""
        now = Clock.now()
        prioritizer = Prioritizer({1: 2}, now)
        old = Task(
            id=1,
            title="old",
            project_id=1,
            criticality=Criticality.HIGH,
            created_at=now - timedelta(days=30),
        )
        assert prioritizer.score(old) == 16
        late = Task(id=2, title="late", created_at=now, due_at=now + timedelta(days=2))
        soon = Task(id=3, title="soon", created_at=now, due_at=now + timedelta(days=1))
        missing = Task(id=4, title="missing", created_at=now)
        assert [t.id for t in prioritizer.rank([missing, late, old, soon])] == [
            1,
            3,
            2,
            4,
        ]
        assert (
            Prioritizer({}).score(
                Task(title="future", created_at=now + timedelta(days=1))
            )
            == 2
        )
