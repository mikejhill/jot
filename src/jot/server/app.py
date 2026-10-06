"""FastAPI composition, application lifetime, and clean HTTP error translation."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from jot.config import JotHome
from jot.db.connection import Database
from jot.exceptions import (
    AppError,
    NotFoundError,
    NotImplementedAppError,
    WorkflowError,
)
from jot.server.actions import ActionRoutes
from jot.server.resources import ResourceRoutes
from jot.server.runtime import Runtime, RuntimeAccess
from jot.server.tasks import TaskRoutes


class Application:
    """Own a single server's services and local static application."""

    def __init__(self, home: JotHome | None = None) -> None:
        self._home = home or JotHome.resolve()
        self._access = RuntimeAccess()

    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncIterator[None]:
        """Open SQLite on the event loop and stop background work before closing it."""
        config = self._home.initialize()
        with Database.open(self._home.database) as db:
            runtime = Runtime(db, self._home, config)
            self._access.current = runtime
            app.state.runtime = runtime
            worker = asyncio.create_task(runtime.work())
            try:
                yield
            finally:
                worker.cancel()
                try:
                    with suppress(asyncio.CancelledError):
                        await worker
                    await runtime.runner.shutdown()
                finally:
                    self._access.current = None

    @staticmethod
    async def error(_request: Request, error: Exception) -> JSONResponse:
        """Translate expected failures into stable, human-readable JSON."""
        code = 400
        if isinstance(error, NotFoundError):
            code = 404
        elif isinstance(error, NotImplementedAppError):
            code = 501
        elif isinstance(error, WorkflowError):
            code = 409
        elif isinstance(error, ValidationError):
            code = 422
        return JSONResponse({"detail": str(error)}, status_code=code)

    def build(self) -> FastAPI:
        """Register async routes and mount only the packaged static directory."""
        app = FastAPI(
            title="Jot", lifespan=self.lifespan, docs_url=None, redoc_url=None
        )
        for error_type in (AppError, ValidationError, sqlite3.Error, OSError):
            app.add_exception_handler(error_type, self.error)
        for routes in (
            TaskRoutes(self._access),
            ActionRoutes(self._access),
            ResourceRoutes(self._access),
        ):
            app.include_router(routes.router)
        app.mount(
            "/", StaticFiles(directory=Path(__file__).parents[1] / "static", html=True)
        )
        return app


def create_app(home: JotHome | None = None) -> FastAPI:
    """Create the public ASGI factory requested by uvicorn and embedding callers."""
    return Application(home).build()
