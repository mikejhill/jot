"""Agent and cleanup HTTP adapters with no provider implementation coupling."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body

from jot.agents.base import JsonObject
from jot.core.models import Run, Task
from jot.server.runtime import RuntimeAccess
from jot.server.schemas import (
    Answers,
    ApplyCleanup,
    Approval,
    Comment,
    Enrichment,
    RunRequest,
    Scan,
)
from jot.services.bus import Topic


class ActionRoutes:
    """Thin adapters over the shared enrichment, runner, and cleanup services."""

    def __init__(self, access: RuntimeAccess) -> None:
        self._access = access
        self.router = APIRouter(prefix="/api")
        for path, endpoint in (
            ("tasks/{task_id}/enrich", self.enrich),
            ("tasks/{task_id}/run", self.run),
            ("tasks/{task_id}/approve", self.approve),
            ("tasks/{task_id}/send-back", self.send_back),
            ("tasks/{task_id}/answers", self.answers),
            ("runs/{run_id}/cancel", self.cancel),
            ("cleanup/scan", self.scan),
            ("cleanup/{proposal_id}/apply", self.apply),
        ):
            self.router.add_api_route("/" + path, endpoint, methods=["POST"])
        self.router.add_api_route("/runs", self.runs, methods=["GET"])
        self.router.add_api_route("/runs/{run_id}", self.get_run, methods=["GET"])
        self.router.add_api_route("/runs/{run_id}/log", self.run_log, methods=["GET"])
        self.router.add_api_route(
            "/cleanup/{proposal_id}", self.proposal, methods=["GET"]
        )

    async def enrich(
        self,
        task_id: int,
        body: Annotated[Enrichment | None, Body()] = None,
    ) -> Task:
        """Enrich one task and notify connected clients."""
        body = body or Enrichment()
        result = await self._access.runtime.enrich.enrich(
            task_id, backend=body.backend, model=body.model
        )
        self._access.runtime.changed(task_id)
        return result

    def _run_changed(self, run: Run) -> Run:
        """Publish run and task invalidations even when a service does not."""
        runtime = self._access.runtime
        runtime.bus.publish(
            Topic.RUN, {"id": run.id, "task_id": run.task_id, "status": run.status}
        )
        runtime.changed(run.task_id)
        return run

    async def run(
        self,
        task_id: int,
        body: Annotated[RunRequest | None, Body()] = None,
    ) -> Run:
        """Start a planned or direct run using the selected backend."""
        body = body or RunRequest()
        return self._run_changed(
            await self._access.runtime.runner.start(
                task_id,
                flow=body.flow,
                backend=body.backend,
                model=body.model,
            )
        )

    async def approve(
        self,
        task_id: int,
        body: Annotated[Approval | None, Body()] = None,
    ) -> Run:
        """Approve a plan and request its execution."""
        body = body or Approval()
        return self._run_changed(
            await self._access.runtime.runner.approve(
                task_id,
                note=body.note,
                backend=body.backend,
                model=body.model,
            )
        )

    async def answers(self, task_id: int, body: Answers) -> Run:
        """Save answers and resume the agent phase that asked the questions."""
        return self._run_changed(
            await self._access.runtime.runner.respond(
                task_id,
                {a.question_id: a.text for a in body.answers},
                backend=body.backend,
                model=body.model,
            )
        )

    async def send_back(self, task_id: int, body: Comment) -> Task:
        """Send review feedback through the runner's workflow authority."""
        task = await self._access.runtime.runner.send_back(task_id, body.comment)
        self._access.runtime.changed(task_id)
        return task

    async def runs(self, task_id: int | None = None) -> list[Run]:
        """Return run metadata, optionally restricted to one task."""
        return [
            run
            for run in self._access.runtime.runs.list()
            if task_id is None or run.task_id == task_id
        ]

    async def run_log(self, run_id: int) -> list[JsonObject]:
        """Return a run's stored log lines (text, thinking, tool, usage, result)."""
        runtime = self._access.runtime
        runtime.runs.get(run_id)
        return runtime.runner.log(run_id)

    async def get_run(self, run_id: int) -> Run:
        """Return one persisted run."""
        return self._access.runtime.runs.get(run_id)

    async def cancel(self, run_id: int) -> dict[str, bool]:
        """Cancel a run and invalidate its task and metadata."""
        runtime = self._access.runtime
        await runtime.runner.cancel(run_id)
        self._run_changed(runtime.runs.get(run_id))
        return {"cancelled": True}

    async def scan(self, body: Annotated[Scan | None, Body()] = None) -> dict[str, int]:
        """Create a proposal without applying any item."""
        body = body or Scan()
        cleanup = self._access.runtime.cleanup
        proposal_id = await cleanup.scan(
            use_agent=body.use_agent, backend=body.backend, model=body.model
        )
        return {"id": proposal_id}

    async def proposal(self, proposal_id: int) -> dict[str, object]:
        """Return a reviewable cleanup proposal."""
        return self._access.runtime.cleanup.proposal(proposal_id)

    async def apply(self, proposal_id: int, body: ApplyCleanup) -> dict[str, list[int]]:
        """Apply explicitly approved indexes and invalidate affected tasks."""
        runtime = self._access.runtime
        ids = runtime.cleanup.apply(proposal_id, body.approved_indexes)
        for task_id in ids:
            runtime.changed(task_id)
        return {"task_ids": ids}
