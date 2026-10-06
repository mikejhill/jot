"""HTTP capture, full-text filtering, lifecycle, and content validation tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class TestTasks:
    """Exercise real task repositories through the HTTP boundary."""

    def test_capture_edit_move_comment_delete(self, client: TestClient) -> None:
        """Capture remains pending and all mutations preserve an audit history."""
        response = client.post("/api/tasks", json={"text": "Improve health checks"})
        assert response.status_code == 201
        task = response.json()
        task_id = task["id"]
        url = f"/api/tasks/{task_id}"
        assert task["status"] == "inbox"
        assert task["source"] == "ui"
        assert task["needs_enrichment"] is True
        assert task["raw_input"] == "Improve health checks"

        assert (
            client.patch(
                url, json={"title": "Holistic health", "labels": ["ops", "ux"]}
            ).status_code
            == 200
        )
        assert client.patch(url, json={"labels": ["ops"]}).json()["labels"] == ["ops"]
        assert client.post(url + "/move", json={"status": "done"}).status_code == 409
        assert (
            client.post(url + "/move", json={"status": "ready"}).json()["status"]
            == "ready"
        )
        assert (
            client.post(
                url + "/comment", json={"comment": "Include dependencies"}
            ).status_code
            == 200
        )
        detail = client.get(url).json()
        assert detail["task"]["title"] == "Holistic health"
        assert {e["kind"] for e in detail["events"]} >= {
            "created",
            "enriched",
            "status",
            "comment",
        }
        assert "planning" in detail["transitions"]
        assert client.delete(url).json() == {"deleted": True}
        assert client.get("/api/tasks").json() == []
        assert client.get("/api/tasks?include_deleted=true").json()[0]["deleted_at"]
        assert client.get(url).json()["events"]

    def test_filters_search_sort_and_prefilled(self, client: TestClient) -> None:
        """Combine repository filters and type before applying a sorted limit."""
        client.post("/api/projects", json={"slug": "orbit", "name": "Orbit"})
        low = client.post(
            "/api/tasks", json={"text": "Documentation", "criticality": "low"}
        ).json()
        high = client.post(
            "/api/tasks",
            json={
                "text": "Holistic telemetry",
                "project": "orbit",
                "labels": ["ops", "health"],
                "criticality": "critical",
                "type": "bug",
                "description": "dependency probes",
                "flow": "direct",
                "repo_path": "C:/repo",
                "due_at": "2026-12-01T00:00:00Z",
            },
        ).json()
        for params in (
            {"project": "orbit"},
            {"labels": ["ops", "health"]},
            {"criticality": "critical"},
            {"type": "bug", "limit": 1},
            {"q": "telemetry"},
            {"q": "probes"},
            {"q": "health"},
            {"sort": "priority", "limit": 1},
            {"sort": "created", "limit": 1},
            {"sort": "updated", "limit": 1},
        ):
            assert client.get("/api/tasks", params=params).json()[0]["id"] == high["id"]
        client.post(f"/api/tasks/{high['id']}/move", json={"status": "ready"})
        assert client.get("/api/tasks?status=ready").json()[0]["id"] == high["id"]
        assert client.get("/api/tasks?statuses=inbox").json()[0]["id"] == low["id"]
        assert client.get("/api/tasks?older_than=100&age_field=created_at").json() == []
        assert client.get("/api/tasks?limit=0").json() == []
        assert client.get("/api/tasks?order=-id").json()[0]["id"] == high["id"]

    @pytest.mark.parametrize(
        "query", ["q=%22", "order=drop", "older_than=-1", "sort=nope", "type=nope"]
    )
    def test_bad_filters(self, client: TestClient, query: str) -> None:
        """Malformed FTS and invalid filter vocabulary become client errors."""
        assert client.get("/api/tasks?" + query).status_code in {400, 422}

    @pytest.mark.parametrize(
        "body",
        [
            {"text": ""},
            {"text": "   "},
            {"text": "x", "status": "done"},
            {"text": "x", "due_at": "2026-12-01"},
        ],
    )
    def test_invalid_capture(self, client: TestClient, body: dict[str, str]) -> None:
        """Reject empty notes, state injection, and ambiguous due timestamps."""
        assert client.post("/api/tasks", json=body).status_code in {400, 422}

    def test_bad_patch_is_atomic(self, client: TestClient) -> None:
        """Invalid content and label updates cannot partially modify a task."""
        task = client.post("/api/tasks", json={"text": "Original"}).json()
        url = f"/api/tasks/{task['id']}"
        assert (
            client.patch(url, json={"title": "Changed", "labels": [""]}).status_code
            == 400
        )
        assert client.get(url).json()["task"]["title"] == "Original"
        assert client.patch(url, json={"title": None}).status_code == 422
        assert client.patch(url, json={"status": "done"}).status_code == 422
        assert client.patch(url, json={"project": "missing"}).status_code == 404
        assert (
            client.patch(url, json={"project": None, "project_id": None}).status_code
            == 400
        )
        assert client.patch(url, json={"project_id": 9999}).status_code == 400
        assert client.get("/api/tasks/9999").status_code == 404
        assert (
            client.post("/api/tasks/9999/comment", json={"comment": "x"}).status_code
            == 404
        )
