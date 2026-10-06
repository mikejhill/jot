"""Plan/approve/execute agent runs for tasks, with leases and worktrees."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jot.agents.base import (
    AgentBackend,
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentMode,
    Json,
    JsonObject,
    JsonValue,
    RunRequest,
)
from jot.agents.registry import BackendRegistry
from jot.core.models import Clock, EventBody, EventKind, Flow, Run, Status
from jot.core.questions import Questions
from jot.core.workflow import Workflow
from jot.db.repository import ProjectRepository, RunRepository, TaskRepository
from jot.exceptions import AppError, WorkflowError
from jot.services.bus import Topic
from jot.services.instructions import InstructionStore
from jot.services.workspace import GitWorkspaces, Workspace

if TYPE_CHECKING:
    from jot.config import Config, JotHome
    from jot.core.models import Task, TaskEvent
    from jot.db.connection import Database
    from jot.services.bus import EventBus

logger = logging.getLogger(__name__)

LEASE_SECONDS = 600
HEARTBEAT_SECONDS = 120
HISTORY_KINDS = {
    EventKind.COMMENT,
    EventKind.PLAN,
    EventKind.QUESTION,
    EventKind.ANSWER,
    EventKind.APPROVAL,
    EventKind.RESULT,
}

QUESTION_FORMAT = """{"id": "q1", "text": "<one question for the owner>",
  "choices": ["<optional suggested answer>", ...]}"""
MAX_QUESTIONS = 10
MAX_CHOICES = 8

PLAN_INSTRUCTIONS = f"""## Your job now: PLAN (read-only)
Investigate the task and the working directory without changing anything.
Then reply with ONLY a JSON object (no code fences):
{{"summary": "<one paragraph refined understanding>",
 "plan": "<markdown: numbered steps, files to touch, verification, rollback>",
 "questions": [{QUESTION_FORMAT}, ...]}}
