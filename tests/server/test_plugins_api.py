"""Plugin and job routes."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from tests.plugins.conftest import Broken, Queuer, Recorder
from tests.server.test_api import items, ok

from jot.app.session import Session
from jot.config import JotHome
from jot.plugins.context import PluginContext
from jot.plugins.host import PluginCatalog, PluginHost
from jot.plugins.rules import RulesPlugin
from jot.server.app import ApiApplication

CATALOG = PluginCatalog.of(RulesPlugin, Recorder, Queuer, Broken)


@pytest.fixture
def client(home: JotHome) -> Iterator[TestClient]:
    """A client whose server knows the test plugins."""
    with TestClient(ApiApplication(home, CATALOG).build()) as test_client:
        yield test_client


def tick(home: JotHome) -> None:
    """Run one host pass, as the worker would."""
    with Session.open(home) as session:
        PluginHost(session, CATALOG).tick()


class TestPluginRoutes:
    """List, enable, configure, UI modules, and failures."""

    def test_list_and_get(self, client: TestClient) -> None:
        """Every plugin is listed; one can be fetched."""
        listed = {p["id"]: p for p in items(client.get("/api/plugins"))}
        assert set(listed) == {"broken", "queuer", "recorder", "rules"}
        assert listed["broken"]["error"]
        assert listed["rules"]["has_ui"] is True
        assert ok(client.get("/api/plugins/rules"))["name"] == "Rules"
        ok(client.get("/api/plugins/nope"), 404)

    def test_enable_configure_and_run(self, client: TestClient, home: JotHome) -> None:
        """Rules configured over HTTP apply to jots created over HTTP."""
        headers = {"X-Jot-Actor": "mike", "X-Jot-Origin": "ui"}
        rules = {
            "rules": [
                {
                    "name": "tag",
                    "when": {"text": "bug"},
                    "then": {"add_labels": ["bug"]},
                }
            ]
        }
        status = ok(
            client.patch(
                "/api/plugins/rules",
                json={"enabled": True, "settings": rules},
                headers=headers,
            )
        )
        assert status["enabled"] is True
        assert status["settings"]["rules"][0]["name"] == "tag"  # ty: ignore[not-subscriptable]
        ok(client.post("/api/jots", json={"title": "a bug"}), 201)
        tick(home)
        assert ok(client.get("/api/jots/1"))["jot"]["labels"] == ["bug"]  # ty: ignore[not-subscriptable]
        events = items(client.get("/api/events"))
        enabled = next(e for e in events if e["kind"] == "plugin.enabled")
        assert (enabled["actor"], enabled["origin"]) == ("mike", "ui")
        off = ok(client.patch("/api/plugins/rules", json={"enabled": False}))
        assert off["enabled"] is False

    def test_errors(self, client: TestClient) -> None:
        """Invalid settings, broken plugins, and missing UI modules are refused."""
        bad = client.patch("/api/plugins/rules", json={"settings": {"rules": 1}})
        assert bad.status_code == 422
        assert "Invalid settings" in bad.json()["error"]
        assert (
            client.patch("/api/plugins/broken", json={"enabled": True}).status_code
            == 422
        )
        assert client.get("/api/plugins/recorder/ui.js").status_code == 404
        assert client.get("/api/plugins/broken/ui.js").status_code == 422
        assert client.patch("/api/plugins/x", json={"enabled": True}).status_code == 404

    def test_ui_module(self, client: TestClient) -> None:
        """UI modules are served as JavaScript."""
        response = client.get("/api/plugins/rules/ui.js")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/javascript")
        assert "export default function setup" in response.text

    def test_actions(self, client: TestClient) -> None:
        """Actions reach enabled plugins; errors map to HTTP statuses."""
        ok(client.post("/api/plugins/recorder/actions/x", json={}), 422)
        client.patch("/api/plugins/recorder", json={"enabled": True})
        error = ok(client.post("/api/plugins/recorder/actions/x"), 404)
        assert "has no action" in str(error["error"])
        ok(client.post("/api/plugins/nope/actions/x"), 404)

    def test_failures(self, client: TestClient, home: JotHome) -> None:
        """Skipped events are listed per plugin."""
        assert items(client.get("/api/plugins/recorder/failures")) == []
        with Session.open(home) as session, session.db.write():
            PluginHost(session, CATALOG).store.add_failure("recorder", 7, 5, "boom")
        failures = items(client.get("/api/plugins/recorder/failures"))
        assert [(f["event_id"], f["error"]) for f in failures] == [(7, "boom")]
        ok(client.get("/api/plugins/nope/failures"), 404)


class TestJobRoutes:
    """List, cancel, and retry jobs."""

    def test_lifecycle(self, client: TestClient, home: JotHome) -> None:
        """Jobs from a plugin can be listed, cancelled, and retried."""
        ok(client.patch("/api/plugins/queuer", json={"enabled": True}))
        ok(client.post("/api/jots", json={"title": "a"}), 201)
        with Session.open(home) as session:
            host = PluginHost(session, CATALOG)
            host.deliver()
            PluginContext(
                session, host.store, "queuer", host.settings("queuer"), clock=host.clock
            ).enqueue("work", {"mode": "ok"})
        queued = items(client.get("/api/jobs?status=queued&plugin=queuer"))
        assert len(queued) == 2
        last = queued[0]["id"]
        assert ok(client.post(f"/api/jobs/{last}/cancel"))["status"] == "cancelled"
        assert client.post(f"/api/jobs/{last}/cancel").status_code == 409
        assert ok(client.post(f"/api/jobs/{last}/retry"))["status"] == "queued"
        tick(home)
        done = items(client.get("/api/jobs?status=done"))
        assert len(done) == 2
        assert client.post("/api/jobs/99/retry").status_code == 404
        assert items(client.get("/api/jobs?limit=1"))[0]["id"] == last
