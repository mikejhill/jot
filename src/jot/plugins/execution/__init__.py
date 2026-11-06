"""Execution: supervised agent work on jots.

External agents use claims (``jot work claim/heartbeat/release``): a lease in
``metadata.execution`` so two agents never hold the same jot. The built-in
runner (``jot work run``) plans read-only through the ``harness@1`` service,
waits for answers and approval, executes in a per-jot git worktree, and hands
the jot back for review. Phases are mirrored to labels and status categories
so lists and boards work without the plugin.
"""

from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, override

import typer
from pydantic import JsonValue

from jot.core.models import Metadata, Origin, WriteContext
from jot.exceptions import AppError, ConflictError, InvalidInputError, NotFoundError
from jot.plugins.api import JobStatus, Plugin
from jot.plugins.execution.claims import (
    CATEGORY,
    DEFAULT_LEASE,
    LABELS,
    PLUGIN,
    RELEASE_TO,
    RUNNING,
    WAITING,
    Claim,
    Claims,
    Phase,
    WorkState,
)
from jot.plugins.execution.contract import CapError
from jot.plugins.execution.runner import (
    AGENT,
    ExecutionSettings,
    RunLog,
    Runner,
    dumps,
)
from jot.plugins.execution.workspace import Workspace
from jot.plugins.harness import SERVICE
from jot.plugins.harness.api import JSON, HarnessEvent, integer, obj, text_of

if TYPE_CHECKING:
    from jot.app.commands import Commands
    from jot.plugins.api import Job, PluginCli
    from jot.plugins.context import PluginContext
    from jot.plugins.host import PluginHost

__all__ = [
    "AGENT",
    "CATEGORY",
    "DEFAULT_LEASE",
    "LABELS",
    "PLUGIN",
    "RELEASE_TO",
    "RUNNING",
    "WAITING",
    "Claim",
    "Claims",
    "Control",
    "ExecutionPlugin",
    "ExecutionSettings",
    "Phase",
    "WorkState",
]
PLAN = "plan"
EXECUTE = "execute"
LIVE = [JobStatus.QUEUED, JobStatus.RUNNING]


class ExecutionPlugin(Plugin):
    """Claims for agents, and a runner that plans and executes jots."""

    id = PLUGIN
    name = "Execution"
    version = "2"
    description = "Supervised agent work: claims, plans, approval, worktrees."
    listens = False
    command = "work"
    settings_model = ExecutionSettings

    @staticmethod
    def options(context: PluginContext) -> ExecutionSettings:
        """Typed settings."""
        settings = context.settings
        if not isinstance(settings, ExecutionSettings):  # pragma: no cover - host bug
            raise TypeError("execution settings have the wrong type")
        return settings

    @override
    def run(self, job: Job, context: PluginContext) -> JsonValue:
        """Run one plan or execute job."""
        if job.jot_id is None or job.kind not in {PLAN, EXECUTE}:
            return {"skipped": f"unknown job {job.kind}"}
        runner = Runner(context, self.options(context))
        return Control.attempt(
            runner,
            job.jot_id,
            job.kind,
            direct=job.payload.get("direct") is True,
            last=job.attempts >= job.max_attempts,
        )

    @classmethod
    @override
    def commands(cls, cli: PluginCli) -> typer.Typer:
        """``jot work ...``."""
        return WorkCommands(cli).app()

    @override
    def action(
        self, name: str, body: Metadata, host: PluginHost, context: WriteContext
    ) -> JsonValue:
        """Runner actions for the web UI and API; runs are always queued."""
        return Actions(Control(host, context.actor, context.origin)).call(name, body)

    @override
    def ui_module(self) -> str | None:
        """The drawer panel and phase badges."""
        return files(__package__ or __name__).joinpath("ui.js").read_text("utf-8")


