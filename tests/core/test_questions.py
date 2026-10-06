"""Question batches, answer pairing, and the resume phase from history."""

from __future__ import annotations

from jot.core.models import EventBody, EventKind, Status, TaskEvent
from jot.core.questions import Questions


def event(event_id: int, kind: EventKind, body: EventBody) -> TaskEvent:
    """Build one history event."""
    return TaskEvent(id=event_id, task_id=1, actor="agent", kind=kind, body=body)


class TestQuestions:
    """Pure derivations over task history."""

    def test_latest_batch_by_run(self) -> None:
        """Only the most recent asking run's questions are open."""
        events = [
            event(1, EventKind.QUESTION, {"text": "old", "run_id": 1}),
            event(2, EventKind.QUESTION, {"text": "a", "run_id": 2, "choices": ["x"]}),
            event(3, EventKind.QUESTION, {"text": "b", "run_id": 2}),
            event(4, EventKind.ANSWER, {"text": "first", "question_id": 2}),
            event(5, EventKind.ANSWER, {"text": "second", "question_id": 2}),
        ]
        assert [q.id for q in Questions.latest(events)] == [2, 3]
        assert Questions.view(events) == [
            {"id": 2, "qid": None, "text": "a", "choices": ["x"], "answer": "second"},
            {"id": 3, "qid": None, "text": "b", "choices": [], "answer": None},
        ]

    def test_legacy_batch_after_plan(self) -> None:
        """Questions without a run id group after the latest plan."""
        events = [
            event(1, EventKind.QUESTION, {"text": "before"}),
            event(2, EventKind.PLAN, {"text": "p"}),
            event(3, EventKind.QUESTION, {"text": "after"}),
        ]
        assert [q.id for q in Questions.latest(events)] == [3]
        assert Questions.latest([event(1, EventKind.QUESTION, {"text": "q"})])
        assert Questions.latest([]) == []

    def test_resume_status(self) -> None:
        """The phase that parked the task is resumed; planning is the default."""

        def parked(source: Status) -> TaskEvent:
            return event(
                9, EventKind.STATUS, {"from": source, "to": Status.NEEDS_INPUT}
            )

        assert Questions.resume_status([parked(Status.EXECUTING)]) is Status.EXECUTING
        assert Questions.resume_status([parked(Status.PLANNING)]) is Status.PLANNING
        assert Questions.resume_status([]) is Status.PLANNING
