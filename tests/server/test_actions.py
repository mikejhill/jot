"""Provider-free verification of run, enrichment, and cleanup HTTP adapters."""

from __future__ import annotations

from typing import override

import pytest
from fastapi.testclient import TestClient

from jot.config import JotHome
from jot.core.models import Clock, EventKind, Flow, Run, Task
from jot.db.repository import RunRepository, TaskRepository
from jot.exceptions import AppError, NotImplementedAppError
from jot.server import create_app
from jot.services.cleanup import CleanupService
from jot.services.enrich import EnrichService
from jot.services.runner import RunService


class FakeRunner(RunService):
    """Persist observable fake runs without spawning an agent."""

    @override
    async def start(
        self,
        task_id: int,
        *,
        flow: Flow | None = None,
        backend: str | None = None,
        model: str | None = None,
    ) -> Run:
        """Record backend, model, and requested run phase."""
        return RunRepository(self.db).create(
            Run(
                task_id=task_id,
                backend=backend or "claude",
                model=model,
                phase="execute" if flow == Flow.DIRECT else "plan",
            )
        )

    @override
    async def approve(
        self,
        task_id: int,
        note: str | None = None,
        backend: str | None = None,
        model: str | None = None,
    ) -> Run:
        """Record the approval note then produce an execute run."""
        TaskRepository(self.db).audit(task_id, EventKind.APPROVAL, {"note": note})
        return await self.start(task_id, flow=Flow.DIRECT, backend=backend, model=model)

    @override
    async def send_back(self, task_id: int, comment: str) -> Task:
        """Record feedback on the existing task."""
        tasks = TaskRepository(self.db)
        tasks.audit(task_id, EventKind.COMMENT, {"text": comment})
        return tasks.get(task_id)

    @override
    async def cancel(self, run_id: int) -> None:
        """Persist cancellation so the HTTP readback can verify it."""
        RunRepository(self.db).update(
            run_id, {"status": "cancelled", "ended_at": Clock.now()}
        )


class FakeCleanup(CleanupService):
    """Return a fixed review proposal and apply only requested item indexes."""

    @override
    async def scan(
        self,
        *,
        use_agent: bool = False,
        backend: str | None = None,
        model: str | None = None,
    ) -> int:
        """Expose the selected scan policy through a deterministic proposal id."""
        del backend, model
        return 2 if use_agent else 1

    @override
    def proposal(self, proposal_id: int) -> dict[str, object]:
        """Describe the task represented by each approved index."""
        return {
            "id": proposal_id,
            "status": "pending",
            "items": [
                {
                    "index": 0,
                    "task_id": 1,
                    "title": "Stale",
                    "action": "delete",
                    "reason": "Old note",
                }
            ],
        }

    @override
    def apply(self, proposal_id: int, approved: list[int]) -> list[int]:
        """Delete the first task only when index zero is explicitly approved."""
        if proposal_id == 1 and 0 in approved:
            TaskRepository(self.db).delete(1)
            return [1]
        return []


class ProviderBoundary:
    """Deterministic replacements for enrichment and expected service failures."""

    @staticmethod
    async def enrich(
        service: EnrichService,
        task_id: int,
        backend: str | None = None,
        model: str | None = None,
    ) -> Task:
        """Store the backend as a title to prove the override reached the service."""
        return TaskRepository(service.db).update(
            task_id,
            {"title": model or backend or "Enriched", "needs_enrichment": False},
        )

    @staticmethod
    async def unavailable(
        _service: RunService,
        task_id: int,
        *,
        flow: Flow | None = None,
        backend: str | None = None,
        model: str | None = None,
    ) -> Run:
        """Model an implementation that has not landed yet."""
        del task_id, flow, backend, model
        raise NotImplementedAppError("runs are not implemented yet")

    @staticmethod
    async def rejected(_service: RunService, task_id: int, comment: str) -> Task:
        """Model a user-facing runner validation failure."""
        del task_id, comment
        raise AppError("No plan to send back")


class TestActions:
    """Verify adapters with monkeypatched service boundaries."""

    def test_runs_and_enrichment(
        self, home: JotHome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Run overrides, approval notes, cancellation, and enrichment round-trip."""
        monkeypatch.setattr("jot.server.runtime.RunService", FakeRunner)
        monkeypatch.setattr(EnrichService, "enrich", ProviderBoundary.enrich)
        with TestClient(create_app(home)) as client:
            task_id = client.post("/api/tasks", json={"text": "Build it"}).json()["id"]
            base = f"/api/tasks/{task_id}"
            first = client.post(
                base + "/run",
                json={"flow": "direct", "backend": "codex", "model": "gpt-6-astra"},
            ).json()
            assert first["phase"] == "execute"
            assert first["backend"] == "codex"
            assert first["model"] == "gpt-6-astra"
            assert client.post(base + "/run").json()["phase"] == "plan"
            approved = client.post(
                base + "/approve",
                json={"note": "Ship it", "backend": "claude", "model": "sonnet"},
            ).json()
            assert approved["backend"] == "claude"
            assert approved["model"] == "sonnet"
            assert client.post(base + "/approve").status_code == 200
            assert (
                client.post(
                    base + "/send-back", json={"comment": "Add tests"}
                ).status_code
                == 200
            )
            assert client.get("/api/runs").status_code == 200
            assert len(client.get(f"/api/runs?task_id={task_id}").json()) == 4
            assert client.get("/api/runs?task_id=999").json() == []
            assert client.get(f"/api/runs/{first['id']}").json()["phase"] == "execute"
            assert client.post(f"/api/runs/{first['id']}/cancel").json() == {
                "cancelled": True
            }
            assert (
                client.get(f"/api/runs/{first['id']}").json()["status"] == "cancelled"
            )
            assert (
                client.post(base + "/enrich", json={"backend": "codex"}).json()["title"]
                == "codex"
            )
            assert client.post(base + "/enrich").json()["needs_enrichment"] is False
            assert (
                client.post(base + "/run", json={"backend": "unknown"}).status_code
                == 422
            )
            events = client.get(base).json()["events"]
            assert any(e["body"].get("note") == "Ship it" for e in events)

    def test_service_errors(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both reserved implementations and domain errors become clean JSON."""
        monkeypatch.setattr(RunService, "start", ProviderBoundary.unavailable)
        monkeypatch.setattr(RunService, "send_back", ProviderBoundary.rejected)
        response = client.post("/api/tasks/1/run", json={})
        assert response.status_code == 501
        assert response.json() == {"detail": "runs are not implemented yet"}
        response = client.post("/api/tasks/1/send-back", json={"comment": "x"})
        assert response.status_code == 400
        assert response.json() == {"detail": "No plan to send back"}

    def test_cleanup_approval(
        self, home: JotHome, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Scanning changes nothing and applying affects only approved items."""
        monkeypatch.setattr("jot.server.runtime.CleanupService", FakeCleanup)
        with TestClient(create_app(home)) as client:
            client.post("/api/tasks", json={"text": "Stale"})
            assert client.post("/api/cleanup/scan").json() == {"id": 1}
            assert client.post(
                "/api/cleanup/scan", json={"use_agent": True}
            ).json() == {"id": 2}
            assert (
                client.get("/api/cleanup/1").json()["items"][0]["reason"] == "Old note"
            )
            assert client.post(
                "/api/cleanup/1/apply", json={"approved_indexes": []}
            ).json() == {"task_ids": []}
            assert len(client.get("/api/tasks").json()) == 1
            assert client.post(
                "/api/cleanup/1/apply", json={"approved_indexes": [0]}
            ).json() == {"task_ids": [1]}
            assert client.get("/api/tasks").json() == []
