"""Typer commands for local capture, queries, workflow, and snapshots."""

from __future__ import annotations

import io
import logging
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated

import typer
from pydantic import ValidationError

from jot.cli_agents import AgentCommands
from jot.config import JotHome
from jot.core.models import (
    Criticality,
    EventKind,
    Flow,
    Project,
    Status,
    Task,
    TaskEvent,
    TaskType,
)
from jot.core.workflow import Workflow
from jot.db.connection import Database
from jot.db.query import TaskQuery
from jot.db.repository import ProjectRepository, TaskRepository
from jot.exceptions import AppError, WorkflowError
from jot.services.archive import ArchiveService, Output

JsonOption = Annotated[bool, typer.Option("--json", help="Emit JSON")]
ProjectOption = Annotated[str | None, typer.Option("--project")]
LabelOption = Annotated[list[str] | None, typer.Option("--label")]
CritOption = Annotated[Criticality | None, typer.Option("--crit")]
TypeOption = Annotated[TaskType | None, typer.Option("--type")]


NOTE_KINDS = frozenset({"comment", "plan", "question", "answer", "result"})


class CommandLine:
    """Build commands from bound service methods without import-time state."""

    def __init__(self) -> None:
        self._json = False
        self.app = typer.Typer(no_args_is_help=True)
        self.app.callback()(self.root)
        self._register()

    def _register(self) -> None:
        """Register command names and nested groups."""
        commands: dict[str, Callable[..., object]] = {
            "add": self.add,
            "ls": self.ls,
            "show": self.show,
            "edit": self.edit,
            "move": self.move,
            "comment": self.comment,
            "next": self.next,
            "claim": self.claim,
            "release": self.release,
            "export": self.export,
            "backup": self.backup,
            "reap": self.reap,
            "serve": self.serve,
        }
        for name, method in commands.items():
            self.app.command(name)(method)
        labels = typer.Typer(no_args_is_help=True)
        labels.command("add")(self.label_add)
        labels.command("rm")(self.label_rm)
        labels.callback()(self.group)
        self.app.add_typer(labels, name="label")
        projects = typer.Typer(no_args_is_help=True)
        projects.command("add")(self.project_add)
        projects.command("ls")(self.project_ls)
        projects.command("edit")(self.project_edit)
        projects.callback()(self.group)
        self.app.add_typer(projects, name="projects")
        AgentCommands(self).register(self.app)

    def root(
        self,
        *,
        verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
        json: JsonOption = False,
    ) -> None:
        """Configure diagnostic logging and optional global JSON output."""
        # Windows consoles default to a legacy code page; agent output is UTF-8.
        for stream in (sys.stdout, sys.stderr):
            if isinstance(stream, io.TextIOWrapper):
                stream.reconfigure(encoding="utf-8")
        logging.basicConfig(
            level=logging.DEBUG if verbose else logging.WARNING, stream=sys.stderr
        )
        self._json = json

    def group(self, *, json: JsonOption = False) -> None:
        """Accept machine output selection on group commands."""
        if json:
            self._json = True

    @contextmanager
    def session(
        self, *, json: bool = False
    ) -> Iterator[tuple[Database, Workflow, JotHome]]:
        """Open one data home, translate expected errors, and close reliably."""
        self._json = self._json or json
        try:
            home = JotHome.resolve()
            config = home.initialize()
            with Database.open(home.database) as database:
                yield database, Workflow(database, config.drawdown.default_flow), home
        except (AppError, ValidationError, sqlite3.Error, OSError) as err:
            self.fail(str(err))

    def fail(self, message: str, *, json: bool = False) -> None:
        """Report a handled failure and exit one."""
        if json or self._json:
            sys.stdout.write(Output.json({"error": message}))
        logging.getLogger("jot").error("%s", message)
        raise typer.Exit(1)

    def emit(self, value: object, text: str, *, json: bool) -> None:
        """Write either machine JSON or human text to stdout."""
        sys.stdout.write(Output.json(value) if json or self._json else text)

    @staticmethod
    def records(tasks: list[Task]) -> list[dict[str, object]]:
        """Convert validated tasks to JSON-ready records."""
        return [task.model_dump(mode="json") for task in tasks]

    def add(  # noqa: PLR0913 - Typer requires one parameter per requested CLI option
        self,
        note: str,
        *,
        project: ProjectOption = None,
        label: LabelOption = None,
        crit: CritOption = None,
        type_: TypeOption = None,
        title: Annotated[str | None, typer.Option("--title")] = None,
        repo: Annotated[str | None, typer.Option("--repo")] = None,
        wait: Annotated[bool, typer.Option("--wait", help="Enrich now")] = False,
        json: JsonOption = False,
    ) -> None:
        """Capture a raw note immediately as an inbox task."""
        with self.session(json=json) as (db, _, _home):
            project_id = ProjectRepository(db).by_slug(project).id if project else None
            task = TaskRepository(db).create(
                Task(
                    title=title or note,
                    raw_input=note,
                    project_id=project_id,
                    labels=label or [],
                    criticality=crit or Criticality.MEDIUM,
                    type=type_ or TaskType.IDEA,
                    repo_path=repo,
                )
            )
        if not wait:
            self.emit({"id": task.id}, f"{task.id}\n", json=json)
            return
        AgentCommands(self).enrich(task_ids=[task.id], json=json)

    def ls(  # noqa: PLR0913 - Typer requires one parameter per requested CLI filter
        self,
        *,
        project: ProjectOption = None,
        label: LabelOption = None,
        status: Annotated[list[Status] | None, typer.Option("--status")] = None,
        crit: CritOption = None,
        older_than: Annotated[str | None, typer.Option("--older-than")] = None,
        age_field: Annotated[str, typer.Option("--age-field")] = "updated_at",
        q: Annotated[str | None, typer.Option("--q")] = None,
        include_deleted: Annotated[bool, typer.Option("--include-deleted")] = False,
        limit: Annotated[int | None, typer.Option("--limit", min=0)] = None,
        order: Annotated[str, typer.Option("--order")] = "id",
        json: JsonOption = False,
    ) -> None:
        """List tasks with project, label, status, age, and full-text filters."""
        with self.session(json=json) as (db, _, _home):
            try:
                days = (
                    int(older_than.removesuffix("d"))
                    if older_than is not None
                    else None
                )
            except ValueError as err:
                raise AppError("--older-than must be days, for example 30d") from err
            tasks = TaskRepository(db).query(
                TaskQuery(
                    project=project,
                    labels=tuple(label or []),
                    statuses=tuple(status or []),
                    criticality=crit,
                    older_than=days,
                    age_field=age_field,
                    q=q,
                    include_deleted=include_deleted,
                    limit=limit,
                    order=order,
                )
            )
            rows = self.records(tasks)
            self.emit(
                rows,
                Output.table(rows, ("id", "status", "criticality", "title")),
                json=json,
            )

    def show(self, task_id: int, *, json: JsonOption = False) -> None:
        """Show task content and its event history."""
        with self.session(json=json) as (db, _, _home):
            repo = TaskRepository(db)
            task = repo.get(task_id)
            value = task.model_dump(mode="json") | {
                "events": [
                    e.model_dump(mode="json") for e in repo.events.for_task(task_id)
                ]
            }
            self.emit(value, Output.json(value), json=json)

    def edit(  # noqa: PLR0913 - Typer requires one parameter per editable CLI field
        self,
        task_id: int,
        *,
        title: Annotated[str | None, typer.Option("--title")] = None,
        description: Annotated[str | None, typer.Option("--description")] = None,
        project: ProjectOption = None,
        crit: CritOption = None,
        type_: TypeOption = None,
        repo: Annotated[str | None, typer.Option("--repo")] = None,
        flow: Annotated[Flow | None, typer.Option("--flow")] = None,
        due_at: Annotated[str | None, typer.Option("--due-at")] = None,
        json: JsonOption = False,
    ) -> None:
        """Edit content and overrides without bypassing workflow validation."""
        with self.session(json=json) as (db, _, _home):
            fields: dict[str, object] = {
                "title": title,
                "description": description,
                "criticality": crit,
                "type": type_,
                "repo_path": repo,
                "flow": flow,
                "due_at": due_at,
            }
            changes = {k: v for k, v in fields.items() if v is not None}
            if project:
                changes["project_id"] = ProjectRepository(db).by_slug(project).id
            task = TaskRepository(db).update(task_id, changes)
            self.emit(task.model_dump(mode="json"), f"Updated {task.id}\n", json=json)

    def label_add(self, task_id: int, name: str, *, json: JsonOption = False) -> None:
        """Attach a normalized label to a task."""
        self._label(task_id, name, remove=False, json=json)

    def label_rm(self, task_id: int, name: str, *, json: JsonOption = False) -> None:
        """Remove a label from a task."""
        self._label(task_id, name, remove=True, json=json)

    def _label(self, task_id: int, name: str, *, remove: bool, json: bool) -> None:
        """Apply a label edit and render the task."""
        with self.session(json=json) as (db, _, _home):
            task = TaskRepository(db).label(task_id, name, remove=remove)
            self.emit(
                task.model_dump(mode="json"),
                f"{task.id}: {', '.join(task.labels)}\n",
                json=json,
            )

    def move(self, task_id: int, status: Status, *, json: JsonOption = False) -> None:
        """Move a task through its effective flow."""
        with self.session(json=json) as (_db, workflow, _home):
            task = workflow.move(task_id, status)
            self.emit(
                task.model_dump(mode="json"), f"{task.id}: {task.status}\n", json=json
            )

    def comment(
        self,
        task_id: int,
        body: str,
        *,
        actor: Annotated[str, typer.Option("--actor")] = "cli",
        kind: Annotated[
            str,
            typer.Option("--kind", help="comment, plan, question, answer, or result"),
        ] = "comment",
        json: JsonOption = False,
    ) -> None:
        """Append a comment (or plan/question/answer/result note) to task history."""
        with self.session(json=json) as (db, _, _home):
            repo = TaskRepository(db)
            repo.get(task_id)
            if kind not in NOTE_KINDS:
                raise AppError(f"--kind must be one of {', '.join(sorted(NOTE_KINDS))}")
            event = repo.events.create(
                TaskEvent(
                    task_id=task_id,
                    actor=actor,
                    kind=EventKind(kind),
                    body={"text": body},
                )
            )
            self.emit(event.model_dump(mode="json"), f"Comment {event.id}\n", json=json)

    def next(
        self,
        *,
        n: Annotated[int, typer.Option("-n", min=0)] = 1,
        project: ProjectOption = None,
        json: JsonOption = False,
    ) -> None:
        """Show the highest-priority eligible ready tasks."""
        with self.session(json=json) as (_db, workflow, _home):
            rows = self.records(workflow.next(project=project, limit=n))
            self.emit(
                rows, Output.table(rows, ("id", "criticality", "title")), json=json
            )

    def claim(  # noqa: PLR0913 - Typer requires explicit CLI option parameters
        self,
        task_id: Annotated[int | None, typer.Argument()] = None,
        *,
        next_: Annotated[bool, typer.Option("--next")] = False,
        agent: Annotated[str, typer.Option("--agent")],
        project: ProjectOption = None,
        direct: Annotated[bool, typer.Option("--direct")] = False,
        lease_seconds: Annotated[int, typer.Option("--lease-seconds", min=1)] = 300,
        json: JsonOption = False,
    ) -> None:
        """Claim a specific task or the next eligible task atomically."""
        with self.session(json=json) as (_db, workflow, _home):
            if next_ == (task_id is not None):
                raise WorkflowError("Specify exactly one of <id> or --next")
            override = Flow.DIRECT if direct else None
            task = self._claim(
                workflow, task_id, agent, (project, override, lease_seconds)
            )
            value = {
                "claimed": task is not None,
                "task": task.model_dump(mode="json") if task else None,
            }
            self.emit(value, f"{task.id}\n" if task else "No task claimed\n", json=json)

    @staticmethod
    def _claim(
        workflow: Workflow,
        task_id: int | None,
        agent: str,
        options: tuple[str | None, Flow | None, int],
    ) -> Task | None:
        """Resolve claim target from the task's effective policy."""
        project, override, lease_seconds = options
        if task_id is None:
            return workflow.claim_next(
                agent, override, project, lease_seconds=lease_seconds
            )
        task = workflow.tasks.get(task_id)
        flow = workflow.flow_for(task, override)
        target = (
            Status.EXECUTING
            if task.status == Status.AWAITING_APPROVAL or flow == Flow.DIRECT
            else Status.PLANNING
        )
        return (
            workflow.tasks.get(task_id)
            if workflow.claim(task_id, agent, target, lease_seconds, flow=flow)
            else None
        )

    def release(
        self,
        task_id: int,
        *,
        agent: Annotated[str, typer.Option("--agent")],
        status: Annotated[Status | None, typer.Option("--status")] = None,
        json: JsonOption = False,
    ) -> None:
        """Release a live owned claim, optionally publishing its next state."""
        with self.session(json=json) as (_db, workflow, _home):
            released = workflow.release(task_id, agent, target_status=status)
            self.emit(
                {"released": released, "id": task_id},
                f"Released: {released}\n",
                json=json,
            )

    def project_add(  # noqa: PLR0913 - Typer requires one parameter per project option
        self,
        slug: str,
        name: str,
        *,
        repo: Annotated[str | None, typer.Option("--repo")] = None,
        priority: Annotated[float, typer.Option("--priority", min=0.001)] = 1,
        alias: Annotated[list[str] | None, typer.Option("--alias")] = None,
        default_flow: Annotated[Flow | None, typer.Option("--default-flow")] = None,
        description: Annotated[str, typer.Option("--description")] = "",
        json: JsonOption = False,
    ) -> None:
        """Create project context and flow defaults."""
        with self.session(json=json) as (db, _, _home):
            project = ProjectRepository(db).create(
                Project(
                    slug=slug,
                    name=name,
                    repo_path=repo,
                    priority=priority,
                    aliases=alias or [],
                    default_flow=default_flow,
                    description=description,
                )
            )
            self.emit(
                project.model_dump(mode="json"),
                f"{project.id}: {project.slug}\n",
                json=json,
            )

    def project_ls(self, *, json: JsonOption = False) -> None:
        """List projects and their priorities."""
        with self.session(json=json) as (db, _, _home):
            rows = [p.model_dump(mode="json") for p in ProjectRepository(db).list()]
            self.emit(
                rows, Output.table(rows, ("id", "slug", "name", "priority")), json=json
            )

    def project_edit(  # noqa: PLR0913 - Typer requires one parameter per project field
        self,
        slug: str,
        *,
        name: Annotated[str | None, typer.Option("--name")] = None,
        repo: Annotated[str | None, typer.Option("--repo")] = None,
        priority: Annotated[float | None, typer.Option("--priority", min=0.001)] = None,
        default_flow: Annotated[Flow | None, typer.Option("--default-flow")] = None,
        description: Annotated[str | None, typer.Option("--description")] = None,
        alias: Annotated[list[str] | None, typer.Option("--alias")] = None,
        archived: Annotated[bool | None, typer.Option("--archived/--active")] = None,
        json: JsonOption = False,
    ) -> None:
        """Edit project defaults and aliases."""
        with self.session(json=json) as (db, _, _home):
            repo_service = ProjectRepository(db)
            fields: dict[str, object] = {
                "name": name,
                "repo_path": repo,
                "priority": priority,
                "default_flow": default_flow,
                "description": description,
                "aliases": alias,
                "archived": archived,
            }
            project = repo_service.update(
                repo_service.by_slug(slug).id,
                {k: v for k, v in fields.items() if v is not None},
            )
            self.emit(
                project.model_dump(mode="json"), f"Updated {project.slug}\n", json=json
            )

    def export(
        self,
        *,
        format_: Annotated[str, typer.Option("--format")] = "jsonl",
        include_deleted: Annotated[bool, typer.Option("--include-deleted")] = False,
        json: JsonOption = False,
    ) -> None:
        """Export task content as JSON Lines or Markdown."""
        with self.session(json=json) as (db, _, _home):
            content = ArchiveService(db).export(
                format_, include_deleted=include_deleted
            )
            self.emit({"format": format_, "content": content}, content, json=json)

    def backup(self, *, json: JsonOption = False) -> None:
        """Save a consistent database snapshot under the data home."""
        with self.session(json=json) as (db, _, home):
            path = ArchiveService(db).backup(home.path / "backups")
            self.emit({"path": str(path)}, f"{path}\n", json=json)

    def reap(self, *, json: JsonOption = False) -> None:
        """Restore expired leases and report affected ids."""
        with self.session(json=json) as (_db, workflow, _home):
            ids = workflow.reap()
            self.emit({"reaped": ids}, f"Reaped {len(ids)} task(s)\n", json=json)

    def serve(
        self,
        *,
        host: Annotated[str | None, typer.Option("--host")] = None,
        port: Annotated[int | None, typer.Option("--port", min=1, max=65535)] = None,
        open_browser: Annotated[bool, typer.Option("--open")] = False,
    ) -> None:
        """Serve the local web application using configured host and port defaults."""
        # Keep optional web imports out of non-server commands.
        from jot.server.launch import ServerLauncher  # noqa: PLC0415

        try:
            ServerLauncher().run(host=host, port=port, open_browser=open_browser)
        except (AppError, OSError) as err:
            self.fail(str(err))
