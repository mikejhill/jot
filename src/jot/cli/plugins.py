"""`jot plugin` and `jot job`: administer plugins and run their work."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated

import typer
from pydantic import ValidationError

from jot.db.store import METADATA
from jot.exceptions import InvalidInputError
from jot.plugins.api import JobStatus
from jot.plugins.host import PluginCatalog, PluginHost, PluginStatus, Worker

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from pydantic import BaseModel

    from jot.cli.main import CommandLine
    from jot.core.models import Metadata
    from jot.plugins.api import Job

Json = Annotated[bool, typer.Option("--json", help="Emit JSON")]
Actor = Annotated[str | None, typer.Option("--actor", help="Who is writing")]
PluginId = Annotated[str, typer.Argument(help="Plugin id")]


class PluginCommands:
    """Plugin and job commands, sharing the main CLI's plumbing."""

    def __init__(self, cli: CommandLine, catalog: PluginCatalog | None = None) -> None:
        self.cli = cli
        self._catalog = catalog

    @property
    def catalog(self) -> PluginCatalog:
        """Discover installed plugins on first use."""
        if self._catalog is None:
            self._catalog = PluginCatalog.discover()
        return self._catalog

    def register(self, app: typer.Typer) -> None:
        """Add the `plugin` and `job` groups."""
        plugins = typer.Typer(no_args_is_help=True, help="Manage plugins")
        plugins.command("ls")(self.ls)
        plugins.command("show")(self.show)
        plugins.command("enable")(self.enable)
        plugins.command("disable")(self.disable)
        plugins.command("config")(self.config)
        plugins.command("failures")(self.failures)
        plugins.command("run")(self.run)
        app.add_typer(plugins, name="plugin")
        jobs = typer.Typer(no_args_is_help=True, help="Inspect plugin jobs")
        jobs.command("ls")(self.jobs)
        jobs.command("cancel")(self.cancel)
        jobs.command("retry")(self.retry)
        app.add_typer(jobs, name="job")

    def mount(self, app: typer.Typer, taken: set[str]) -> None:
        """Mount each installed plugin's own commands as ``jot <id> ...``."""
        for plugin_id, entry in sorted(self.catalog.entries.items()):
            if entry.plugin is None or plugin_id in taken:
                continue
            group = entry.plugin.commands(self)
            if group is not None:
                app.add_typer(group, name=plugin_id)

    @contextmanager
    def host(self, *, json: bool) -> Iterator[PluginHost]:
        """Open a session (errors become exit codes) and bind a host to it."""
        with self.cli.session(json=json) as session:
            yield PluginHost(session, self.catalog)

    def emit(self, value: BaseModel | Sequence[BaseModel], text: str) -> None:
        """Write JSON or text through the main CLI."""
        self.cli.emit(value, text)

    @staticmethod
    def line(status: PluginStatus) -> str:
        """One row per plugin."""
        if status.error:
            return f"{status.id:<16} broken   {status.error}\n"
        state = "enabled " if status.enabled else "disabled"
        extra = f"  lag {status.lag}" if status.enabled else ""
        if status.last_error:
            extra += f"  last error: {status.last_error}"
        return (
            f"{status.id:<16} {state} v{status.version}  {status.description}{extra}\n"
        )

    @staticmethod
    def job_line(job: Job) -> str:
        """One row per job."""
        jot = f" jot {job.jot_id}" if job.jot_id else ""
        error = f"  {job.error}" if job.error else ""
        return (
            f"{job.id:>6}  {job.plugin}:{job.kind:<16} {job.status.value:<9}"
            f" {job.attempts}/{job.max_attempts}{jot}{error}\n"
        )

    def ls(self, *, json: Json = False) -> None:
        """List installed plugins."""
        with self.host(json=json) as host:
            statuses = host.statuses()
        self.cli.emit(statuses, "".join(map(self.line, statuses)) or "No plugins\n")

    def show(self, plugin_id: PluginId, *, json: Json = False) -> None:
        """Show a plugin's state, settings, and settings schema."""
        with self.host(json=json) as host:
            status = host.status(plugin_id)
        text = self.line(status) + f"settings: {json_text(status.settings)}\n"
        self.cli.emit(status, text)

    def enable(
        self,
        plugin_id: PluginId,
        *,
        from_start: Annotated[
            bool, typer.Option("--from-start", help="Replay all past events")
        ] = False,
        actor: Actor = None,
        json: Json = False,
    ) -> None:
        """Enable a plugin; it sees events from now on unless --from-start."""
        with self.host(json=json) as host:
            status = host.enable(
                plugin_id, self.cli.context(actor, None), from_start=from_start
            )
        self.cli.emit(status, self.line(status))

    def disable(
        self, plugin_id: PluginId, *, actor: Actor = None, json: Json = False
    ) -> None:
        """Disable a plugin."""
        with self.host(json=json) as host:
            status = host.disable(plugin_id, self.cli.context(actor, None))
        self.cli.emit(status, self.line(status))

    def config(
        self,
        plugin_id: PluginId,
        *,
        settings: Annotated[
            str | None, typer.Option("--set", help="Settings as a JSON object")
        ] = None,
        file: Annotated[
            typer.FileText | None,
            typer.Option("--file", help="Read settings JSON from a file ('-' = stdin)"),
        ] = None,
        actor: Actor = None,
        json: Json = False,
    ) -> None:
        """Print a plugin's settings, or replace them with --set or --file."""
        with self.host(json=json) as host:
            raw = settings if settings is not None else (file.read() if file else None)
            if raw is None:
                status = host.status(plugin_id)
            else:
                status = host.configure(
                    plugin_id, json_object(raw), self.cli.context(actor, None)
                )
        self.cli.emit(status.settings, json_text(status.settings) + "\n")

    def failures(self, plugin_id: PluginId, *, json: Json = False) -> None:
        """List events a plugin skipped after repeated errors."""
        with self.host(json=json) as host:
            host.catalog.get(plugin_id)
            found = host.failures(plugin_id)
        text = "".join(
            f"event {f.event_id}  {f.attempts} attempts  {f.error}\n" for f in found
        )
        self.cli.emit(found, text or "No failures\n")

    def run(
        self,
        *,
        once: Annotated[
            bool, typer.Option("--once", help="One pass, then exit")
        ] = False,
        interval: Annotated[
            float, typer.Option("--interval", min=0.1, help="Seconds between passes")
        ] = 1.0,
        json: Json = False,
    ) -> None:
        """Deliver events to enabled plugins and run their jobs."""
        self.cli._json = self.cli._json or json
        with self.cli.session(json=json) as session:
            home = session.home
        worker = Worker(home, self.catalog, interval=interval)
        try:
            result = worker.run(once=once)
        except KeyboardInterrupt:
            return
        self.cli.emit(
            result, f"delivered {result.delivered} events, ran {result.jobs} jobs\n"
        )

    def jobs(
        self,
        *,
        plugin: Annotated[str | None, typer.Option("--plugin")] = None,
        status: Annotated[list[JobStatus] | None, typer.Option("--status")] = None,
        limit: Annotated[int, typer.Option("--limit", "-n", min=1)] = 50,
        json: Json = False,
    ) -> None:
        """List plugin jobs, newest first."""
        with self.host(json=json) as host:
            found = host.jobs(plugin_id=plugin, statuses=status, limit=limit)
        self.cli.emit(found, "".join(map(self.job_line, found)) or "No jobs\n")

    def cancel(
        self, job_id: Annotated[int, typer.Argument()], *, json: Json = False
    ) -> None:
        """Cancel a queued or running job."""
        with self.host(json=json) as host:
            job = host.cancel(job_id)
        self.cli.emit(job, self.job_line(job))

    def retry(
        self, job_id: Annotated[int, typer.Argument()], *, json: Json = False
    ) -> None:
        """Requeue a failed or cancelled job."""
        with self.host(json=json) as host:
            job = host.retry(job_id)
        self.cli.emit(job, self.job_line(job))


def json_object(raw: str) -> Metadata:
    """Parse a JSON object or raise InvalidInputError."""
    try:
        return METADATA.validate_json(raw)
    except ValidationError as err:
        raise InvalidInputError(f"Settings must be a JSON object: {err}") from err


def json_text(value: object) -> str:
    """Render a value as indented JSON."""
    return json.dumps(value, indent=2, sort_keys=True)
