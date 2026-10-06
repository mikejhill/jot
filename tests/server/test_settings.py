"""Live settings edits, fake discovery, and capture selection over HTTP."""

from __future__ import annotations

from fastapi.testclient import TestClient


class TestSettingsAPI:
    """Exercise validation and effective settings with real persistence."""

    def test_round_trip_and_discover(self, client: TestClient) -> None:
        """Saving a second instance changes action resolution without restarting."""
        response = client.get("/api/settings")
        assert response.status_code == 200
        settings = response.json()
        assert settings["harnesses"]["claude"]["kind"] == "claude-sdk"
        assert "model_suggestions" in settings
        body = {
            "harnesses": {
                "test": {
                    "kind": "fake",
                    "label": "Offline",
                    "models": {
                        "allowed": ["tiny", "default"],
                        "default": {"triage": "tiny"},
                    },
                }
            },
            "actions": {"triage": "test"},
            "pins": [
                {
                    "label": "Tiny",
                    "harness": "test",
                    "model": "tiny",
                    "actions": ["triage"],
                }
            ],
        }
        saved = client.put("/api/settings", json=body)
        assert saved.status_code == 200, saved.text
        assert client.get("/api/settings").json()["actions"]["triage"] == "test"
        assert client.post("/api/settings/harnesses/test/discover").json() == {
            "skills": [],
            "mcp": [],
            "plugins": [],
        }
        assert (
            client.post("/api/settings/harnesses/missing/discover").status_code == 400
        )
        captured = client.post(
            "/api/tasks", json={"text": "Task", "harness": "test", "model": "tiny"}
        )
        assert captured.status_code == 201
        events = client.get(f"/api/tasks/{captured.json()['id']}").json()["events"]
        assert events[0]["body"]["harness"] == "test"
        assert events[0]["body"]["model"] == "tiny"
        assert (
            client.post(
                "/api/tasks",
                json={"text": "Rejected", "harness": "test", "model": "blocked"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/tasks", json={"text": "Auto", "harness": "auto"}
            ).status_code
            == 201
        )

    def test_invalid_save_is_atomic(self, client: TestClient) -> None:
        """Unknown references and malformed fields leave effective settings intact."""
        before = client.get("/api/settings").json()
        for body in (
            {"actions": {"plan": "missing"}},
            {"harnesses": {"x": {"kind": "oops"}}},
            {"actions": {"typo": "claude"}},
            {"projects": {}},
        ):
            assert client.put("/api/settings", json=body).status_code == 400
            assert client.get("/api/settings").json() == before