class Control:
    """Owner and runner actions shared by the CLI and (later) the HTTP API."""

    def __init__(
        self, host: PluginHost, actor: str, origin: Origin = Origin.CLI
    ) -> None:
        if not host.status(PLUGIN).enabled:
            raise InvalidInputError(
                "Execution is off; run `jot plugin enable execution`"
            )
        self.host = host
        self.actor = actor
        self.origin = origin
        self.claims = Claims(host.session, origin=origin)

    @property
    def settings(self) -> ExecutionSettings:
        """Current settings."""
        settings = self.host.settings(PLUGIN)
        if not isinstance(settings, ExecutionSettings):  # pragma: no cover - host bug
            raise TypeError("execution settings have the wrong type")
        return settings

    def commands(self, reason: str) -> Commands:
        """Commands attributed to the owner."""
        return self.host.session.commands(
            WriteContext(actor=self.actor, origin=self.origin, reason=reason)
        )

    @staticmethod
    def attempt(
        runner: Runner, jot_id: int, kind: str, *, direct: bool, last: bool
    ) -> JsonValue:
        """Run a phase; retry on harness or git errors, give up on the last try."""
        phase = Phase.PLANNING if kind == PLAN else Phase.EXECUTING
        try:
            state = (
                runner.plan(jot_id)
                if kind == PLAN
                else runner.execute(jot_id, direct=direct)
            )
            while kind == EXECUTE and runner.needs_repair(state):
                cycle = runner.repair(jot_id)
                if runner.context.job is not None:
                    runner.context.enqueue(
                        EXECUTE,
                        {},
                        jot_id=jot_id,
                        dedupe_key=f"jot-{jot_id}-repair-{cycle}",
                    )
                    return {"repair": cycle}
                state = runner.execute(jot_id)
        except ConflictError as err:
            return {"stopped": str(err)}  # lease lost or cancelled: write nothing
        except CapError as err:
            runner.give_up(jot_id, phase, str(err), outcome="capped")
            return {"capped": str(err)}
        except InvalidInputError as err:
            runner.give_up(jot_id, phase, str(err))
            return {"failed": str(err)}
        except AppError as err:
            if not last and not runner.repeated(jot_id, str(err)):
                raise
            runner.give_up(jot_id, phase, str(err))
            return {"failed": str(err)}
        return {"phase": state.phase.value, "version": state.version}

    def queue(
        self, jot_id: int, kind: str, *, direct: bool = False, now: bool
    ) -> WorkState:
        """Queue a plan or execute job, or (``now``) run it here."""
        context = self.host.context_for(PLUGIN)
        if now:
            runner = Runner(context, self.settings, name="cli")
            Control.attempt(runner, jot_id, kind, direct=direct, last=True)
        else:
            context.enqueue(
                kind, {"direct": direct}, jot_id=jot_id, dedupe_key=f"jot-{jot_id}"
            )
        return self.claims.state(jot_id)

    def check_runnable(self, jot_id: int) -> None:
        """Refuse when the harness or a repository is missing."""
        if not self.host.services(SERVICE):
            raise InvalidInputError(
                "The runner needs harness@1; run `jot plugin enable harness` "
                "and a backend such as `jot plugin enable harness-claude`"
            )
        jot = self.host.session.queries.get(jot_id)
        Runner(self.host.context_for(PLUGIN), self.settings).repo_for(jot)

    def start(
        self, jot_id: int, *, direct: bool = False, now: bool = False
    ) -> WorkState:
        """Plan (or with ``direct``, execute) a ready jot."""
        self.check_runnable(jot_id)
        jot = self.host.session.queries.get(jot_id)
        if not self.claims.ready(jot):
            state = self.claims.state(jot_id)
            held = f" and held by {state.claim.agent}" if state.claim else ""
            raise InvalidInputError(
                f"Jot {jot_id} is {Claims.words(state.phase)}{held}; not ready to run"
            )
        settings = self.settings
        direct = (
            direct
            or (jot.project is not None and jot.project in settings.direct_projects)
            or bool(settings.direct_label and settings.direct_label in jot.labels)
        )
        phase = Claims.phase_of(jot)
        executing = direct or phase in {Phase.APPROVED, Phase.EXECUTING}
        return self.queue(
            jot_id, EXECUTE if executing else PLAN, direct=direct, now=now
        )

    def approve(
        self, jot_id: int, *, run: bool | None = None, now: bool = False
    ) -> WorkState:
        """Approve the plan; queue execution when the runner made it."""
        jot = self.host.session.queries.get(jot_id)
        record = Claims.record_of(jot)
        plan = obj(record.get("plan"))
        comment = integer(plan.get("comment"))
        # Approving binds to the plan and the jot as they are now.
        revision = Runner.revision(jot, comment) if comment is not None else ""
        text = f"Approved plan {revision}." if revision else "Approved."
        extra: Metadata = {"approved": revision or None, "repairs": 0}
        if revision:
            extra["plan"] = plan | {"revision": revision}
        state = self.claims.approve(jot_id, self.actor, text, extra)
        if run if run is not None else record.get("runner") is True:
            self.check_runnable(jot_id)
            return self.queue(jot_id, EXECUTE, now=now)
        return state

    def answer(
        self, jot_id: int, question: int, text: str, *, now: bool = False
    ) -> WorkState:
        """Answer one question; resume once every open question has an answer."""
        comments = self.host.session.queries.comments(jot_id)
        asked = next((c for c in comments if c.id == question), None)
        if asked is None or asked.kind != "execution.question":
            raise InvalidInputError(
                f"Comment {question} is not a question on jot {jot_id}"
            )
        self.commands("answer").comment(
            jot_id, text, kind="execution.answer", reply_to=question
        )
        jot = self.host.session.queries.get(jot_id)
        if Claims.phase_of(jot) != Phase.NEEDS_INPUT:
            return self.claims.state(jot_id)
        open_ids = record_ids(Claims.record_of(jot).get("questions"))
        answered = {c.reply_to for c in comments} | {question}
        if any(qid not in answered for qid in open_ids):
            return self.claims.state(jot_id)
        return self.resume(jot_id, now=now)

    def resume(self, jot_id: int, *, now: bool = False) -> WorkState:
        """Put a jot waiting for input back into the phase that asked."""
        record = Claims.record_of(self.host.session.queries.get(jot_id))
        executing = text_of(record.get("asked_in")) == Phase.EXECUTING.value
        target = Phase.APPROVED if executing else Phase.IDLE
        if record.get("runner") is not True:
            return self.claims.reopen(jot_id, self.actor, target)
        self.check_runnable(jot_id)
        self.claims.reopen(jot_id, self.actor, target)
        return self.queue(jot_id, EXECUTE if executing else PLAN, now=now)

    def send_back(self, jot_id: int, note: str, *, now: bool = False) -> WorkState:
        """Re-plan (from awaiting approval) or rework (from review) with a note."""
        phase = self.claims.state(jot_id).phase
        if phase not in {Phase.AWAITING_APPROVAL, Phase.REVIEW}:
            raise InvalidInputError(
                f"Jot {jot_id} is {Claims.words(phase)}; send back a plan or a result"
            )
        self.check_runnable(jot_id)
        self.commands("send back").comment(jot_id, note, kind="execution.feedback")
        if phase == Phase.AWAITING_APPROVAL:
            self.claims.reopen(jot_id, self.actor, Phase.IDLE)
            return self.queue(jot_id, PLAN, now=now)
        self.claims.reopen(jot_id, self.actor, Phase.APPROVED, {"repairs": 0})
        return self.queue(jot_id, EXECUTE, now=now)

    def retry(self, jot_id: int, *, now: bool = False) -> WorkState:
        """Run again after a failure (or start a ready jot)."""
        if self.claims.state(jot_id).phase == Phase.NEEDS_INPUT:
            return self.resume(jot_id, now=now)
        return self.start(jot_id, now=now)

    def cancel(self, jot_id: int) -> WorkState:
        """Cancel queued and running jobs and put the jot back before its run."""
        jobs = [
            job
            for job in self.host.jobs(plugin_id=PLUGIN, statuses=LIVE)
            if job.jot_id == jot_id
        ]
        state = self.claims.state(jot_id)
        if not jobs and state.phase not in RUNNING:
            raise InvalidInputError(f"Jot {jot_id} has no run to cancel")
        for job in jobs:
            self.host.cancel(job.id)
        if state.phase in RUNNING:
            target = Phase.APPROVED if state.phase == Phase.EXECUTING else Phase.IDLE
            return self.claims.reset(jot_id, self.actor, target)
        return state

    def log(self, jot_id: int) -> list[HarnessEvent]:
        """The latest run's events."""
        jot = self.host.session.queries.get(jot_id)
        path = text_of(Claims.record_of(jot).get("log"))
        if not path:
            raise InvalidInputError(f"Jot {jot_id} has no run log")
        return RunLog(Path(path)).read()

    def clean(self, jot_id: int, *, force: bool = False) -> Workspace:
        """Remove a jot's worktree (the branch stays)."""
        jot = self.host.session.queries.get(jot_id)
        if Claims.phase_of(jot) in RUNNING and self.claims.live(Claims.claim_of(jot)):
            raise InvalidInputError(f"Jot {jot_id} is running; cancel it first")
        workspace = Runner(self.host.context_for(PLUGIN), self.settings).workspace(jot)
        workspace.remove(force=force)
        return workspace

    def repos(
        self,
        updates: dict[str, str | None],
        *,
        default: str | None = None,
        from_import: bool = False,
    ) -> ExecutionSettings:
        """Change the project -> repository mapping."""
        settings = self.settings
        repos = dict(settings.repos)
        if from_import:
            found = imported_repos(self.host)
            repos |= {k: v for k, v in found.items() if k not in repos}
        projects = self.host.session.queries.projects(include_archived=True)
        known = {p.slug for p in projects}
        for slug, path in updates.items():
            if path is None:
                repos.pop(slug, None)
                continue
            if slug not in known:
                raise InvalidInputError(f"No project {slug}")
            repos[slug] = str(Workspace.check_repo(Path(path).expanduser()))
        values = settings.model_dump(mode="json") | {"repos": repos}
        if default is not None:
            values["default_repo"] = (
                str(Workspace.check_repo(Path(default).expanduser())) if default else ""
            )
        self.host.configure(
            PLUGIN, values, WriteContext(actor=self.actor, origin=self.origin)
        )
        return self.settings


