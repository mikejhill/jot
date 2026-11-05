"""The `jot` command line: capture, organize, and query jots.

The CLI is a first-class automation path. Every command accepts ``--json``,
writes accept ``--actor`` and ``--reason``, and edits accept ``--if-version``.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Annotated

import typer
from pydantic import JsonValue, ValidationError

from jot.app.commands import JotPatch, Placement
from jot.app.queries import JotFilter, Sort
from jot.app.session import Session
from jot.cli.output import Emittable, Output, Plain
from jot.core.models import Origin, StatusCategory, WriteContext
from jot.exceptions import AppError, InvalidInputError

logger = logging.getLogger("jot")

SKILL_TARGETS = {
    "claude": Path(".claude") / "skills",
    "codex": Path(".codex") / "skills",
    "copilot": Path(".copilot") / "skills",
}
DETAIL_SECTIONS = frozenset({"comments", "history", "links"})

Json = Annotated[bool, typer.Option("--json", help="Emit JSON")]
Actor = Annotated[
    str | None,
    typer.Option("--actor", help="Who is writing (default: $JOT_ACTOR or 'user')"),
]
Reason = Annotated[
    str | None, typer.Option("--reason", help="Why; recorded in history")
]
IfVersion = Annotated[
    int | None,
    typer.Option(
        "--if-version", help="Fail (exit 3) unless the jot is at this version"
    ),
]
JotId = Annotated[int, typer.Argument(help="Jot id")]
Profile = Annotated[str | None, typer.Option("--profile", "-p")]
Project = Annotated[str | None, typer.Option("--project", "-P", help="Project slug")]
Labels = Annotated[list[str] | None, typer.Option("--label", "-l")]
Fields = Annotated[
    list[str] | None, typer.Option("--field", "-f", help="Profile field: name=value")
]
Meta = Annotated[
    list[str] | None,
    typer.Option("--meta", "-m", help="Metadata namespace: ns=<json or text>"),
]


class CommandLine:
    """Typer application built from bound methods (no import-time state)."""

    def __init__(self) -> None:
        self._json = False
        self.app = typer.Typer(no_args_is_help=True, add_completion=False)
        self.app.callback()(self.root)
        self._register()

    def _register(self) -> None:
        """Register commands and groups."""
        commands: dict[str, Callable[..., None]] = {
            "add": self.add,
            "ls": self.ls,
            "show": self.show,
            "edit": self.edit,
            "status": self.status,
            "label": self.label,
            "rank": self.rank,
            "archive": self.archive,
            "unarchive": self.unarchive,
            "rm": self.rm,
            "restore": self.restore,
            "history": self.history,
            "comment": self.comment,
            "comment-edit": self.comment_edit,
            "comment-rm": self.comment_rm,
            "link": self.link,
            "unlink": self.unlink,
            "labels": self.labels,
            "events": self.events,
            "install-skills": self.install_skills,
            "serve": self.serve,
        }
        for name, method in commands.items():
            self.app.command(name)(method)
        projects = typer.Typer(no_args_is_help=True, help="Manage projects")
        projects.command("add")(self.project_add)
        projects.command("ls")(self.project_ls)
        projects.command("edit")(self.project_edit)
        self.app.add_typer(projects, name="project")
        profiles = typer.Typer(no_args_is_help=True, help="Inspect profiles")
        profiles.command("ls")(self.profile_ls)
        profiles.command("show")(self.profile_show)
        self.app.add_typer(profiles, name="profile")

    # Plumbing -------------------------------------------------------------

    def root(
        self,
        *,
        verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
        json: Json = False,
    ) -> None:
        """Jot: capture, organize, and retrieve bite-sized information."""
        for stream in (sys.stdout, sys.stderr):
            if isinstance(stream, io.TextIOWrapper):
                stream.reconfigure(encoding="utf-8")
        logging.basicConfig(
            level=logging.DEBUG if verbose else logging.WARNING, stream=sys.stderr
        )
        self._json = json

    @contextmanager
    def session(self, *, json: bool) -> Iterator[Session]:
        """Open the data home and turn expected errors into exit codes."""
        self._json = self._json or json
        try:
            with Session.open() as session:
                yield session
        except AppError as err:
            self.fail(str(err), err.exit_code)
        except (ValidationError, sqlite3.Error, OSError) as err:
            self.fail(str(err), 1)

    def fail(self, message: str, code: int) -> None:
        """Report a handled failure on stderr (and stdout as JSON) and exit."""
        if self._json:
            sys.stdout.write(Output.json({"error": message, "code": code}))
        sys.stderr.write(f"error: {message}\n")
        raise typer.Exit(code)

    def emit(self, value: Emittable, text: str) -> None:
        """Write JSON or text to stdout."""
        if self._json:
            sys.stdout.write(Output.json(value))
        else:
            sys.stdout.write(text)

    @staticmethod
    def context(actor: str | None, reason: str | None) -> WriteContext:
        """Build the write context for this invocation."""
        kwargs: dict[str, str] = {}
        if correlation := os.environ.get("JOT_CORRELATION_ID"):
            kwargs["correlation_id"] = correlation
        return WriteContext(
            actor=actor or os.environ.get("JOT_ACTOR") or "user",
            origin=Origin.CLI,
            reason=reason,
            **kwargs,
        )

    @staticmethod
    def pairs(values: list[str] | None, *, parse_json: bool) -> dict[str, JsonValue]:
        """Parse ``key=value`` options; an empty value means remove (None)."""
        result: dict[str, JsonValue] = {}
        for item in values or []:
            key, sep, raw = item.partition("=")
            if not sep or not key:
                raise InvalidInputError(f"Expected key=value, got {item!r}")
            if raw == "":
                result[key] = None
            elif parse_json:
                try:
                    result[key] = json.loads(raw)
                except json.JSONDecodeError:
                    result[key] = raw
            else:
                result[key] = raw
        return result

    # Jots -----------------------------------------------------------------

    def add(  # noqa: PLR0913 - one parameter per CLI option
        self,
        text: Annotated[list[str] | None, typer.Argument(help="Jot text")] = None,
        *,
        title: Annotated[str | None, typer.Option("--title", "-t")] = None,
        stdin: Annotated[
            bool, typer.Option("--stdin", help="Read the body from stdin")
        ] = False,
        profile: Profile = None,
        status: Annotated[str | None, typer.Option("--status", "-s")] = None,
        project: Project = None,
        label: Labels = None,
        field: Fields = None,
        meta: Meta = None,
        actor: Actor = None,
        reason: Reason = None,
        json: Json = False,
    ) -> None:
        """Capture a jot. A one-line jot becomes the title; longer text the body."""
        content = sys.stdin.read() if stdin else " ".join(text or [])
        heading = title or ""
        if title is None and "\n" not in content.strip():
            heading, content = content.strip(), ""
        with self.session(json=json) as session:
            jot = session.commands(self.context(actor, reason)).create(
                title=heading,
                body=content,
                profile=profile,
                status=status,
                project=project,
                labels=label or [],
                fields=self.pairs(field, parse_json=False),
                metadata=self.pairs(meta, parse_json=True),
            )
        self.emit(jot, f"{jot.id}\n")

    def ls(  # noqa: PLR0913 - one parameter per CLI filter
        self,
        *,
        profile: Annotated[list[str] | None, typer.Option("--profile", "-p")] = None,
        status: Annotated[list[str] | None, typer.Option("--status", "-s")] = None,
        category: Annotated[
            list[StatusCategory] | None, typer.Option("--category", "-c")
        ] = None,
        project: Project = None,
        label: Labels = None,
        search: Annotated[str | None, typer.Option("--search", "-q")] = None,
        include_archived: Annotated[bool, typer.Option("--archived", "-a")] = False,
        include_deleted: Annotated[bool, typer.Option("--deleted")] = False,
        sort: Annotated[Sort, typer.Option("--sort")] = Sort.RANK,
        limit: Annotated[int | None, typer.Option("--limit", "-n", min=0)] = None,
        json: Json = False,
    ) -> None:
        """List jots in rank order. Archived and deleted jots are hidden by default."""
        criteria = JotFilter(
            profiles=profile or [],
            statuses=status or [],
            categories=category or [],
            project=project,
            labels=label or [],
            text=search,
            include_archived=include_archived,
            include_deleted=include_deleted,
            sort=sort,
            limit=limit,
        )
        with self.session(json=json) as session:
            jots = session.queries.jots(criteria)
        self.emit(jots, Output.lines(jots))

    def show(
        self,
        jot_id: JotId,
        *,
        with_: Annotated[
            str,
            typer.Option("--with", help="Comma list: comments, links, history"),
        ] = "comments,links",
        json: Json = False,
    ) -> None:
        """Show a jot with its comments and links (add history with --with)."""
        sections = {part.strip() for part in with_.split(",") if part.strip()}
        with self.session(json=json) as session:
            if unknown := sections - DETAIL_SECTIONS:
                raise InvalidInputError(f"Unknown section {sorted(unknown)[0]!r}")
            detail = session.queries.detail(jot_id)
        if self._json:
            data = detail.model_dump(mode="json")
            for section in DETAIL_SECTIONS - sections:
                data.pop(section)
            self.emit(data, "")
        else:
            self.emit(detail, Output.detail(detail, sections))

    def edit(  # noqa: PLR0913 - one parameter per CLI option
        self,
        jot_id: JotId,
        *,
        title: Annotated[str | None, typer.Option("--title", "-t")] = None,
        body: Annotated[str | None, typer.Option("--body", "-b")] = None,
        stdin: Annotated[
            bool, typer.Option("--stdin", help="Read the body from stdin")
        ] = False,
        project: Annotated[
            str | None, typer.Option("--project", "-P", help="Slug; '' clears it")
        ] = None,
        profile: Annotated[str | None, typer.Option("--profile", "-p")] = None,
        field: Fields = None,
        meta: Meta = None,
        if_version: IfVersion = None,
        base_version: Annotated[
            int | None,
            typer.Option(
                "--base-version",
                help="Fail only if a field you are changing changed since this version",
            ),
        ] = None,
        actor: Actor = None,
        reason: Reason = None,
        json: Json = False,
    ) -> None:
        """Change a jot's title, body, project, profile, fields, or metadata."""
        values: dict[str, object] = {
            "title": title,
            "body": sys.stdin.read() if stdin else body,
            "profile": profile,
        }
        patch_args = {key: value for key, value in values.items() if value is not None}
        if project is not None:
            patch_args["project"] = project or None
        with self.session(json=json) as session:
            patch = JotPatch.model_validate(
                patch_args
                | {
                    "fields": self.pairs(field, parse_json=False),
                    "metadata": self.pairs(meta, parse_json=True),
                }
            )
            jot = session.commands(self.context(actor, reason)).update(
                jot_id, patch, expected_version=if_version, base_version=base_version
            )
        self.emit(jot, Output.jot(jot))

    def status(  # noqa: PLR0913 - one parameter per CLI option
        self,
        jot_id: JotId,
        status: Annotated[str, typer.Argument(help="Target status")],
        *,
        if_version: IfVersion = None,
        actor: Actor = None,
        reason: Reason = None,
        json: Json = False,
    ) -> None:
        """Move a jot to another status of its profile."""
        with self.session(json=json) as session:
            jot = session.commands(self.context(actor, reason)).set_status(
                jot_id, status, expected_version=if_version
            )
        self.emit(jot, Output.line(jot))

    def label(  # noqa: PLR0913 - one parameter per CLI option
        self,
        jot_id: JotId,
        *,
        add: Annotated[list[str] | None, typer.Option("--add", "-a")] = None,
        remove: Annotated[list[str] | None, typer.Option("--rm", "-r")] = None,
        if_version: IfVersion = None,
        actor: Actor = None,
        reason: Reason = None,
        json: Json = False,
    ) -> None:
        """Add (--add) and remove (--rm) labels."""
        with self.session(json=json) as session:
            jot = session.commands(self.context(actor, reason)).label(
                jot_id, add=add or [], remove=remove or [], expected_version=if_version
            )
        self.emit(jot, Output.line(jot))

    def rank(  # noqa: PLR0913 - one parameter per CLI option
        self,
        jot_id: JotId,
        *,
        top: Annotated[bool, typer.Option("--top")] = False,
        bottom: Annotated[bool, typer.Option("--bottom")] = False,
        before: Annotated[int | None, typer.Option("--before")] = None,
        after: Annotated[int | None, typer.Option("--after")] = None,
        actor: Actor = None,
        reason: Reason = None,
        json: Json = False,
    ) -> None:
        """Reorder a jot: --top, --bottom, --before ID, or --after ID."""
        choices = [
            (Placement.TOP, None) if top else None,
            (Placement.BOTTOM, None) if bottom else None,
            (Placement.BEFORE, before) if before is not None else None,
            (Placement.AFTER, after) if after is not None else None,
        ]
        picked = [choice for choice in choices if choice is not None]
        with self.session(json=json) as session:
            if len(picked) != 1:
                raise InvalidInputError(
                    "Give exactly one of --top, --bottom, --before, --after"
                )
            placement, anchor = picked[0]
            jot = session.commands(self.context(actor, reason)).move_rank(
                jot_id, placement, anchor
            )
        self.emit(jot, Output.line(jot))

    def archive(
        self,
        jot_id: JotId,
        *,
        actor: Actor = None,
        reason: Reason = None,
        json: Json = False,
    ) -> None:
        """Hide a jot from default lists."""
        with self.session(json=json) as session:
            jot = session.commands(self.context(actor, reason)).archive(jot_id)
        self.emit(jot, Output.line(jot))

    def unarchive(
        self,
        jot_id: JotId,
        *,
        actor: Actor = None,
        reason: Reason = None,
        json: Json = False,
    ) -> None:
        """Bring an archived jot back."""
        with self.session(json=json) as session:
            jot = session.commands(self.context(actor, reason)).archive(
                jot_id, archived=False
            )
        self.emit(jot, Output.line(jot))

    def rm(
        self,
        jot_id: JotId,
        *,
        actor: Actor = None,
        reason: Reason = None,
        json: Json = False,
    ) -> None:
        """Delete a jot (soft: `jot restore` brings it back, history is kept)."""
        with self.session(json=json) as session:
            jot = session.commands(self.context(actor, reason)).delete(jot_id)
        self.emit(jot, f"Deleted {jot.id}\n")

    def restore(
        self,
        jot_id: JotId,
        *,
        actor: Actor = None,
        reason: Reason = None,
        json: Json = False,
    ) -> None:
        """Restore a deleted jot."""
        with self.session(json=json) as session:
            jot = session.commands(self.context(actor, reason)).delete(
                jot_id, deleted=False
            )
        self.emit(jot, Output.line(jot))

    def history(self, jot_id: JotId, *, json: Json = False) -> None:
        """Show a jot's change history."""
        with self.session(json=json) as session:
            session.queries.get(jot_id, include_deleted=True)
            events = session.queries.history(jot_id)
        self.emit(events, "".join(Output.event(event) for event in events))

    # Comments and links ---------------------------------------------------

    def comment(  # noqa: PLR0913 - one parameter per CLI option
        self,
        jot_id: JotId,
        text: Annotated[list[str], typer.Argument(help="Comment text")],
        *,
        kind: Annotated[str, typer.Option("--kind")] = "comment",
        reply_to: Annotated[int | None, typer.Option("--reply-to")] = None,
        actor: Actor = None,
        reason: Reason = None,
        json: Json = False,
    ) -> None:
        """Add a comment to a jot."""
        with self.session(json=json) as session:
            comment = session.commands(self.context(actor, reason)).comment(
                jot_id, " ".join(text), kind=kind, reply_to=reply_to
            )
        self.emit(comment, f"{comment.id}\n")

    def comment_edit(
        self,
        comment_id: Annotated[int, typer.Argument(help="Comment id")],
        text: Annotated[list[str], typer.Argument(help="New text")],
        *,
        actor: Actor = None,
        reason: Reason = None,
        json: Json = False,
    ) -> None:
        """Replace a comment's text (the old text stays in history)."""
        with self.session(json=json) as session:
            comment = session.commands(self.context(actor, reason)).edit_comment(
                comment_id, " ".join(text)
            )
        self.emit(comment, Output.comment(comment))

    def comment_rm(
        self,
        comment_id: Annotated[int, typer.Argument(help="Comment id")],
        *,
        actor: Actor = None,
        reason: Reason = None,
        json: Json = False,
    ) -> None:
        """Delete a comment (its text stays in history)."""
        with self.session(json=json) as session:
            session.commands(self.context(actor, reason)).delete_comment(comment_id)
        self.emit({"deleted": comment_id}, f"Deleted comment {comment_id}\n")

    def link(  # noqa: PLR0913 - one parameter per CLI option
        self,
        src: Annotated[int, typer.Argument(help="From jot")],
        dst: Annotated[int, typer.Argument(help="To jot")],
        *,
        rel: Annotated[
            str, typer.Option("--rel", help="parent, relates, blocks, duplicates")
        ] = "relates",
        actor: Actor = None,
        reason: Reason = None,
        json: Json = False,
    ) -> None:
        """Link two jots (src REL dst, e.g. 3 parent 5 = 3 is the parent of 5)."""
        with self.session(json=json) as session:
            link = session.commands(self.context(actor, reason)).link(src, dst, rel)
        self.emit(link, f"{link.src} {link.rel} {link.dst}\n")

    def unlink(  # noqa: PLR0913 - one parameter per CLI option
        self,
        src: Annotated[int, typer.Argument(help="From jot")],
        dst: Annotated[int, typer.Argument(help="To jot")],
        *,
        rel: Annotated[str, typer.Option("--rel")] = "relates",
        actor: Actor = None,
        reason: Reason = None,
        json: Json = False,
    ) -> None:
        """Remove a link."""
        with self.session(json=json) as session:
            session.commands(self.context(actor, reason)).unlink(src, dst, rel)
        self.emit({"src": src, "dst": dst, "rel": rel}, "Unlinked\n")

    # Catalog ----------------------------------------------------------------

    def labels(self, *, json: Json = False) -> None:
        """List labels with their jot counts."""
        with self.session(json=json) as session:
            rows = session.queries.labels()
        data: Plain = [{"name": name, "count": count} for name, count in rows]
        self.emit(data, "".join(f"{count:>5}  {name}\n" for name, count in rows))

    def events(
        self,
        *,
        after: Annotated[int, typer.Option("--after", help="Event id cursor")] = 0,
        limit: Annotated[int, typer.Option("--limit", "-n", min=1)] = 100,
        json: Json = False,
    ) -> None:
        """Print the change feed after an event id (for scripts and plugins)."""
        with self.session(json=json) as session:
            events = session.queries.events(after, limit)
        self.emit(
            events,
            "".join(
                f"{e.id:>6} jot {e.jot_id or '-'}{Output.event(e)}" for e in events
            ),
        )

    def project_add(
        self,
        slug: Annotated[str, typer.Argument(help="Short id, e.g. jot")],
        name: Annotated[str, typer.Argument(help="Display name")],
        *,
        description: Annotated[str, typer.Option("--description", "-d")] = "",
        actor: Actor = None,
        json: Json = False,
    ) -> None:
        """Create a project."""
        with self.session(json=json) as session:
            project = session.commands(self.context(actor, None)).create_project(
                slug, name, description
            )
        self.emit(project, Output.project(project))

    def project_ls(
        self,
        *,
        include_archived: Annotated[bool, typer.Option("--archived", "-a")] = False,
        json: Json = False,
    ) -> None:
        """List projects."""
        with self.session(json=json) as session:
            projects = session.queries.projects(include_archived=include_archived)
        self.emit(
            projects, "".join(Output.project(p) for p in projects) or "No projects\n"
        )

    def project_edit(  # noqa: PLR0913 - one parameter per CLI option
        self,
        slug: Annotated[str, typer.Argument(help="Project slug")],
        *,
        name: Annotated[str | None, typer.Option("--name")] = None,
        description: Annotated[str | None, typer.Option("--description", "-d")] = None,
        archived: Annotated[bool | None, typer.Option("--archive/--unarchive")] = None,
        actor: Actor = None,
        json: Json = False,
    ) -> None:
        """Rename, describe, archive, or restore a project."""
        with self.session(json=json) as session:
            project = session.commands(self.context(actor, None)).update_project(
                slug, name=name, description=description, archived=archived
            )
        self.emit(project, Output.project(project))

    def profile_ls(self, *, json: Json = False) -> None:
        """List profiles with their statuses."""
        with self.session(json=json) as session:
            profiles = session.profiles.all()
        self.emit(profiles, "".join(Output.profile(p) for p in profiles))

    def profile_show(
        self, profile_id: Annotated[str, typer.Argument()], *, json: Json = False
    ) -> None:
        """Show one profile."""
        with self.session(json=json) as session:
            profile = session.profiles.get(profile_id)
        self.emit(profile, Output.profile(profile))

    def install_skills(
        self,
        *,
        target: Annotated[
            list[str] | None,
            typer.Option("--target", help="claude, codex, copilot (repeatable)"),
        ] = None,
        dest: Annotated[
            Path | None, typer.Option("--dest", help="Install into <dest>/jot instead")
        ] = None,
        json: Json = False,
    ) -> None:
        """Install the `jot` agent skill so coding agents can drive Jot's CLI."""
        self._json = self._json or json
        names = target or ["claude"]
        if unknown := [name for name in names if name not in SKILL_TARGETS]:
            self.fail(
                f"Unknown target {unknown[0]!r}; use {', '.join(SKILL_TARGETS)}", 1
            )
        roots = [dest] if dest else [Path.home() / SKILL_TARGETS[n] for n in names]
        installed: list[str] = []
        with resources.as_file(resources.files("jot") / "skills" / "jot") as skill:
            for root in roots:
                folder = root / "jot"
                shutil.copytree(skill, folder, dirs_exist_ok=True)
                installed.append(str(folder))
        self.emit(installed, "".join(f"Installed {path}\n" for path in installed))

    def serve(
        self,
        *,
        host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
        port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8765,
        open_browser: Annotated[
            bool, typer.Option("--open", help="Open a browser")
        ] = False,
    ) -> None:
        """Serve the web UI and HTTP API (docs at /api/docs)."""
        from jot.server.launch import ServerLauncher  # noqa: PLC0415 - lazy: web deps

        try:
            launcher = ServerLauncher()
            sys.stderr.write(f"Jot at {launcher.url(host, port)}\n")
            launcher.run(host=host, port=port, open_browser=open_browser)
        except AppError as err:
            self.fail(str(err), err.exit_code)
