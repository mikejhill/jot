"""The runner: plan and execute jots through the ``harness@1`` service.

A ``plan`` job claims the jot, runs the agent read-only in the project's
repository, and records either questions (``needs-input``) or a plan
(``awaiting-approval``). An ``execute`` job claims an approved (or direct)
jot, runs the agent in the jot's own git worktree, commits what it left
behind, and records a result (``review``). Every streamed event goes to a run
log under ``<JOT_HOME>/plugins/execution/runs``.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field

from jot.core.models import Origin, Record
from jot.exceptions import ConflictError, InvalidInputError
from jot.plugins.execution.claims import (
    DEFAULT_LEASE,
    PLUGIN,
    Claims,
    Phase,
    WorkState,
)
from jot.plugins.execution.workspace import Workspace
from jot.plugins.harness import SERVICE, HarnessService
from jot.plugins.harness.api import (
    EventKind,
    Harness,
    HarnessError,
    HarnessEvent,
    Permission,
    RunRequest,
    obj,
    text_of,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from jot.core.models import Comment, Jot, Metadata
    from jot.plugins.context import PluginContext

AGENT = "plugin:execution"
HEARTBEAT_SECONDS = 15.0
MAX_QUESTIONS = 10
MAX_CHOICES = 8
HISTORY = 30
SHOWN = 10
PLAN_ACTION = "execution.plan"
RUN_ACTION = "execution.run"
OUTCOMES = frozenset(
    {"completed", "needs_decision", "needs_more_work", "cannot_proceed"}
)
SYSTEM = (
    "You are a careful software agent working one item from the owner's task "
    "list. Follow the owner's guidance. Never publish anything (push, open "
    "pull requests, post messages) unless the guidance says you may."
)
QUESTION_FORMAT = (
    '{"text": "<one question for the owner>", '
    '"choices": ["<optional suggested answer>"]}'
)
PLAN_GUIDANCE = (
    "Investigate the item and the repository without changing anything. Name "
    "the files to touch, how you will verify the change, and how to roll it "
    "back. Ask only what blocks a good plan. Execution will run in a dedicated "
    "git worktree on its own branch, so do not plan branch creation."
)
EXECUTE_GUIDANCE = (
    "Carry out the approved plan in the current working directory. Verify your "
    "work with tests, lint, or a check that fits. Commit on the current branch "
    "with clear messages and never switch branches. Never read or change files "
    "outside the working directory."
)
PLAN_REPLY = f"""Reply with ONLY a JSON object (no code fences):
{{"summary": "<one paragraph: your understanding of the item>",
 "plan": "<markdown: numbered steps, files to touch, verification, rollback>",
 "questions": [{QUESTION_FORMAT}]}}
Use an empty questions list when nothing blocks execution."""
EXECUTE_REPLY = f"""Finish with ONLY a JSON object (no code fences):
{{"summary": "<markdown: what changed>",
 "verification": "<what you ran or checked, and the result; or why no check applies>",
 "outcome": "completed | needs_more_work | needs_decision | cannot_proceed",
 "questions": [{QUESTION_FORMAT}]}}
If you cannot continue without the owner, stop and list questions; you will be
resumed in the same working directory with their answers."""


class Question(Record):
    """One question from the agent, with optional suggested answers."""

    text: str
    choices: list[str] = Field(default_factory=list)

    @classmethod
    def parse_all(cls, value: object) -> list[Question]:
        """Accept question objects or strings; drop blanks; cap the counts."""
        if not isinstance(value, list):
            return []
        found: list[Question] = []
        for item in value:
            data = obj(item)
            text = (text_of(item) or text_of(data.get("text"))).strip()
            if not text:
                continue
            raw = data.get("choices")
            choices = [text_of(c).strip() for c in raw] if isinstance(raw, list) else []
            found.append(
                cls(text=text, choices=[c for c in choices if c][:MAX_CHOICES])
            )
        return found[:MAX_QUESTIONS]

    def body(self) -> str:
        """The question as a comment."""
        if not self.choices:
            return self.text
        options = "\n".join(f"- {choice}" for choice in self.choices)
        return f"{self.text}\n\nSuggested answers:\n{options}"


class Answer(Record):
    """What the agent said at the end of a run."""

    summary: str = ""
    plan: str = ""
    verification: str = ""
    outcome: str = "completed"
    questions: list[Question] = Field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> Answer:
        """Parse the agent's JSON; fall back to the raw text as the summary."""
        try:
            data = Harness.parse_object(text)
        except HarnessError:
            return cls(summary=text.strip())
        outcome = text_of(data.get("outcome")).strip().lower()
        return cls(
            summary=text_of(data.get("summary")).strip(),
            plan=text_of(data.get("plan")).strip(),
            verification=text_of(data.get("verification")).strip(),
            outcome=outcome if outcome in OUTCOMES else "completed",
            questions=Question.parse_all(data.get("questions")),
        )