Use an empty questions list when nothing blocks execution. Questions pause the
task for the owner; their answers come back to you to finish the plan."""

EXECUTE_INSTRUCTIONS = f"""## Your job now: EXECUTE
Carry out the task (following the approved plan and the owner's answers, if any)
in the current working directory. Verify your work (tests, lint, or a check that
fits the task). If this is a git repository, commit your changes on the current
branch with a clear message; never push and never switch branches. Finish with a
short markdown summary: what changed, how it was verified, and any follow-ups.
If you cannot continue without the owner's input, stop and reply with ONLY a
JSON object (no code fences) instead; you will be resumed in the same working
directory with their answers:
{{"summary": "<markdown: progress so far>", "questions": [{QUESTION_FORMAT}, ...]}}"""

CONTINUE_INSTRUCTIONS = """## The owner answered your questions
Continue the same {phase} using these answers. An unanswered question means the
owner leaves it to your judgement. Ask again only if something still blocks you.

{pairs}"""


@dataclass(frozen=True, slots=True)
class Question:
    """One structured question from an agent, with optional suggested answers."""

    id: str
    text: str
    choices: tuple[str, ...] = ()

    @classmethod
    def parse_all(cls, value: JsonValue | None) -> tuple[Question, ...]:
        """Accept question objects or plain strings; drop blanks; cap the counts."""
        if not isinstance(value, list):
            return ()
        result: list[Question] = []
        for item in value:
            data = Json.obj(item)
            text = (Json.text(item) or Json.text(data.get("text"))).strip()
            if not text:
                continue
            raw = data.get("choices")
            choices = (
                [Json.text(c).strip() for c in raw] if isinstance(raw, list) else []
            )
            qid = Json.text(data.get("id")).strip() or f"q{len(result) + 1}"
            result.append(cls(qid, text, tuple(c for c in choices if c)[:MAX_CHOICES]))
        return tuple(result[:MAX_QUESTIONS])


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    """Parsed plan-phase output."""

    summary: str
    plan: str
    questions: tuple[Question, ...]

    @classmethod
    def parse(cls, text: str) -> PlanOutcome:
        """Parse the agent's JSON plan, falling back to the raw text as the plan."""
        try:
            data = AgentBackend.parse_json_object(text)
        except AgentError:
            return cls(summary="", plan=text.strip(), questions=())
        return cls(
            summary=Json.text(data.get("summary")),
            plan=Json.text(data.get("plan")) or text.strip(),
            questions=Question.parse_all(data.get("questions")),
        )


@dataclass(frozen=True, slots=True)
class PauseOutcome:
    """Execute-phase output that stops to ask the owner questions."""

    summary: str
    questions: tuple[Question, ...]

    @classmethod
    def parse(cls, text: str) -> PauseOutcome | None:
        """Return the pause request, or None when the run finished normally."""
        try:
            data = AgentBackend.parse_json_object(text)
        except AgentError:
            return None
        questions = Question.parse_all(data.get("questions"))
        if not questions:
            return None
        return cls(Json.text(data.get("summary")), questions)


class RunService:
    """Start, track, and cancel agent runs; publishes progress on the bus."""

    def __init__(
        self, db: Database, home: JotHome, config: Config, bus: EventBus
    ) -> None:
        self.db = db
        self.home = home
        self.config = config
        self.bus = bus
        self.tasks = TaskRepository(db)
        self.projects = ProjectRepository(db)
        self.runs = RunRepository(db)
        self.workflow = Workflow(db, config.drawdown.default_flow)
        self.instructions = InstructionStore(home)
        self.git = GitWorkspaces(self._worktree_root())
        self.logs = home.path / "runs"
        self._active: dict[int, asyncio.Task[None]] = {}
        self._slots = asyncio.Semaphore(config.drawdown.concurrency)

    def _worktree_root(self) -> Path:
        """Return the configured worktree folder (default ``$JOT_HOME/worktrees``)."""
        configured = self.config.execution.worktree_root.strip()
        return (
            Path(configured).expanduser()
            if configured
            else self.home.path / "worktrees"
        )

    # Public API

    async def start(
        self,
        task_id: int,
        *,
        flow: Flow | None = None,
        backend: str | None = None,
        model: str | None = None,
    ) -> Run:
        """Claim a ready task and launch its next phase in the background.

        Planned flow runs a read-only plan; direct flow executes immediately.

        Raises:
            WorkflowError: The task is not ready or another agent holds it.
        """
        task = self.tasks.get(task_id)
        if task.status is not Status.READY:
            raise WorkflowError(
                f"Task {task_id} is {task.status}; only ready tasks run"
            )
        resolved = self.workflow.flow_for(task, flow)
        phase = AgentMode.PLAN if resolved is Flow.PLANNED else AgentMode.EXECUTE
        return self._launch(task, phase, backend, resolved, model)

    async def approve(
        self,
        task_id: int,
        note: str | None = None,
        backend: str | None = None,
        model: str | None = None,
    ) -> Run:
        """Approve an awaiting_approval plan and launch the execute run.

        Raises:
            WorkflowError: The task is not awaiting approval.
        """
        task = self.tasks.get(task_id)
        if task.status is not Status.AWAITING_APPROVAL:
            raise WorkflowError(
                f"Task {task_id} is {task.status}, not awaiting approval"
            )
        with self.db.write():
            self.tasks.audit(
                task_id, EventKind.APPROVAL, {"text": note or "Approved"}, actor="owner"
            )
        return self._launch(task, AgentMode.EXECUTE, backend, Flow.PLANNED, model)

    async def send_back(self, task_id: int, comment: str) -> Task:
        """Return a task in awaiting_approval or review to ready with feedback."""
        with self.db.write():
            self.tasks.audit(
                task_id, EventKind.COMMENT, {"text": comment}, actor="owner"
            )
        task = self.workflow.move(task_id, Status.READY, actor="owner")
        self._publish_task(task_id)
        return task

    async def respond(
        self,
        task_id: int,
        answers: dict[int, str],
        backend: str | None = None,
        model: str | None = None,
    ) -> Run:
        """Save the owner's answers and resume the phase that asked.

        Answers map question event ids to text; blank or missing answers leave
        the question to the agent's judgement.

        Raises:
            WorkflowError: The task is not waiting on input, or an id is not one
                of its open questions.
        """
        task = self.tasks.get(task_id)
        if task.status is not Status.NEEDS_INPUT:
            raise WorkflowError(f"Task {task_id} is {task.status}, not needs_input")
        events = self.tasks.events.for_task(task_id)
        questions = {q.id: q for q in Questions.latest(events)}
        unknown = sorted(set(answers) - set(questions))
        if unknown:
            raise WorkflowError(
                f"Not open questions on task {task_id}: "
                + ", ".join(str(i) for i in unknown)
            )
        with self.db.write():
            for question_id, text in answers.items():
                if text.strip():
                    self.tasks.audit(
                        task_id,
                        EventKind.ANSWER,
                        {
                            "text": text.strip(),
                            "question_id": question_id,
                            "question": questions[question_id].body.get("text"),
                        },
                        actor="owner",
                    )
        resume = Questions.resume_status(events)
        phase = AgentMode.EXECUTE if resume is Status.EXECUTING else AgentMode.PLAN
        return self._launch(task, phase, backend, self.workflow.flow_for(task), model)

    async def cancel(self, run_id: int) -> None:
        """Stop a running run; its task lease is released by the run itself.

        Raises:
            WorkflowError: The run is not active in this process.
        """
        job = self._active.get(run_id)
        if job is None:
            raise WorkflowError(f"Run {run_id} is not active in this server")
        job.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await job

    def active(self) -> list[int]:
        """Return ids of runs currently executing in this process."""
        return sorted(self._active)

    async def shutdown(self) -> None:
        """Cancel all active runs (server shutdown)."""
        for run_id in self.active():
            with contextlib.suppress(AppError):
                await self.cancel(run_id)

    async def wait(self, run_id: int) -> Run:
        """Wait for a run launched by this process to finish (CLI use)."""
        job = self._active.get(run_id)
        if job is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await job
        return self.runs.get(run_id)

    def log(self, run_id: int) -> list[JsonObject]:
        """Return the stored log lines of a run."""
        path = self.logs / f"{run_id}.jsonl"
        if not path.is_file():
            return []
        lines = (Json.loads(line) for line in path.read_text("utf-8").splitlines())
        return [line for line in lines if isinstance(line, dict)]

    # Launch and lifecycle

    def _launch(
        self,
        task: Task,
        phase: AgentMode,
        backend: str | None,
        flow: Flow,
        model: str | None = None,
    ) -> Run:
        """Claim the task, record a run, and schedule the background job."""
        name = backend or self.config.drawdown.backend
        resolved = self.config.model_for(name, phase.value, model)
        agent = BackendRegistry.create(name, resolved)
        owner = f"jot:{name}:{os.getpid()}:{task.id}"
        target = Status.PLANNING if phase is AgentMode.PLAN else Status.EXECUTING
        if not self.workflow.claim(task.id, owner, target, LEASE_SECONDS, flow=flow):
            raise WorkflowError(f"Task {task.id} is already claimed by another agent")
        run = self.runs.create(
            Run(
                task_id=task.id,
                backend=name,
                model=resolved or "default",
                phase=phase.value,
                status="running",
            )
        )
        job = asyncio.create_task(self._job(run, agent, owner, phase))
        self._active[run.id] = job
        job.add_done_callback(lambda _: self._active.pop(run.id, None))
        self._publish_run(run)
        self._publish_task(task.id)
        return run

    async def _job(
        self, run: Run, agent: AgentBackend, owner: str, phase: AgentMode
    ) -> None:
        """Run one phase end to end, always releasing the lease."""
        heartbeat = asyncio.create_task(self._heartbeat(run.task_id, owner))
        status, summary, target = "failed", "", None
        try:
            async with self._slots:
                workspace = await self._workspace(run, phase)
                result = await self._stream(run, agent, phase, workspace)
                if result.is_error:
                    summary = result.text[:2000] or "agent reported an error"
                elif phase is AgentMode.PLAN:
                    summary, target = self._record_plan(run, result.text)
                    status = "succeeded"
                else:
                    summary, target = await self._record_result(run, result, workspace)
                    status = "succeeded"
        except asyncio.CancelledError:
            status, summary = "cancelled", "Cancelled by owner"
            raise
        except AppError as err:
            summary = str(err)
            logger.warning("run %s failed: %s", run.id, err)
        finally:
            heartbeat.cancel()
            self._finish(run, owner, status, summary, target)

    def _finish(
        self, run: Run, owner: str, status: str, summary: str, target: Status | None
    ) -> None:
        """Persist the run outcome and release the claim to the next status."""
        try:
            self.runs.update(
                run.id,
                {"status": status, "summary": summary, "ended_at": Clock.stamp()},
            )
            if status != "succeeded":
                with self.db.write():
                    self.tasks.audit(
                        run.task_id,
                        EventKind.COMMENT,
                        {"text": f"Run {run.id} {status}: {summary[:1000]}"},
                        actor=owner,
                    )
            self.workflow.release(run.task_id, owner, target_status=target)
        except AppError:
            logger.exception("could not finalize run %s", run.id)
        self._publish_run(self.runs.get(run.id))
        self._publish_task(run.task_id)

    async def _heartbeat(self, task_id: int, owner: str) -> None:
        """Extend the lease periodically while the run is alive."""
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            if not self.workflow.heartbeat(task_id, owner, LEASE_SECONDS):
                logger.warning("lost lease on task %s", task_id)
                return

    # Work

    async def _workspace(self, run: Run, phase: AgentMode) -> Workspace:
        """Pick the working directory; execution in git repos uses a worktree."""
        task = self.tasks.get(run.task_id)
        project = self.projects.get(task.project_id) if task.project_id else None
        configured = task.repo_path or (project.repo_path if project else None)
        if not configured:
            scratch = self.home.path / "workspaces" / str(task.id)
            scratch.mkdir(parents=True, exist_ok=True)
            return Workspace(cwd=scratch)
        path = Path(configured).expanduser()
        if not path.is_dir():
            raise AppError(f"Repository path does not exist: {path}")
        isolate = self.config.execution.isolation == "worktree"
        if phase is AgentMode.PLAN or not isolate or not await self.git.is_repo(path):
            return Workspace(cwd=path)
        workspace = await self.git.worktree(path, task.id, task.title)
        self.runs.update(
            run.id, {"worktree": str(workspace.worktree), "branch": workspace.branch}
        )
        return workspace

    async def _stream(
        self, run: Run, agent: AgentBackend, phase: AgentMode, workspace: Workspace
    ) -> AgentEvent:
        """Run the agent, logging and publishing every event; return the result."""
        task = self.tasks.get(run.task_id)
        request = RunRequest(
            prompt=self._prompt(task, phase, workspace),
            cwd=workspace.cwd,
            mode=phase,
            system=self.instructions.compose("drawdown", self._slug(task)),
        )
        self.logs.mkdir(parents=True, exist_ok=True)
        result = AgentEvent(AgentEventKind.RESULT, "no result", is_error=True)
        with (self.logs / f"{run.id}.jsonl").open("a", encoding="utf-8") as log:
            async for event in agent.run(request):
                line: JsonObject = {
                    "run_id": run.id,
                    "task_id": run.task_id,
                    "kind": event.kind.value,
                    "text": event.text,
                    "ts": Clock.stamp(),
                }
                log.write(json.dumps(line, ensure_ascii=False) + "\n")
                log.flush()
                self.bus.publish(Topic.RUN_LOG, line)
                if event.kind is AgentEventKind.RESULT:
                    result = event
        if result.session_id or result.cost_usd is not None:
            self.runs.update(
                run.id, {"session_id": result.session_id, "cost": result.cost_usd}
            )
        return result

    def _prompt(self, task: Task, phase: AgentMode, workspace: Workspace) -> str:
        """Compose task details, history, and phase instructions."""
        events = self.tasks.events.for_task(task.id)
        history = [
            f"- [{e.ts:%Y-%m-%d %H:%M}] {self._history_line(e)}"
            for e in events
            if e.kind in HISTORY_KINDS and e.body.get("text")
        ]
        location = f"Working directory: {workspace.cwd}"
        if workspace.branch:
            location += f" (git worktree on branch {workspace.branch})"
        sections = [
            f"# Task {task.id}: {task.title}",
            f"Project: {self._slug(task) or '(none)'} | Type: {task.type} | "
            f"Criticality: {task.criticality} | Labels: {', '.join(task.labels)}",
            f"## Description\n\n{task.description or '(none)'}",
            f"## Original note\n\n{task.raw_input}",
            "## History\n\n" + ("\n".join(history) or "(none)"),
            location,
            PLAN_INSTRUCTIONS if phase is AgentMode.PLAN else EXECUTE_INSTRUCTIONS,
        ]
        resumed = self._continuation(events, phase)
        if resumed:
            sections.append(resumed)
        return "\n\n".join(sections)

    @staticmethod
    def _history_line(event: TaskEvent) -> str:
        """Render one history event, linking answers to their questions."""
        text = event.body.get("text")
        if event.kind is EventKind.ANSWER and event.body.get("question"):
            return f'answer by {event.actor} to "{event.body["question"]}": {text}'
        line = f"{event.kind} by {event.actor}: {text}"
        choices = Questions.choices(event)
        return f"{line} (choices: {' | '.join(choices)})" if choices else line

    @staticmethod
    def _continuation(events: list[TaskEvent], phase: AgentMode) -> str:
        """Pair the latest questions with answers when resuming from needs_input."""
        moves = [e for e in events if e.kind is EventKind.STATUS]
        if not moves or moves[-1].body.get("from") != Status.NEEDS_INPUT:
            return ""
        answers = Questions.answers(events)
        pairs = "\n".join(
            f"- Q: {q.body.get('text')}\n  A: "
            + (answers.get(q.id) or "(no answer; use your judgement)")
            for q in Questions.latest(events)
        )
        name = "plan" if phase is AgentMode.PLAN else "execution"
        return CONTINUE_INSTRUCTIONS.format(phase=name, pairs=pairs or "(none)")

    def _record_questions(
        self, run: Run, questions: tuple[Question, ...], actor: str
    ) -> None:
        """Store each question with its id, choices, and the asking run."""
        for question in questions:
            self.tasks.audit(
                run.task_id,
                EventKind.QUESTION,
                {
                    "text": question.text,
                    "qid": question.id,
                    "choices": list(question.choices),
                    "run_id": run.id,
                },
                actor=actor,
            )

    def _record_plan(self, run: Run, text: str) -> tuple[str, Status]:
        """Store plan and questions; questions wait for input, else approval."""
        outcome = PlanOutcome.parse(text)
        actor = f"agent:{run.backend}"
        with self.db.write():
            if outcome.summary:
                self.tasks.audit(
                    run.task_id,
                    EventKind.COMMENT,
                    {"text": outcome.summary},
                    actor=actor,
                )
            self.tasks.audit(
                run.task_id,
                EventKind.PLAN,
                {"text": outcome.plan, "run_id": run.id},
                actor=actor,
            )
            self._record_questions(run, outcome.questions, actor)
        target = Status.NEEDS_INPUT if outcome.questions else Status.AWAITING_APPROVAL
        return outcome.summary or outcome.plan[:500], target

    async def _record_result(
        self, run: Run, result: AgentEvent, workspace: Workspace
    ) -> tuple[str, Status]:
        """Store the execution summary and diffstat; next status is review.

        An execution that stops with questions records them and waits for input.
        """
        actor = f"agent:{run.backend}"
        paused = PauseOutcome.parse(result.text)
        if paused is not None:
            summary = paused.summary or "Paused with questions for the owner"
            with self.db.write():
                self.tasks.audit(
                    run.task_id,
                    EventKind.COMMENT,
                    {"text": summary, "run_id": run.id},
                    actor=actor,
                )
                self._record_questions(run, paused.questions, actor)
            return summary[:2000], Status.NEEDS_INPUT
        diffstat = await self.git.diffstat(workspace)
        body: EventBody = {
            "text": result.text,
            "run_id": run.id,
            "branch": workspace.branch,
            "worktree": str(workspace.worktree) if workspace.worktree else None,
            "diffstat": diffstat,
        }
        with self.db.write():
            self.tasks.audit(run.task_id, EventKind.RESULT, body, actor=actor)
        return result.text[:2000], Status.REVIEW

    def _slug(self, task: Task) -> str | None:
        """Return the task's project slug, if any."""
        return self.projects.get(task.project_id).slug if task.project_id else None

    def _publish_task(self, task_id: int) -> None:
        """Notify live UIs that a task changed."""
        self.bus.publish(Topic.TASK, {"id": task_id})

    def _publish_run(self, run: Run) -> None:
        """Notify live UIs that a run changed state."""
        self.bus.publish(
            Topic.RUN, {"id": run.id, "task_id": run.task_id, "status": run.status}
        )
