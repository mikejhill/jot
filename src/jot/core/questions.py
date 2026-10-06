"""Agent questions and owner answers derived from task history."""

from __future__ import annotations

from jot.core.models import EventKind, Status, TaskEvent


class Questions:
    """Group question events into batches and pair them with their answers."""

    @staticmethod
    def latest(events: list[TaskEvent]) -> list[TaskEvent]:
        """Return the questions raised by the most recent asking run.

        Runner questions carry the ``run_id`` that asked them. Questions posted
        without one (external agents, older history) form a batch after the
        latest plan.
        """
        questions = [e for e in events if e.kind is EventKind.QUESTION]
        if not questions:
            return []
        run_id = questions[-1].body.get("run_id")
        if run_id is not None:
            return [q for q in questions if q.body.get("run_id") == run_id]
        plans = [e.id for e in events if e.kind is EventKind.PLAN]
        floor = plans[-1] if plans else 0
        return [q for q in questions if q.id > floor and q.body.get("run_id") is None]

    @staticmethod
    def answers(events: list[TaskEvent]) -> dict[int, str]:
        """Map question event ids to their latest answer text."""
        result: dict[int, str] = {}
        for event in events:
            question = event.body.get("question_id")
            text = event.body.get("text")
            if event.kind is EventKind.ANSWER and isinstance(question, int):
                result[question] = text if isinstance(text, str) else ""
        return result

    @staticmethod
    def choices(question: TaskEvent) -> list[str]:
        """Return a question's suggested answers, if any."""
        value = question.body.get("choices")
        return list(value) if isinstance(value, list) else []

    @classmethod
    def view(cls, events: list[TaskEvent]) -> list[dict[str, object]]:
        """Return the latest batch with choices and any saved answer."""
        answers = cls.answers(events)
        return [
            {
                "id": q.id,
                "qid": q.body.get("qid"),
                "text": q.body.get("text", ""),
                "choices": cls.choices(q),
                "answer": answers.get(q.id),
            }
            for q in cls.latest(events)
        ]

    @staticmethod
    def resume_status(events: list[TaskEvent]) -> Status:
        """Return the run phase a needs_input task was parked from."""
        for event in reversed(events):
            if (
                event.kind is EventKind.STATUS
                and event.body.get("to") == Status.NEEDS_INPUT
            ):
                if event.body.get("from") == Status.EXECUTING:
                    return Status.EXECUTING
                break
        return Status.PLANNING
