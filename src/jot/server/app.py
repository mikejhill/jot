"""HTTP API and web UI over the shared command layer.

Every route opens a short-lived session, so the server holds no state beyond
the data home. Writes take their context from ``X-Jot-*`` headers.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path
from typing import Annotated

from fastapi import Body, Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, JsonValue, ValidationError
from starlette.middleware.base import RequestResponseEndpoint

from jot.app.commands import Commands
from jot.app.queries import JotDetail, JotFilter, Sort
from jot.app.session import Session
from jot.config import JotHome
from jot.core.models import (
    Comment,
    Event,
    Jot,
    Link,
    Metadata,
    Origin,
    Project,
    StatusCategory,
    WriteContext,
)
from jot.core.profiles import Profile
from jot.exceptions import AppError, ConflictError, NotFoundError
from jot.plugins.api import Job, JobStatus
from jot.plugins.host import PluginCatalog, PluginHost, PluginStatus
from jot.plugins.store import Failure
from jot.server.schemas import (
    AddComment,
    ChangeLabels,
    CreateJot,
    CreateProject,
    EditComment,
    LinkBody,
    MoveRank,
    SetStatus,
    UpdateJot,
    UpdatePlugin,
    UpdateProject,
)

WRITE_ORIGINS = frozenset({Origin.API, Origin.UI})


class LabelCount(BaseModel):
    """A label and how many live jots carry it."""

    name: str
    count: int


class Health(BaseModel):
    """Liveness and data-home details."""

    status: str
    home: str


class Dependencies:
    """Per-request dependencies, resolved by FastAPI from module-level aliases."""

    @staticmethod
    def session(request: Request) -> Iterator[Session]:
        """Open one session per request on the app's data home."""
        home = request.app.state.home
        if not isinstance(home, JotHome):  # pragma: no cover - set by build()
            raise TypeError("app.state.home is not configured")
        with Session.open(home) as session:
            yield session

    @staticmethod
    def host(request: Request, session: Db) -> PluginHost:
        """Return a plugin host bound to the request's session."""
        catalog = request.app.state.catalog
        if not isinstance(catalog, PluginCatalog):  # pragma: no cover - set by build()
            raise TypeError("app.state.catalog is not configured")
        return PluginHost(session, catalog)

    @staticmethod
    def context(
        actor: Annotated[str | None, Header(alias="X-Jot-Actor")] = None,
        origin: Annotated[str | None, Header(alias="X-Jot-Origin")] = None,
        reason: Annotated[str | None, Header(alias="X-Jot-Reason")] = None,
        correlation: Annotated[str | None, Header(alias="X-Jot-Correlation-Id")] = None,
    ) -> WriteContext:
        """Build the write context from request headers (origin: api or ui)."""
        chosen = Origin(origin) if origin in WRITE_ORIGINS else Origin.API
        kwargs = {"correlation_id": correlation} if correlation else {}
        return WriteContext(
            actor=actor or "user", origin=chosen, reason=reason, **kwargs
        )


Db = Annotated[Session, Depends(Dependencies.session)]
Ctx = Annotated[WriteContext, Depends(Dependencies.context)]
Host = Annotated[PluginHost, Depends(Dependencies.host)]


