"""Structured questions pause runs in needs_input; answers resume the same phase."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from jot.agents.base import AgentEvent, RunRequest
from jot.agents.fake import FakeBackend
from jot.config import Config, JotHome
from jot.core.models import EventKind, Flow, Run, Status, Task
from jot.core.workflow import Workflow
from jot.db.connection import Database
from jot.db.repository import TaskRepository
from jot.exceptions import WorkflowError
from jot.services.bus import EventBus
from jot.services.runner import PauseOutcome, PlanOutcome, Question, RunService

PLAN_WITH_QUESTIONS = json.dumps(
    {
        "summary": "Needs a database decision",
        "plan": "## Steps\n1. Pick a database",
        "questions": [
            {"id": "db", "text": "Which database?", "choices": ["Postgres", "SQLite"]},
            "Any deadline?",
        ],
    }
)
PLAN_DONE = json.dumps({"summary": "s", "plan": "1. Use Postgres", "questions": []})


class Recorder:
    """Capture every prompt the fake backend receives."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.prompts: list[str] = []
        original = FakeBackend.run
        prompts = self.prompts

        async def run(
            self: FakeBackend, request: RunRequest
        ) -> AsyncIterator[AgentEvent]:
            prompts.append(request.prompt)
            async for event in original(self, request):
                yield event

        monkeypatch.setattr(FakeBackend, "run", run)


@pytest.fixture
def service(db: Database, home: JotHome, config: Config) -> RunService:
    """Runner on the fake backend."""
    return RunService(db, home, config, EventBus())


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Prompt recorder around the fake backend."""
    return Recorder(monkeypatch)


def ready(db: Database) -> Task:
    """Create a ready task."""
    task = TaskRepository(db).create(Task(title="Store data", raw_input="store"))
    return Workflow(db).move(task.id, Status.READY)


class TestQuestionFlow:
    """End-to-end pause and resume."""

    def test_plan_questions_pause_and_resume_planning(
        self, db: Database, service: RunService, recorder: Recorder
    ) -> None:
        """Plan questions -> needs_input; answers resume planning -> approval."""
        task = ready(db)
        tasks = TaskRepository(db)
        FakeBackend.replies = [PLAN_WITH_QUESTIONS, PLAN_DONE]

        async def plan() -> Run:
            return await service.wait((await service.start(task.id)).id)

        asyncio.run(plan())
        assert tasks.get(task.id).status is Status.NEEDS_INPUT
        questions = [
            e for e in tasks.events.for_task(task.id) if e.kind is EventKind.QUESTION
        ]
        assert [q.body["qid"] for q in questions] == ["db", "q2"]
        assert questions[0].body["choices"] == ["Postgres", "SQLite"]
        assert questions[1].body["choices"] == []
        assert {q.body["run_id"] for q in questions} == {1}

        async def answer() -> Run:
            run = await service.respond(task.id, {questions[0].id: " Postgres "})
            return await service.wait(run.id)

        resumed = asyncio.run(answer())
        assert resumed.phase == "plan"
        assert tasks.get(task.id).status is Status.AWAITING_APPROVAL
        answers = [
            e for e in tasks.events.for_task(task.id) if e.kind is EventKind.ANSWER
        ]
        assert len(answers) == 1
        assert answers[0].body == {
            "text": "Postgres",
            "question_id": questions[0].id,
            "question": "Which database?",
        }
        prompt = recorder.prompts[-1]
        assert 'answer by owner to "Which database?": Postgres' in prompt
        assert "(choices: Postgres | SQLite)" in prompt
        assert "## The owner answered your questions" in prompt
        assert "Continue the same plan" in prompt
        assert "- Q: Any deadline?\n  A: (no answer; use your judgement)" in prompt
        assert "answered your questions" not in recorder.prompts[0]

    def test_execute_questions_resume_execution(
        self, db: Database, service: RunService, recorder: Recorder
    ) -> None:
        """An execute run that asks parks in needs_input and resumes executing."""
        task = ready(db)
        tasks = TaskRepository(db)
        FakeBackend.replies = [
            '{"summary": "Half done", "questions": ["Which branch?"]}',
            "Done: finished.",
        ]

        async def scenario() -> tuple[Run, Status, Run]:
            first = await service.wait(
                (await service.start(task.id, flow=Flow.DIRECT)).id
            )
            parked = tasks.get(task.id).status
            second = await service.wait((await service.respond(task.id, {})).id)
            return first, parked, second

        first, parked, second = asyncio.run(scenario())
        assert parked is Status.NEEDS_INPUT
        assert first.summary == "Half done"
        assert second.phase == "execute"
        assert tasks.get(task.id).status is Status.REVIEW
        assert "Continue the same execution" in recorder.prompts[-1]
        kinds = [e.kind for e in tasks.events.for_task(task.id)]
        assert EventKind.ANSWER not in kinds
        assert kinds.count(EventKind.RESULT) == 1

    def test_respond_guards(self, db: Database, service: RunService) -> None:
        """Only needs_input tasks accept answers, and only for open questions."""
        task = ready(db)
        FakeBackend.replies = [PLAN_WITH_QUESTIONS]

        async def scenario() -> None:
            with pytest.raises(WorkflowError, match="not needs_input"):
                await service.respond(task.id, {})
            await service.wait((await service.start(task.id)).id)
            with pytest.raises(WorkflowError, match="Not open questions"):
                await service.respond(task.id, {999: "x"})

        asyncio.run(scenario())
        assert TaskRepository(db).get(task.id).status is Status.NEEDS_INPUT

    def test_send_back_from_needs_input(
        self, db: Database, service: RunService
    ) -> None:
        """The owner may abandon the questions and return the task to ready."""
        task = ready(db)
        FakeBackend.replies = [PLAN_WITH_QUESTIONS]

        async def scenario() -> Task:
            await service.wait((await service.start(task.id)).id)
            return await service.send_back(task.id, "rethink")

        assert asyncio.run(scenario()).status is Status.READY


class TestParsing:
    """Question parsing is tolerant and bounded."""

    def test_questions(self) -> None:
        """Objects and strings parse; blanks drop; counts are capped."""
        parsed = Question.parse_all(
            [
                {"id": "a", "text": "Pick", "choices": ["x", "", 3, *"abcdefghij"]},
                "  ",
                {"text": ""},
                "Plain?",
                42,
            ]
        )
        assert parsed[0] == Question(
            "a", "Pick", ("x", "a", "b", "c", "d", "e", "f", "g")
        )
        assert parsed[1] == Question("q2", "Plain?")
        assert len(parsed) == 2
        assert len(Question.parse_all([f"q{i}" for i in range(20)])) == 10
        assert Question.parse_all("nope") == ()

    def test_outcomes(self) -> None:
        """Plans keep their questions; pauses need at least one question."""
        plan = PlanOutcome.parse('{"plan": "p", "questions": ["q"]}')
        assert plan.questions == (Question("q1", "q"),)
        assert PauseOutcome.parse("Done: all good.") is None
        assert PauseOutcome.parse('Used {"questions": []} in config') is None
        paused = PauseOutcome.parse('```json\n{"questions": ["Which?"]}\n```')
        assert paused == PauseOutcome("", (Question("q1", "Which?"),))
