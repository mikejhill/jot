"""Agent-facing CLI commands: enrichment, drawdown runs, cleanup, skills."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from jot.agents.registry import BackendRegistry
from jot.core.models import Flow, Run
from jot.db.repository import RunRepository, TaskRepository
from jot.exceptions import WorkflowError
from jot.services.bus import EventBus, Topic
from jot.services.cleanup import CleanupService
from jot.services.enrich import EnrichService
from jot.services.runner import RunService
from jot.services.settings import SettingsStore

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from jot.cli import CommandLine

BackendOption = Annotated[
    str | None,
    typer.Option("--harness", "--backend", help="Configured harness id or auto"),
]
JsonFlag = Annotated[bool, typer.Option("--json")]
ModelOption = Annotated[
    str | None,
    typer.Option("--model", help="Model for this action (e.g. opus, gpt-6-astra)"),
]
LEGACY_SKILLS = ("jot-capture", "jot-drawdown", "jot-cleanup", "jot-groom")
SKILL_TARGETS = {
    "claude": Path(".claude") / "skills",
    "codex": Path(".codex") / "skills",
    "copilot": Path(".copilot") / "skills",
}


class AgentCommands:
    """Register agent-backed commands onto the main Typer app."""

    def __init__(self, cli: CommandLine) -> None:
        self.cli = cli

    def register(self, app: typer.Typer) -> None:
        """Attach every agent command to ``app``."""
        app.command("enrich")(self.enrich)
        app.command("plan")(self.plan)
        app.command("run")(self.run)
        app.command("approve")(self.approve)
        app.command("answer")(self.answer)
        app.command("send-back")(self.send_back)
        app.command("runs")(self.runs)
        app.command("log")(self.log)
        app.command("install-skills")(self.install_skills)
        cleanup = typer.Typer(no_args_is_help=True, help="Find and prune stale tasks")
        cleanup.command("scan")(self.cleanup_scan)
        cleanup.command("show")(self.cleanup_show)
        cleanup.command("apply")(self.cleanup_apply)
        app.add_typer(cleanup, name="cleanup")
        harness = typer.Typer(no_args_is_help=True)
        harness.command("ls")(self.harness_ls)
        harness.command("show")(self.harness_show)
        harness.command("discover")(self.harness_discover)
        app.add_typer(harness, name="harness")
        pins = typer.Typer(no_args_is_help=True)
        pins.command("ls")(self.pins_ls)
        app.add_typer(pins, name="pins")

    def harness_ls(self, *, json: JsonFlag = False) -> None:
        """List all configured harnesses and their kinds."""
        with self.cli.session(json=json) as (_, _, home):
            values = SettingsStore.effective(home.initialize())["harnesses"]
            rows = values.items() if isinstance(values, dict) else []
            lines = ["ID\tKIND\tENABLED\tLABEL"] + [
                f"{key}\t{item.get('kind')}\t{item.get('enabled')}\t{item.get('label')}"
                for key, item in rows
                if isinstance(item, dict)
            ]
            self.cli.emit(values, "\n".join(lines) + "\n", json=json)

    def harness_show(self, harness_id: str, *, json: JsonFlag = False) -> None:
        """Show the complete configuration of a named harness."""
        with self.cli.session(json=json) as (_, _, home):
            config = home.initialize()
            harness = config.effective_harnesses().get(harness_id)
            if harness is None:
                raise WorkflowError(f"Unknown harness {harness_id!r}")
            self.cli.emit(
                harness.model_dump(),
                harness.model_dump_json(indent=2) + "\n",
                json=json,
            )

    def harness_discover(self, harness_id: str, *, json: JsonFlag = False) -> None:
        """Run one cheap discovery session for a configured harness."""
        with self.cli.session(json=json) as (_, _, home):
            result = asyncio.run(
                BackendRegistry.discover(home.initialize(), harness_id)
            )
            self.cli.emit(result, str(result) + "\n", json=json)

    def pins_ls(self, *, json: JsonFlag = False) -> None:
        """List global shortcuts and their optional action filters."""
        with self.cli.session(json=json) as (_, _, home):
            pins = [pin.model_dump(exclude_none=True) for pin in home.initialize().pins]
            self.cli.emit(pins, str(pins) + "\n", json=json)

    def enrich(
        self,
        task_ids: Annotated[list[int] | None, typer.Argument()] = None,
        *,
        backend: BackendOption = None,
        model: ModelOption = None,
        json: JsonFlag = False,
    ) -> None:
        """Enrich given tasks, or every task still waiting for enrichment."""
        with self.cli.session(json=json) as (db, _, home):
            service = EnrichService(db, home, home.initialize())
            if task_ids:
                tasks = [
                    asyncio.run(service.enrich(i, backend, model)) for i in task_ids
                ]
            else:
                tasks = asyncio.run(
                    service.enrich_pending(limit=1000, backend=backend, model=model)
                )
            lines = [
                f"{t.id}\t{t.status}\t{t.criticality}\t{t.title}"
                f"\t[{', '.join(t.labels)}]"
                for t in tasks
            ]
            self.cli.emit(
                self.cli.records(tasks),
                "\n".join(lines) + "\n" if lines else "Nothing to enrich\n",
                json=json,
            )

    def plan(  # noqa: PLR0913 - Typer requires one parameter per CLI option
        self,
        task_id: Annotated[int | None, typer.Argument()] = None,
        *,
        next_: Annotated[bool, typer.Option("--next")] = False,
        project: Annotated[str | None, typer.Option("--project")] = None,
        backend: BackendOption = None,
        model: ModelOption = None,
        json: JsonFlag = False,
    ) -> None:
        """Run a read-only planning pass; the task then awaits your approval."""
        self._start(
            task_id,
            Flow.PLANNED,
            next_=next_,
            project=project,
            backend=backend,
            model=model,
            json=json,
        )

    def run(  # noqa: PLR0913 - Typer requires one parameter per CLI option
        self,
        task_id: Annotated[int | None, typer.Argument()] = None,
        *,
        direct: Annotated[
            bool, typer.Option("--direct", help="Skip planning and approval")
        ] = False,
        next_: Annotated[bool, typer.Option("--next")] = False,
        project: Annotated[str | None, typer.Option("--project")] = None,
        backend: BackendOption = None,
        model: ModelOption = None,
        json: JsonFlag = False,
    ) -> None:
        """Draw down a ready task using its flow (or --direct to execute now)."""
        self._start(
            task_id,
            Flow.DIRECT if direct else None,
            next_=next_,
            project=project,
            backend=backend,
            model=model,
            json=json,
        )

    def approve(
        self,
        task_id: int,
        *,
        note: Annotated[str | None, typer.Option("--note")] = None,
        backend: BackendOption = None,
        model: ModelOption = None,
        json: JsonFlag = False,
    ) -> None:
        """Approve a plan (answers go in --note) and execute it now."""
        self._drive(
            lambda service: service.approve(task_id, note, backend, model), json=json
        )

    def answer(
        self,
        task_id: int,
        *,
        answer: Annotated[
            list[str] | None,
            typer.Option(
                "--answer",
                "-a",
                help="<question-event-id>=<text>; repeat per question, omit to defer",
            ),
        ] = None,
        backend: BackendOption = None,
        model: ModelOption = None,
        json: JsonFlag = False,
    ) -> None:
        """Answer a needs_input task's questions and resume its run."""
        answers: dict[int, str] = {}
        for item in answer or []:
            key, sep, text = item.partition("=")
            if not sep or not key.strip().isdigit():
                raise WorkflowError(f"--answer must be <event-id>=<text>, got {item!r}")
            answers[int(key)] = text
        self._drive(
            lambda service: service.respond(task_id, answers, backend, model),
            json=json,
        )

    def send_back(self, task_id: int, comment: str, *, json: JsonFlag = False) -> None:
        """Return a planned or reviewed task to ready with feedback."""
        with self.cli.session(json=json) as (db, _, home):
            service = RunService(db, home, home.initialize(), EventBus())
            task = asyncio.run(service.send_back(task_id, comment))
            self.cli.emit(
                task.model_dump(mode="json"), f"{task.id}\t{task.status}\n", json=json
            )

    def runs(
        self,
        task_id: Annotated[int | None, typer.Argument()] = None,
        *,
        json: JsonFlag = False,
    ) -> None:
        """List agent runs, optionally for one task."""
        with self.cli.session(json=json) as (db, _, _home):
            runs = [
                r
                for r in RunRepository(db).list()
                if task_id is None or r.task_id == task_id
            ]
            lines = [
                f"{r.id}\ttask {r.task_id}\t{r.phase}"
                f"\t{r.backend}/{r.model or 'default'}\t{r.status}"
                f"\t{r.branch or ''}"
                for r in runs
            ]
            self.cli.emit(
                [r.model_dump(mode="json") for r in runs],
                "\n".join(lines) + "\n" if lines else "No runs\n",
                json=json,
            )

    def log(self, run_id: int, *, json: JsonFlag = False) -> None:
        """Print the stored agent log of a run."""
        with self.cli.session(json=json) as (db, _, home):
            lines = RunService(db, home, home.initialize(), EventBus()).log(run_id)
            text = "".join(
                f"[{line.get('kind')}] {line.get('text')}\n" for line in lines
            )
            self.cli.emit(lines, text or "No log\n", json=json)

    def _start(  # noqa: PLR0913 - mirrors the CLI options
        self,
        task_id: int | None,
        flow: Flow | None,
        *,
        next_: bool,
        project: str | None,
        backend: str | None,
        model: str | None,
        json: bool,
    ) -> None:
        """Resolve the task (explicit id or --next) and drive its run."""

        async def launch(service: RunService) -> Run:
            chosen = task_id
            if chosen is None:
                if not next_:
                    raise WorkflowError("Give a task id or --next")
                ranked = service.workflow.next(project=project)
                if not ranked:
                    raise WorkflowError("No ready tasks")
                chosen = ranked[0].id
            return await service.start(chosen, flow=flow, backend=backend, model=model)

        self._drive(launch, json=json)

    def _drive(
        self, launch: Callable[[RunService], Awaitable[Run]], *, json: bool
    ) -> None:
        """Launch a run in-process, stream its log to stderr, and report the result."""
        with self.cli.session(json=json) as (db, _, home):
            config = home.initialize()

            async def main() -> Run:
                bus = EventBus()
                service = RunService(db, home, config, bus)
                printer = asyncio.create_task(self._print(bus))
                await asyncio.sleep(0)
                try:
                    run = await launch(service)
                    return await service.wait(run.id)
                finally:
                    printer.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await printer

            run = asyncio.run(main())
            task = TaskRepository(db).get(run.task_id)
            text = (
                f"Run {run.id} {run.status}: task {task.id} is now {task.status}\n"
                f"{run.summary or ''}\n"
            )
            self.cli.emit(run.model_dump(mode="json"), text, json=json)

    async def _print(self, bus: EventBus) -> None:
        """Echo live run log lines to stderr."""
        async for message in bus.subscribe():
            if message.topic is Topic.RUN_LOG:
                kind = message.payload.get("kind")
                text = str(message.payload.get("text", ""))
                sys.stderr.write(f"[{kind}] {text[:500]}\n")

    def cleanup_scan(
        self,
        *,
        agent: Annotated[
            bool, typer.Option("--agent", help="Add an LLM review pass")
        ] = False,
        backend: BackendOption = None,
        model: ModelOption = None,
        json: JsonFlag = False,
    ) -> None:
        """Create a cleanup proposal; nothing changes until you apply items."""
        with self.cli.session(json=json) as (db, _, home):
            service = CleanupService(db, home, home.initialize())
            proposal_id = asyncio.run(
                service.scan(use_agent=agent, backend=backend, model=model)
            )
            self._show_proposal(service, proposal_id, json=json)

    def cleanup_show(
        self,
        proposal_id: Annotated[int | None, typer.Argument()] = None,
        *,
        json: JsonFlag = False,
    ) -> None:
        """Show a cleanup proposal (default: the latest)."""
        with self.cli.session(json=json) as (db, _, home):
            service = CleanupService(db, home, home.initialize())
            chosen = proposal_id or service.latest()
            if chosen is None:
                raise WorkflowError("No cleanup proposals yet; run `jot cleanup scan`")
            self._show_proposal(service, chosen, json=json)

    def cleanup_apply(
        self,
        proposal_id: int,
        *,
        approve: Annotated[
            str, typer.Option("--approve", help="Item indexes, e.g. 0,2,5 or all")
        ],
        json: JsonFlag = False,
    ) -> None:
        """Apply only the approved items of a proposal."""
        with self.cli.session(json=json) as (db, _, home):
            service = CleanupService(db, home, home.initialize())
            if approve.strip().lower() == "all":
                raw = service.proposal(proposal_id)["items"]
                indexes = list(range(len(raw))) if isinstance(raw, list) else []
            else:
                try:
                    indexes = [int(i) for i in approve.split(",") if i.strip()]
                except ValueError as err:
                    raise WorkflowError("--approve takes indexes like 0,2,5") from err
            affected = service.apply(proposal_id, indexes)
            self.cli.emit(
                {"affected": affected},
                f"Updated {len(affected)} task(s): {affected}\n",
                json=json,
            )

    def _show_proposal(
        self, service: CleanupService, proposal_id: int, *, json: bool
    ) -> None:
        """Print a proposal as a numbered list."""
        proposal = service.proposal(proposal_id)
        raw = proposal["items"]
        items = [i for i in raw if isinstance(i, dict)] if isinstance(raw, list) else []
        lines = [f"Proposal {proposal_id} ({proposal['status']}), {len(items)} item(s)"]
        lines += [
            f"  [{i.get('index')}] #{i.get('task_id')} {i.get('action')}: "
            f"{i.get('title')} - {i.get('reason')}"
            for i in items
        ]
        if items:
            lines.append(
                f"Apply with: jot cleanup apply {proposal_id} --approve 0,1,..."
            )
        self.cli.emit(proposal, "\n".join(lines) + "\n", json=json)

    def install_skills(
        self,
        *,
        target: Annotated[
            list[str] | None,
            typer.Option("--target", help="claude, codex, copilot (repeatable)"),
        ] = None,
        dest: Annotated[
            Path | None,
            typer.Option("--dest", help="Install into <dest>/<skill> instead"),
        ] = None,
        json: JsonFlag = False,
    ) -> None:
        """Install the `jot` skill (SKILL.md + references) into agent skill folders.

        Also removes the per-workflow jot-* skills installed by older versions.
        """
        targets = (
            [dest]
            if dest
            else [
                Path.home() / SKILL_TARGETS[name]
                for name in (target or ["claude"])
                if name in SKILL_TARGETS
            ]
        )
        if not targets:
            self.cli.fail(f"--target must be one of {', '.join(SKILL_TARGETS)}")
        installed: list[str] = []
        with resources.as_file(resources.files("jot") / "skills" / "jot") as skill:
            for root in targets:
                for legacy in LEGACY_SKILLS:
                    shutil.rmtree(root / legacy, ignore_errors=True)
                folder = root / "jot"
                shutil.copytree(skill, folder, dirs_exist_ok=True)
                installed.append(str(folder))
        self.cli.emit(
            installed, "".join(f"Installed {p}\n" for p in installed), json=json
        )
