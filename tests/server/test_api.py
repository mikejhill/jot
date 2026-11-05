"""HTTP API over the command layer."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from jot.config import JotHome
from jot.server.app import ApiApplication

type Json = dict[str, object]

JSON_OBJECT = TypeAdapter(dict[str, object])
JSON_LIST = TypeAdapter(list[dict[str, object]])


@pytest.fixture
def client(home: JotHome) -> Iterator[TestClient]:
    """A client for a fresh data home."""
    with TestClient(ApiApplication(home).build()) as test_client:
        yield test_client


def ok(response: object, status: int = 200) -> Json:
    """Assert a status and return the JSON body."""
    from httpx import Response  # noqa: PLC0415

    assert isinstance(response, Response)
    assert response.status_code == status, response.text
    return JSON_OBJECT.validate_python(response.json())


def items(response: object) -> list[Json]:
    """Assert 200 and return a JSON list."""
    from httpx import Response  # noqa: PLC0415

    assert isinstance(response, Response)
    assert response.status_code == 200, response.text
    return JSON_LIST.validate_python(response.json())


class TestJots:
    """Create, read, update, and list."""

    def test_lifecycle(self, client: TestClient) -> None:
        """Every jot route works end to end and records the UI origin."""
        ok(client.post("/api/projects", json={"slug": "p", "name": "P"}), 201)
        created = ok(
            client.post(
                "/api/jots",
                json={"title": "Sales model", "labels": ["sales"], "project": "p"},
                headers={"X-Jot-Origin": "ui", "X-Jot-Actor": "mike"},
            ),
            201,
        )
        jot_id = created["id"]
        assert created["created_by"] == "mike"
        updated = ok(
            client.patch(
                f"/api/jots/{jot_id}",
                json={"body": "details", "expected_version": 1},
                headers={"X-Jot-Reason": "detail", "X-Jot-Correlation-Id": "c1"},
            )
        )
        assert (updated["body"], updated["title"], updated["version"]) == (
            "details",
            "Sales model",
            2,
        )
        moved = ok(client.post(f"/api/jots/{jot_id}/status", json={"status": "done"}))
        assert moved["category"] == "done"
        labeled = ok(
            client.post(
                f"/api/jots/{jot_id}/labels", json={"add": ["x"], "remove": ["sales"]}
            )
        )
        assert labeled["labels"] == ["x"]
        second = ok(client.post("/api/jots", json={"title": "Second"}), 201)
        ranked = ok(
            client.post(f"/api/jots/{second['id']}/rank", json={"placement": "top"})
        )
        assert [j["id"] for j in items(client.get("/api/jots"))] == [
            ranked["id"],
            jot_id,
        ]
        assert ok(client.post(f"/api/jots/{jot_id}/archive"))["archived_at"]
        assert ok(client.post(f"/api/jots/{jot_id}/unarchive"))["archived_at"] is None
        assert ok(client.delete(f"/api/jots/{jot_id}"))["deleted_at"]
        assert ok(client.post(f"/api/jots/{jot_id}/restore"))["deleted_at"] is None
        detail = ok(client.get(f"/api/jots/{jot_id}"))
        history = detail["history"]
        assert isinstance(history, list)
        assert history[0]["origin"] == "ui"
        assert history[1]["reason"] == "detail"
        assert history[1]["correlation_id"] == "c1"
        assert history[1]["origin"] == "api"

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("", ["a", "b"]),
            ("?category=active", ["b"]),
            ("?profile=task", ["b"]),
            ("?status=open", ["a"]),
            ("?label=l", ["a"]),
            ("?project=p", ["b"]),
            ("?q=alph", ["a"]),
            ("?archived=true", ["a", "b", "c"]),
            ("?deleted=true", ["a", "b", "d"]),
            ("?sort=created&limit=1", ["b"]),
        ],
    )
    def test_filters(self, client: TestClient, query: str, expected: list[str]) -> None:
        """Query parameters map to list filters."""
        client.post("/api/projects", json={"slug": "p", "name": "P"})
        client.post("/api/jots", json={"title": "a", "body": "alpha", "labels": ["l"]})
        client.post(
            "/api/jots",
            json={"title": "b", "profile": "task", "status": "doing", "project": "p"},
        )
        archived = ok(client.post("/api/jots", json={"title": "c"}), 201)
        client.post(f"/api/jots/{archived['id']}/archive")
        deleted = ok(client.post("/api/jots", json={"title": "d"}), 201)
        client.delete(f"/api/jots/{deleted['id']}")
        found = items(client.get(f"/api/jots{query}"))
        assert [j["title"] for j in found] == expected

    def test_base_version_merge(self, client: TestClient) -> None:
        """A late writer merges disjoint fields and conflicts on the same one."""
        jot = ok(client.post("/api/jots", json={"title": "a"}), 201)
        client.patch(f"/api/jots/{jot['id']}", json={"title": "user"})
        merged = client.patch(
            f"/api/jots/{jot['id']}",
            json={"metadata": {"triage": {"ok": True}}, "base_version": 1},
        )
        assert ok(merged)["title"] == "user"
        clash = client.patch(
            f"/api/jots/{jot['id']}", json={"title": "late", "base_version": 1}
        )
        assert clash.status_code == 409


class TestCommentsLinksCatalog:
    """Comments, links, projects, profiles, labels, events, health."""

    def test_comments_and_links(self, client: TestClient) -> None:
        """Comments are edited and deleted; links are created and removed."""
        a = ok(client.post("/api/jots", json={"title": "a"}), 201)
        b = ok(client.post("/api/jots", json={"title": "b"}), 201)
        comment = ok(
            client.post(f"/api/jots/{a['id']}/comments", json={"body": "hi"}), 201
        )
        edited = ok(client.patch(f"/api/comments/{comment['id']}", json={"body": "yo"}))
        assert edited["body"] == "yo"
        assert client.delete(f"/api/comments/{comment['id']}").status_code == 204
        link = {"src": a["id"], "dst": b["id"], "rel": "parent"}
        assert ok(client.post("/api/links", json=link), 201) == link
        assert client.post("/api/links/delete", json=link).status_code == 204
        assert ok(client.get(f"/api/jots/{a['id']}"))["links"] == []

    def test_catalog(self, client: TestClient, home: JotHome) -> None:
        """Projects, profiles, labels, events, and health are exposed."""
        ok(client.post("/api/projects", json={"slug": "p", "name": "P"}), 201)
        project = ok(client.patch("/api/projects/p", json={"archived": True}))
        assert project["archived"] is True
        assert items(client.get("/api/projects")) == []
        assert len(items(client.get("/api/projects?archived=true"))) == 1
        assert [p["id"] for p in items(client.get("/api/profiles"))] == ["note", "task"]
        client.post("/api/jots", json={"title": "a", "labels": ["l"]})
        assert items(client.get("/api/labels")) == [{"name": "l", "count": 1}]
        events = items(client.get("/api/events"))
        assert events[0]["kind"] == "project.created"
        assert items(client.get(f"/api/events?after={events[-1]['id']}")) == []
        assert ok(client.get("/api/health")) == {"status": "ok", "home": str(home.path)}


class TestErrors:
    """Expected failures map to HTTP status codes."""

    @pytest.mark.parametrize(
        ("method", "path", "body", "status"),
        [
            ("get", "/api/jots/99", None, 404),
            ("post", "/api/jots", {"title": ""}, 422),
            ("post", "/api/jots", {"title": "x", "profile": "epic"}, 422),
            ("post", "/api/jots", {"title": "x", "bogus": 1}, 422),
            ("post", "/api/projects", {"slug": "Bad Slug", "name": "x"}, 422),
            ("patch", "/api/comments/5", {"body": "x"}, 404),
            ("post", "/api/jots/1/rank", {"placement": "before"}, 422),
        ],
    )
    def test_status_codes(
        self,
        client: TestClient,
        method: str,
        path: str,
        body: Json | None,
        status: int,
    ) -> None:
        """Each failure carries a message and the right status."""
        client.post("/api/jots", json={"title": "seed"})
        response = client.request(method, path, json=body)
        assert response.status_code == status, response.text
        assert response.json()

    def test_version_conflict(self, client: TestClient) -> None:
        """A stale expected version is a 409 with the reason."""
        client.post("/api/jots", json={"title": "a"})
        response = client.post(
            "/api/jots/1/status", json={"status": "done", "expected_version": 9}
        )
        assert response.status_code == 409
        assert response.json() == {"error": "Jot 1 is at version 1, not 9"}

    def test_broken_config(self, client: TestClient, home: JotHome) -> None:
        """A broken config file is reported as a 422, not a crash."""
        home.config_file.write_text("[", encoding="utf-8")
        response = client.get("/api/jots")
        assert response.status_code == 422
        assert "Cannot load Jot home" in response.json()["error"]


class TestUi:
    """Static UI files are served and never cached stale."""

    @pytest.mark.parametrize(
        "path", ["/", "/app.js", "/styles.css", "/markdown.js", "/vendor/preact.mjs"]
    )
    def test_static(self, client: TestClient, path: str) -> None:
        """UI files load with no-cache headers."""
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"

    def test_openapi(self, client: TestClient) -> None:
        """API docs are published."""
        assert "/api/jots" in client.get("/api/openapi.json").json()["paths"]