class Actions:
    """``POST /api/plugins/execution/actions/<name>`` with ``{"jot": id, ...}``."""

    NAMES = (
        "state",
        "run",
        "approve",
        "answer",
        "send_back",
        "retry",
        "cancel",
        "log",
    )
    TAIL = 200

    def __init__(self, control: Control) -> None:
        self.control = control

    @staticmethod
    def jot_id(body: Metadata) -> int:
        """The jot the action is about."""
        jot_id = integer(body.get("jot"))
        if jot_id is None:
            raise InvalidInputError('Give the jot as {"jot": <id>}')
        return jot_id

    @staticmethod
    def text(body: Metadata, key: str) -> str:
        """A required text field."""
        value = text_of(body.get(key)).strip()
        if not value:
            raise InvalidInputError(f"{key} is required")
        return value

    def call(self, name: str, body: Metadata) -> JsonValue:
        """Dispatch one action; return the jot's state (or log events)."""
        if name not in self.NAMES:
            raise NotFoundError(f"Execution has no action {name!r}")
        jot_id = self.jot_id(body)
        control = self.control
        if name == "log":
            tail = integer(body.get("tail")) or self.TAIL
            events = control.log(jot_id)[-tail:]
            return [
                event.model_dump(mode="json", exclude_defaults=True) for event in events
            ]
        if name == "run":
            state = control.start(jot_id, direct=body.get("direct") is True)
        elif name == "approve":
            state = control.approve(jot_id)
        elif name == "answer":
            question = integer(body.get("question"))
            if question is None:
                raise InvalidInputError("question (a comment id) is required")
            state = control.answer(jot_id, question, self.text(body, "text"))
        elif name == "send_back":
            state = control.send_back(jot_id, self.text(body, "note"))
        elif name == "retry":
            state = control.retry(jot_id)
        elif name == "cancel":
            state = control.cancel(jot_id)
        else:
            state = control.claims.state(jot_id)
        return JSON.validate_python(state.model_dump(mode="json"))