class ApiApplication:
    """Build the FastAPI app for one data home."""

    def __init__(self, home: JotHome, catalog: PluginCatalog | None = None) -> None:
        self.home = home
        self.catalog = catalog if catalog is not None else PluginCatalog.discover()

    def build(self) -> FastAPI:
        """Return the configured application."""
        app = FastAPI(
            title="Jot",
            version="0.2",
            docs_url="/api/docs",
            openapi_url="/api/openapi.json",
        )
        app.add_exception_handler(AppError, self.app_error)  # ty: ignore[invalid-argument-type]
        app.add_exception_handler(sqlite3.Error, self.storage_error)  # ty: ignore[invalid-argument-type]
        app.middleware("http")(self.no_cache)
        app.state.home = self.home
        app.state.catalog = self.catalog
        self._routes(app)
        static = Path(str(files("jot") / "static"))
        app.mount("/", StaticFiles(directory=static, html=True), name="ui")
        return app

    @staticmethod
    async def no_cache(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Make browsers revalidate UI files so an upgrade never runs stale JS."""
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @staticmethod
    async def app_error(_request: Request, error: AppError) -> JSONResponse:
        """Map expected failures to HTTP status codes."""
        status = 422
        if isinstance(error, NotFoundError):
            status = 404
        elif isinstance(error, ConflictError):
            status = 409
        return JSONResponse({"error": str(error)}, status_code=status)

    @staticmethod
    async def storage_error(_request: Request, error: sqlite3.Error) -> JSONResponse:
        """Report database failures without a traceback."""
        return JSONResponse({"error": f"Database error: {error}"}, status_code=500)

    def _routes(self, app: FastAPI) -> None:  # noqa: C901 - route table
        """Register every API route."""

        def commands(session: Session, context: WriteContext) -> Commands:
            return session.commands(context)

        @app.get("/api/health")
        def health() -> Health:
            return Health(status="ok", home=str(self.home.path))

        @app.get("/api/jots")
        def list_jots(  # noqa: PLR0913 - one parameter per filter
            session: Db,
            *,
            profile: Annotated[list[str] | None, Query()] = None,
            status: Annotated[list[str] | None, Query()] = None,
            category: Annotated[list[StatusCategory] | None, Query()] = None,
            project: str | None = None,
            label: Annotated[list[str] | None, Query()] = None,
            q: str | None = None,
            archived: bool = False,
            deleted: bool = False,
            sort: Sort = Sort.RANK,
            limit: Annotated[int | None, Query(ge=0)] = None,
        ) -> list[Jot]:
            return session.queries.jots(
                JotFilter(
                    profiles=profile or [],
                    statuses=status or [],
                    categories=category or [],
                    project=project,
                    labels=label or [],
                    text=q,
                    include_archived=archived,
                    include_deleted=deleted,
                    sort=sort,
                    limit=limit,
                )
            )

        @app.post("/api/jots", status_code=201)
        def create_jot(body: CreateJot, session: Db, context: Ctx) -> Jot:
            return commands(session, context).create(**body.model_dump())

        @app.get("/api/jots/{jot_id}")
        def get_jot(jot_id: int, session: Db) -> JotDetail:
            return session.queries.detail(jot_id)

        @app.patch("/api/jots/{jot_id}")
        def update_jot(jot_id: int, body: UpdateJot, session: Db, context: Ctx) -> Jot:
            return commands(session, context).update(
                jot_id,
                body.patch(),
                expected_version=body.expected_version,
                base_version=body.base_version,
            )

        @app.post("/api/jots/{jot_id}/status")
        def set_status(jot_id: int, body: SetStatus, session: Db, context: Ctx) -> Jot:
            return commands(session, context).set_status(
                jot_id, body.status, expected_version=body.expected_version
            )

        @app.post("/api/jots/{jot_id}/labels")
        def labels(jot_id: int, body: ChangeLabels, session: Db, context: Ctx) -> Jot:
            return commands(session, context).label(
                jot_id,
                add=body.add,
                remove=body.remove,
                expected_version=body.expected_version,
            )

        @app.post("/api/jots/{jot_id}/rank")
        def rank(jot_id: int, body: MoveRank, session: Db, context: Ctx) -> Jot:
            return commands(session, context).move_rank(
                jot_id, body.placement, body.anchor
            )

        @app.post("/api/jots/{jot_id}/archive")
        def archive(jot_id: int, session: Db, context: Ctx) -> Jot:
            return commands(session, context).archive(jot_id)

        @app.post("/api/jots/{jot_id}/unarchive")
        def unarchive(jot_id: int, session: Db, context: Ctx) -> Jot:
            return commands(session, context).archive(jot_id, archived=False)

        @app.delete("/api/jots/{jot_id}")
        def delete(jot_id: int, session: Db, context: Ctx) -> Jot:
            return commands(session, context).delete(jot_id)

        @app.post("/api/jots/{jot_id}/restore")
        def restore(jot_id: int, session: Db, context: Ctx) -> Jot:
            return commands(session, context).delete(jot_id, deleted=False)

        @app.post("/api/jots/{jot_id}/comments", status_code=201)
        def add_comment(
            jot_id: int, body: AddComment, session: Db, context: Ctx
        ) -> Comment:
            return commands(session, context).comment(
                jot_id, body.body, kind=body.kind, reply_to=body.reply_to
            )

        @app.patch("/api/comments/{comment_id}")
        def edit_comment(
            comment_id: int, body: EditComment, session: Db, context: Ctx
        ) -> Comment:
            return commands(session, context).edit_comment(comment_id, body.body)

        @app.delete("/api/comments/{comment_id}", status_code=204)
        def delete_comment(comment_id: int, session: Db, context: Ctx) -> Response:
            commands(session, context).delete_comment(comment_id)
            return Response(status_code=204)

        @app.post("/api/links", status_code=201)
        def link(body: LinkBody, session: Db, context: Ctx) -> Link:
            return commands(session, context).link(body.src, body.dst, body.rel)

        @app.post("/api/links/delete", status_code=204)
        def unlink(body: LinkBody, session: Db, context: Ctx) -> Response:
            commands(session, context).unlink(body.src, body.dst, body.rel)
            return Response(status_code=204)

        @app.get("/api/projects")
        def projects(
            session: Db,
            archived: bool = False,  # noqa: FBT001, FBT002 - query flag
        ) -> list[Project]:
            return session.queries.projects(include_archived=archived)

        @app.post("/api/projects", status_code=201)
        def create_project(body: CreateProject, session: Db, context: Ctx) -> Project:
            return commands(session, context).create_project(
                body.slug, body.name, body.description
            )

        @app.patch("/api/projects/{slug}")
        def update_project(
            slug: str, body: UpdateProject, session: Db, context: Ctx
        ) -> Project:
            return commands(session, context).update_project(
                slug,
                name=body.name,
                description=body.description,
                archived=body.archived,
            )

        @app.get("/api/profiles")
        def profiles(session: Db) -> list[Profile]:
            return session.profiles.all()

        @app.get("/api/labels")
        def label_counts(session: Db) -> list[LabelCount]:
            return [LabelCount(name=n, count=c) for n, c in session.queries.labels()]

        @app.get("/api/events")
        def events(
            session: Db,
            after: int = 0,
            limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        ) -> list[Event]:
            return session.queries.events(after, limit)

        @app.get("/api/plugins")
        def plugins(host: Host) -> list[PluginStatus]:
            return host.statuses()

        @app.get("/api/plugins/{plugin_id}")
        def plugin(plugin_id: str, host: Host) -> PluginStatus:
            return host.status(plugin_id)

        @app.patch("/api/plugins/{plugin_id}")
        def update_plugin(
            plugin_id: str, body: UpdatePlugin, host: Host, context: Ctx
        ) -> PluginStatus:
            if body.settings is not None:
                host.configure(plugin_id, body.settings, context)
            if body.enabled is True:
                host.enable(plugin_id, context, from_start=body.from_start)
            elif body.enabled is False:
                host.disable(plugin_id, context)
            return host.status(plugin_id)

        @app.get("/api/plugins/{plugin_id}/ui.js")
        def plugin_ui(plugin_id: str, host: Host) -> Response:
            source = host.instance(plugin_id).ui_module()
            if source is None:
                raise NotFoundError(f"Plugin {plugin_id!r} has no UI module")
            return Response(source, media_type="text/javascript")

        @app.post("/api/plugins/{plugin_id}/actions/{name}")
        def plugin_action(
            plugin_id: str,
            name: str,
            host: Host,
            context: Ctx,
            body: Annotated[Metadata | None, Body()] = None,
        ) -> JsonValue:
            return host.action(plugin_id, name, body or {}, context)

        @app.get("/api/plugins/{plugin_id}/failures")
        def plugin_failures(plugin_id: str, host: Host) -> list[Failure]:
            host.catalog.get(plugin_id)
            return host.failures(plugin_id)

        @app.get("/api/jobs")
        def jobs(
            host: Host,
            plugin: str | None = None,
            status: Annotated[list[JobStatus] | None, Query()] = None,
            limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        ) -> list[Job]:
            return host.jobs(plugin_id=plugin, statuses=status, limit=limit)

        @app.post("/api/jobs/{job_id}/cancel")
        def cancel_job(job_id: int, host: Host) -> Job:
            return host.cancel(job_id)

        @app.post("/api/jobs/{job_id}/retry")
        def retry_job(job_id: int, host: Host) -> Job:
            return host.retry(job_id)

        @app.exception_handler(ValidationError)
        async def invalid(_request: Request, error: ValidationError) -> JSONResponse:
            return JSONResponse({"error": str(error)}, status_code=422)