class RunLog:
    """Append-only JSON lines of a run's events."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def folder(cls, home: Path) -> Path:
        """Where run logs live."""
        return home / "plugins" / PLUGIN / "runs"

    def write(self, event: HarnessEvent) -> None:
        """Append one event."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as out:
            out.write(event.model_dump_json(exclude_defaults=True) + "\n")

    def read(self) -> list[HarnessEvent]:
        """Every event logged so far."""
        if not self.path.is_file():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [HarnessEvent.model_validate_json(line) for line in lines if line]


class ExecutionSettings(Record):
    """Execution settings."""

    lease_seconds: int = Field(default=DEFAULT_LEASE, ge=30, le=86_400)
    repos: dict[str, str] = Field(
        default_factory=dict, description="Project slug -> repository path"
    )
    default_repo: str = Field(default="", description="Repository for other jots")
    worktree_root: str = Field(
        default="", description="Where worktrees go (default <JOT_HOME>/worktrees)"
    )
    allow_shell: list[str] = Field(
        default_factory=list,
        description="Projects whose execute runs may use a shell (full permission)",
    )
    plan_guidance: str = PLAN_GUIDANCE
    execute_guidance: str = EXECUTE_GUIDANCE


class Runner:
    """Run the plan and execute phases for one jot at a time."""

    def __init__(
        self,
        context: PluginContext,
        settings: ExecutionSettings,
        *,
        name: str = "job",
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.context = context
        self.settings = settings
        self.name = name
        self.monotonic = monotonic
        self.claims = Claims(
            context.session,
            correlation_id=context.correlation_id,
            origin=Origin.PLUGIN,
        )
        self._beat = 0.0

    # Configuration --------------------------------------------------------------

    @property
    def home(self) -> Path:
        """The data home folder."""
        return self.context.session.home.path

    def harness(self) -> HarnessService:
        """The harness router."""
        service = self.context.service(SERVICE)
        if not isinstance(service, HarnessService):  # pragma: no cover - host bug
            raise TypeError("harness@1 has the wrong type")
        return service

    def repo_for(self, jot: Jot) -> Path:
        """The repository a jot's project maps to."""
        path = self.settings.repos.get(jot.project or "") or self.settings.default_repo
        if not path:
            where = (
                f"project {jot.project}" if jot.project else "jots without a project"
            )
            raise InvalidInputError(
                f"No repository for {where}; run "
                f"`jot work repos --set {jot.project or '<project>'}=<path>`"
            )
        return Path(path).expanduser()

    def workspace(self, jot: Jot) -> Workspace:
        """The jot's worktree."""
        root = (
            Path(self.settings.worktree_root).expanduser()
            if self.settings.worktree_root
            else self.home / "worktrees"
        )
        return Workspace(self.repo_for(jot), root, jot.id, jot.title)

    def permission(self, jot: Jot) -> Permission:
        """Execute-phase permission: full (with a shell) only where allowed."""
        if jot.project and jot.project in self.settings.allow_shell:
            return Permission.FULL
        return Permission.WORKSPACE_WRITE

    # Context for the agent -----------------------------------------------------

    def history(self, jot_id: int) -> list[str]:
        """Earlier comments, with answers under their questions."""
        comments = self.context.queries.comments(jot_id)
        by_id = {c.id: c for c in comments}
        lines: list[str] = []
        for comment in comments[-HISTORY:]:
            parent = by_id.get(comment.reply_to) if comment.reply_to else None
            lines.append(self._line(comment, parent))
        return lines

    @staticmethod
    def _line(comment: Comment, parent: Comment | None) -> str:
        """One comment for the prompt."""
        if parent is not None and parent.kind == "execution.question":
            return f'- Owner answered "{parent.body}": {comment.body}'
        return f"- [{comment.kind}] {comment.author}: {comment.body}"

    def prompt(self, jot: Jot, guidance: str, reply: str, extra: str = "") -> str:
        """The item, its history, guidance, and the reply format."""
        sections = [
            f"## Item #{jot.id}: {jot.title}" + (f"\n\n{jot.body}" if jot.body else ""),
        ]
        if history := self.history(jot.id):
            sections.append("## History\n\n" + "\n".join(history))
        if extra:
            sections.append(extra)
        sections += [f"## Guidance\n\n{guidance}", f"## Reply\n\n{reply}"]
        return "\n\n".join(sections)

    @staticmethod
    def revision(jot: Jot, comment_id: int) -> str:
        """A plan revision: the plan comment plus the jot's title and body."""
        digest = hashlib.sha256(f"{comment_id}\n{jot.title}\n{jot.body}".encode())
        return digest.hexdigest()[:12]

    # Running -------------------------------------------------------------------

    def heartbeat(self, jot_id: int, *, force: bool = False) -> None:
        """Extend the job lease and the jot claim; raise when either was lost."""
        now = self.monotonic()
        if not force and now - self._beat < HEARTBEAT_SECONDS:
            return
        self._beat = now
        if self.context.job is not None:
            self.context.heartbeat()
        self.claims.heartbeat(jot_id, AGENT, lease=self.settings.lease_seconds)

    def stream(
        self, jot_id: int, request: RunRequest, action: str, log: RunLog
    ) -> HarnessEvent:
        """Run the agent, logging every event; return the final result."""
        events: Iterator[HarnessEvent] = self.harness().run(request, action=action)
        final: HarnessEvent | None = None
        try:
            for event in events:
                log.write(event)
                if event.kind == EventKind.RESULT:
                    final = event
                self.heartbeat(jot_id)
        finally:
            close = getattr(events, "close", None)
            if callable(close):
                close()
        if final is None:
            raise HarnessError("The agent ended without a result")
        if final.is_error:
            raise HarnessError(f"The agent failed: {final.text[:500]}")
        return final

    def log_for(self, jot_id: int, phase: Phase) -> RunLog:
        """A fresh log file for this run."""
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        job = self.context.job.id if self.context.job else self.name
        name = f"{jot_id}-{phase.value}-{job}-{stamp}.jsonl"
        return RunLog(RunLog.folder(self.home) / name)

    def ask(
        self, jot_id: int, questions: list[Question], asked_in: Phase, extra: Metadata
    ) -> WorkState:
        """Post questions and wait for the owner."""
        commands = self.context.commands("execution: questions")
        with self.context.session.db.write():
            ids = [
                commands.comment(jot_id, q.body(), kind="execution.question").id
                for q in questions
            ]
            return self.claims.release(
                jot_id,
                AGENT,
                Phase.NEEDS_INPUT,
                extra=extra | {"asked_in": asked_in.value, "questions": list(ids)},
            )

    def plan(self, jot_id: int) -> WorkState:
        """Investigate read-only and record a plan or questions."""
        state = self.claims.claim(jot_id, AGENT, lease=self.settings.lease_seconds)
        if state.phase != Phase.PLANNING:
            raise InvalidInputError(
                f"Jot {jot_id} is {state.phase.value}, not planning"
            )
        jot = self.context.queries.get(jot_id)
        repo = Workspace.check_repo(self.repo_for(jot))
        log = self.log_for(jot_id, Phase.PLANNING)
        self.claims.annotate(jot_id, AGENT, {"runner": True, "log": str(log.path)})
        self._beat = self.monotonic()
        final = self.stream(
            jot_id,
            RunRequest(
                prompt=self.prompt(jot, self.settings.plan_guidance, PLAN_REPLY),
                cwd=str(repo),
                permission=Permission.READ_ONLY,
                system=SYSTEM,
            ),
            PLAN_ACTION,
            log,
        )
        answer = Answer.parse(final.text)
        extra: Metadata = {"model": final.model}
        if answer.questions:
            return self.ask(jot_id, answer.questions, Phase.PLANNING, extra)
        parts = [part for part in (answer.summary, answer.plan) if part]
        body = "\n\n".join(parts) or "(empty plan)"
        commands = self.context.commands("execution: plan")
        with self.context.session.db.write():
            comment = commands.comment(jot_id, body, kind="execution.plan")
            current = self.context.queries.get(jot_id)
            extra["plan"] = {
                "comment": comment.id,
                "revision": self.revision(current, comment.id),
            }
            return self.claims.release(
                jot_id, AGENT, Phase.AWAITING_APPROVAL, extra=extra
            )

    def execute(self, jot_id: int, *, direct: bool = False) -> WorkState:
        """Do the work in the jot's worktree and hand it back for review."""
        state = self.claims.claim(
            jot_id, AGENT, lease=self.settings.lease_seconds, direct=direct
        )
        if state.phase != Phase.EXECUTING:
            raise InvalidInputError(
                f"Jot {jot_id} is {state.phase.value}, not executing"
            )
        jot = self.context.queries.get(jot_id)
        record = Claims.record_of(jot)
        workspace = self.workspace(jot)
        base = text_of(record.get("base"))
        started = workspace.ensure()
        base = base or started
        log = self.log_for(jot_id, Phase.EXECUTING)
        self.claims.annotate(
            jot_id,
            AGENT,
            {
                "runner": True,
                "log": str(log.path),
                "worktree": str(workspace.path),
                "branch": workspace.branch,
                "base": base,
            },
        )
        session = text_of(record.get("session")) or None
        if session and not self.harness().resumable(RUN_ACTION):
            session = None
        plan = self.approved_plan(jot)
        self._beat = self.monotonic()
        final = self.stream(
            jot_id,
            RunRequest(
                prompt=self.prompt(
                    jot, self.settings.execute_guidance, EXECUTE_REPLY, plan
                ),
                cwd=str(workspace.path),
                permission=self.permission(jot),
                system=SYSTEM,
                session_id=session,
            ),
            RUN_ACTION,
            log,
        )
        answer = Answer.parse(final.text)
        self.heartbeat(jot_id, force=True)
        workspace.commit_pending(f"jot #{jot_id}: {jot.title}")
        extra: Metadata = {"model": final.model, "session": final.session_id}
        if answer.questions:
            return self.ask(jot_id, answer.questions, Phase.EXECUTING, extra)
        return self.finish(
            jot_id, workspace=workspace, base=base, answer=answer, log=log, extra=extra
        )

    def approved_plan(self, jot: Jot) -> str:
        """The latest plan, for the execute prompt."""
        plans = [
            c
            for c in self.context.queries.comments(jot.id)
            if c.kind == "execution.plan"
        ]
        return f"## Approved plan\n\n{plans[-1].body}" if plans else ""

    def finish(  # noqa: PLR0913 - everything the result reports
        self,
        jot_id: int,
        *,
        workspace: Workspace,
        base: str,
        answer: Answer,
        log: RunLog,
        extra: Metadata,
    ) -> WorkState:
        """Record the result and hand the jot back for review."""
        commits = workspace.commits(base)
        stat = workspace.diffstat(base)
        outcome = answer.outcome
        lines = [
            f"**Outcome:** {outcome.replace('_', ' ')}",
            answer.summary or "(no summary)",
            f"**Verification:** {answer.verification or 'none reported'}",
            f"**Branch:** `{workspace.branch}` in `{workspace.path}`",
        ]
        if commits:
            lines.append("**Commits:**\n" + "\n".join(f"- {c}" for c in commits))
        if stat:
            lines.append(f"```\n{stat}\n```")
        if untracked := workspace.untracked():
            shown = ", ".join(f"`{name}`" for name in untracked[:SHOWN])
            extra_files = len(untracked) - SHOWN
            more = f" and {extra_files} more" if extra_files > 0 else ""
            lines.append(f"**Not committed (untracked):** {shown}{more}")
        lines.append(f"Log: `{log.path}`")
        target = Phase.NEEDS_INPUT if outcome == "cannot_proceed" else Phase.REVIEW
        commands = self.context.commands("execution: result")
        with self.context.session.db.write():
            commands.comment(jot_id, "\n\n".join(lines), kind="execution.result")
            return self.claims.release(
                jot_id,
                AGENT,
                target,
                extra=extra
                | {
                    "outcome": outcome,
                    "head": workspace.head(),
                    "commits": len(commits),
                    "asked_in": Phase.EXECUTING.value,
                },
            )

    def give_up(self, jot_id: int, phase: Phase, error: str) -> None:
        """After the last failed attempt: hand the jot to the owner with the error."""
        commands = self.context.commands("execution: failed")
        with self.context.session.db.write():
            try:
                self.claims.release(
                    jot_id,
                    AGENT,
                    Phase.NEEDS_INPUT,
                    extra={"asked_in": phase.value, "outcome": "failed"},
                )
            except ConflictError:
                return  # someone else holds it now (or it was cancelled)
            commands.comment(
                jot_id,
                f"The {phase.value} run failed: {error[:500]}\n\n"
                f"Retry with `jot work retry {jot_id}`.",
                kind="execution.note",
            )


def dumps(events: list[HarnessEvent]) -> str:
    """Readable text for a run log."""
    lines: list[str] = []
    for event in events:
        if event.kind in {EventKind.TEXT, EventKind.TOOL, EventKind.ERROR}:
            lines.append(f"[{event.kind.value}] {event.text}")
        elif event.kind == EventKind.RESULT:
            lines.append(f"[result{' error' if event.is_error else ''}] {event.text}")
        elif event.kind == EventKind.USAGE and event.usage is not None:
            usage = event.usage.model_dump(exclude_none=True)
            lines.append(f"[usage] {json.dumps(usage)}")
    return "\n".join(lines)