def record_ids(value: JsonValue) -> list[int]:
    """Integer ids from a JSON list."""
    if not isinstance(value, list):
        return []
    return [i for i in (integer(v) for v in value) if i is not None]


def imported_repos(host: PluginHost) -> dict[str, str]:
    """Project -> repo path from v0.1 import events."""
    found: dict[str, str] = {}
    after = 0
    while events := host.session.queries.events(after, 500):
        for event in events:
            if event.kind != "project.imported":
                continue
            project = obj(event.data.get("project"))
            slug, path = text_of(project.get("slug")), text_of(project.get("repo_path"))
            if slug and path:
                found[slug] = path
        after = events[-1].id
    return found


Json = Annotated[bool, typer.Option("--json", help="Emit JSON")]
Agent = Annotated[str, typer.Option("--agent", "-a", help="Your stable agent name")]
JotId = Annotated[int, typer.Argument(help="Jot id")]
Now = Annotated[
    bool, typer.Option("--now", help="Run here and wait instead of queueing")
]


class WorkCommands:
    """``jot work``: the agent queue and the runner."""

    def __init__(self, cli: PluginCli) -> None:
        self.cli = cli

    def app(self) -> typer.Typer:
        """Build the command group."""
        app = typer.Typer(
            no_args_is_help=True, help="Agent work on jots: claims and the runner"
        )
        for name, command in (
            ("next", self.next),
            ("claim", self.claim),
            ("heartbeat", self.heartbeat),
            ("release", self.release),
            ("state", self.state),
            ("run", self.run),
            ("approve", self.approve),
            ("answer", self.answer),
            ("send-back", self.send_back),
            ("retry", self.retry),
            ("cancel", self.cancel),
            ("log", self.log),
            ("repos", self.repos),
            ("clean", self.clean),
        ):
            app.command(name)(command)
        return app

    @staticmethod
    def actor() -> str:
        """The owner's actor name."""
        return os.environ.get("JOT_ACTOR") or "user"

    @staticmethod
    def claims(host: PluginHost) -> tuple[Claims, int]:
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

    def emit(self, state: WorkState) -> None:
        """Print one state."""
        self.cli.emit(state, self.line(state))

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
        self.emit(state)

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
        self.emit(state)

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
        self.emit(state)

    def state(self, jot_id: JotId, *, json: Json = False) -> None:
        """Show a jot's phase and claim."""
        with self.cli.host(json=json) as host:
            state = self.claims(host)[0].state(jot_id)
        self.emit(state)

    def run(
        self,
        jot_id: JotId,
        *,
        direct: Annotated[
            bool, typer.Option("--direct", help="Skip planning and approval")
        ] = False,
        now: Now = False,
        json: Json = False,
    ) -> None:
        """Have the runner plan (or execute) a jot through the harness."""
        with self.cli.host(json=json) as host:
            state = Control(host, self.actor()).start(jot_id, direct=direct, now=now)
        self.emit(state)

    def approve(
        self,
        jot_id: JotId,
        *,
        run: Annotated[
            bool | None,
            typer.Option(
                "--run/--no-run",
                help="Queue execution (default: when the runner made the plan)",
            ),
        ] = None,
        actor: Annotated[str | None, typer.Option("--actor")] = None,
        now: Now = False,
        json: Json = False,
    ) -> None:
        """Approve a plan so it may be executed."""
        with self.cli.host(json=json) as host:
            control = Control(host, actor or self.actor())
            state = control.approve(jot_id, run=run, now=now)
        self.emit(state)

    def answer(
        self,
        jot_id: JotId,
        question: Annotated[int, typer.Argument(help="The question's comment id")],
        text: Annotated[str, typer.Argument(help="Your answer")],
        *,
        now: Now = False,
        json: Json = False,
    ) -> None:
        """Answer an agent's question; the run resumes when all are answered."""
        with self.cli.host(json=json) as host:
            state = Control(host, self.actor()).answer(jot_id, question, text, now=now)
        self.emit(state)

    def send_back(
        self,
        jot_id: JotId,
        note: Annotated[str, typer.Argument(help="What to change")],
        *,
        now: Now = False,
        json: Json = False,
    ) -> None:
        """Re-plan a plan, or rework a result, with your note."""
        with self.cli.host(json=json) as host:
            state = Control(host, self.actor()).send_back(jot_id, note, now=now)
        self.emit(state)

    def retry(self, jot_id: JotId, *, now: Now = False, json: Json = False) -> None:
        """Run again after a failure or questions."""
        with self.cli.host(json=json) as host:
            state = Control(host, self.actor()).retry(jot_id, now=now)
        self.emit(state)

    def cancel(self, jot_id: JotId, *, json: Json = False) -> None:
        """Stop a queued or running run."""
        with self.cli.host(json=json) as host:
            state = Control(host, self.actor()).cancel(jot_id)
        self.emit(state)

    def log(self, jot_id: JotId, *, json: Json = False) -> None:
        """Show the latest run's log."""
        with self.cli.host(json=json) as host:
            events = Control(host, self.actor()).log(jot_id)
        self.cli.emit(events, dumps(events) + "\n")

    def repos(
        self,
        *,
        set_: Annotated[
            list[str] | None,
            typer.Option("--set", help="project=path (repeatable)"),
        ] = None,
        unset: Annotated[
            list[str] | None, typer.Option("--unset", help="Project (repeatable)")
        ] = None,
        default: Annotated[
            str | None,
            typer.Option("--default", help="Repository for other jots ('' clears)"),
        ] = None,
        from_import: Annotated[
            bool,
            typer.Option("--from-import", help="Fill from v0.1 project repo paths"),
        ] = False,
        json: Json = False,
    ) -> None:
        """Show or change which repository each project's jots run in."""
        updates: dict[str, str | None] = {}
        for slug in unset or []:
            updates[slug] = None
        with self.cli.host(json=json) as host:
            for pair in set_ or []:
                slug, sep, path = pair.partition("=")
                if not sep or not slug or not path:
                    raise InvalidInputError(f"Use --set project=path, not {pair!r}")
                updates[slug] = path
            control = Control(host, self.actor())
            changed = bool(updates) or default is not None or from_import
            settings = (
                control.repos(updates, default=default, from_import=from_import)
                if changed
                else control.settings
            )
        rows = [f"{slug}  {path}\n" for slug, path in sorted(settings.repos.items())]
        if settings.default_repo:
            rows.append(f"(default)  {settings.default_repo}\n")
        self.cli.emit(settings, "".join(rows) or "No repositories mapped\n")

    def clean(
        self,
        jot_id: JotId,
        *,
        force: Annotated[
            bool, typer.Option("--force", help="Discard uncommitted changes")
        ] = False,
        json: Json = False,
    ) -> None:
        """Remove a jot's worktree (its branch stays)."""
        with self.cli.host(json=json) as host:
            workspace = Control(host, self.actor()).clean(jot_id, force=force)
            state = Claims(host.session).state(jot_id)
        self.cli.emit(state, f"Removed {workspace.path}\n")
