"""Project, label, configuration, and path-contained instruction endpoints."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter
from starlette.responses import StreamingResponse

from jot.agents.registry import MODEL_SUGGESTIONS, BackendRegistry
from jot.config import ACTIONS
from jot.core.models import Project
from jot.exceptions import NotFoundError, RepositoryError
from jot.server.runtime import EventStream, RuntimeAccess
from jot.server.schemas import Markdown


class ResourceRoutes:
    """Manage supporting local resources through explicit, bounded operations."""

    def __init__(self, access: RuntimeAccess) -> None:
        self._access = access
        self.router = APIRouter(prefix="/api")
        self.router.add_api_route("/projects", self.projects, methods=["GET"])
        self.router.add_api_route(
            "/projects", self.create_project, methods=["POST"], status_code=201
        )
        self.router.add_api_route(
            "/projects/{project_id}", self.project, methods=["GET"]
        )
        self.router.add_api_route(
            "/projects/{project_id}", self.patch_project, methods=["PATCH"]
        )
        self.router.add_api_route(
            "/projects/{project_id}", self.delete_project, methods=["DELETE"]
        )
        self.router.add_api_route("/labels", self.labels, methods=["GET"])
        self.router.add_api_route("/instructions", self.instructions, methods=["GET"])
        self.router.add_api_route(
            "/instructions/{name:path}", self.instruction, methods=["GET"]
        )
        self.router.add_api_route(
            "/instructions/{name:path}", self.save_instruction, methods=["PUT"]
        )
        self.router.add_api_route("/config", self.config, methods=["GET"])
        self.router.add_api_route("/stream", self.stream, methods=["GET"])

    async def projects(self) -> list[Project]:
        """List project defaults, including archived projects."""
        return self._access.runtime.projects.list()

    async def create_project(self, body: Project) -> Project:
        """Create a validated project."""
        return self._access.runtime.projects.create(body)

    async def project(self, project_id: int) -> Project:
        """Return one project."""
        return self._access.runtime.projects.get(project_id)

    async def patch_project(self, project_id: int, body: dict[str, object]) -> Project:
        """Validate a partial update against the complete project model."""
        return self._access.runtime.projects.update(project_id, body)

    async def delete_project(self, project_id: int) -> dict[str, bool]:
        """Delete a project subject to repository foreign-key constraints."""
        self._access.runtime.projects.delete(project_id)
        return {"deleted": True}

    async def labels(self) -> list[dict[str, str | int]]:
        """Count labels on live tasks only."""
        counts = Counter(
            label for task in self._access.runtime.tasks.list() for label in task.labels
        )
        return [
            {"name": name, "count": count} for name, count in sorted(counts.items())
        ]

    def _path(self, name: str) -> Path:
        """Reject traversal, device names, and escaping symlinks."""
        if not re.fullmatch(r"(?:[a-z0-9_-]+|projects/[a-z0-9_-]+)\.md", name):
            raise RepositoryError("Invalid instruction path")
        stem = name.rsplit("/", 1)[-1].removesuffix(".md").upper()
        if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(
            r"(?:COM|LPT)[0-9]", stem
        ):
            raise RepositoryError("Invalid instruction filename")
        root = (self._access.runtime.home.path / "instructions").resolve()
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise RepositoryError("Instruction path escapes instructions directory")
        return path

    async def instructions(self) -> list[str]:
        """List only valid Markdown instruction files."""
        root = self._access.runtime.home.path / "instructions"
        names: list[str] = []
        for path in sorted(root.rglob("*.md")):
            name = path.relative_to(root).as_posix()
            try:
                self._path(name)
            except RepositoryError:
                continue
            names.append(name)
        return names

    async def instruction(self, name: str) -> Markdown:
        """Read an existing instruction file as UTF-8 Markdown."""
        path = self._path(name)
        if not path.is_file():
            raise NotFoundError(f"Instruction {name!r} not found")
        return Markdown(content=path.read_text(encoding="utf-8"))

    async def save_instruction(self, name: str, body: Markdown) -> Markdown:
        """Save one validated Markdown path without touching other files."""
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.content, encoding="utf-8")
        return body

    async def config(self) -> dict[str, object]:
        """Expose read-only defaults and supported backend choices."""
        config = self._access.runtime.config
        backends = BackendRegistry.names()
        defaults = {
            name: {action: config.model_for(name, action) for action in ACTIONS}
            for name in backends
        }
        return asdict(config) | {
            "backends": backends,
            "model_defaults": defaults,
            "model_suggestions": MODEL_SUGGESTIONS,
        }

    async def stream(self) -> StreamingResponse:
        """Open a live event stream with proxy buffering disabled."""
        return StreamingResponse(
            EventStream(self._access.runtime.bus).messages(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
