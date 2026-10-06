"""Resource, path containment, local assets, and read-only configuration tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class TestResources:
    """Exercise supporting resources with real storage."""

    def test_projects_and_labels(self, client: TestClient) -> None:
        """Create, update, list, archive, and delete projects; count live labels."""
        body = {
            "slug": "test",
            "name": "Test",
            "repo_path": "C:/test",
            "aliases": ["T"],
            "priority": 2,
            "default_flow": "direct",
        }
        response = client.post("/api/projects", json=body)
        assert response.status_code == 201
        project_id = response.json()["id"]
        url = f"/api/projects/{project_id}"
        assert client.get(url).json()["aliases"] == ["T"]
        assert client.get("/api/projects").json()[0]["default_flow"] == "direct"
        assert (
            client.patch(url, json={"name": "Renamed", "archived": True}).json()[
                "archived"
            ]
            is True
        )
        assert client.patch(url, json={"priority": -1}).status_code == 422
        assert client.patch(url, json={"id": 99}).status_code == 400
        assert client.post("/api/projects", json=body).status_code == 400
        assert client.delete(url).json() == {"deleted": True}
        assert client.get(url).status_code == 404
        first = client.post(
            "/api/tasks", json={"text": "One", "labels": ["ops"]}
        ).json()
        client.post("/api/tasks", json={"text": "Two", "labels": ["ops"]})
        assert client.get("/api/labels").json() == [{"name": "ops", "count": 2}]
        client.delete(f"/api/tasks/{first['id']}")
        assert client.get("/api/labels").json() == [{"name": "ops", "count": 1}]

    def test_instructions(self, client: TestClient) -> None:
        """Default and project Markdown files round-trip as plain UTF-8."""
        assert set(client.get("/api/instructions").json()) == {
            "triage.md",
            "drawdown.md",
            "cleanup.md",
        }
        assert "Triage" in client.get("/api/instructions/triage.md").json()["content"]
        for path in ("triage.md", "projects/orbit.md"):
            url = "/api/instructions/" + path
            assert (
                client.put(url, json={"content": "# Notes\n日本語"}).status_code == 200
            )
            assert client.get(url).json() == {"content": "# Notes\n日本語"}
        assert "projects/orbit.md" in client.get("/api/instructions").json()
        assert client.get("/api/instructions/missing.md").status_code == 404

    @pytest.mark.parametrize(
        "path",
        [
            "%2e%2e%2fconfig.toml",
            "projects%2f..%2f..%2fescape.md",
            "..%5cescape.md",
            "C:%5cescape.md",
            "projects/x/y.md",
            "con.md",
            "projects/lpt1.md",
            "x.md:stream",
            "%2fescape.md",
        ],
    )
    def test_instruction_traversal(self, client: TestClient, path: str) -> None:
        """Reject encoded traversal, Windows alternate paths, and device names."""
        assert client.put(
            "/api/instructions/" + path, json={"content": "bad"}
        ).status_code in {400, 404}

    def test_config_and_static(self, client: TestClient) -> None:
        """UI and vendor modules load locally and settings cannot be changed."""
        config = client.get("/api/config").json()
        assert config["backends"] == ["claude", "codex", "copilot"]
        assert config["server"] == {"host": "127.0.0.1", "port": 8765}
        assert config["model_defaults"]["claude"]["plan"] is None
        assert "opus" in config["model_suggestions"]["claude"]
        assert client.put("/api/config", json={}).status_code == 405
        for path in (
            "/",
            "/app.js",
            "/styles.css",
            "/vendor/preact.mjs",
            "/vendor/htm.mjs",
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert "https://" not in response.text
        assert "Capture" in client.get("/app.js").text
        assert client.get("/config.toml").status_code == 404
