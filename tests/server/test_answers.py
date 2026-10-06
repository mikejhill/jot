"""Answering a needs_input task over HTTP with the offline fake backend."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jot.agents.base import Json, JsonObject
from jot.agents.fake import FakeBackend
from jot.config import JotHome
from jot.core.models import Task
from jot.server import create_app
from jot.services.enrich import EnrichService

QUESTIONS = json.dumps(
    {
        "plan": "# Plan\n1. Decide",
        "questions": [{"id": "db", "text": "Which DB?", "choices": ["pg", "lite"]}],
    }
)


async def no_pending(_self: EnrichService, limit: int = 10) -> list[Task]:
    """Keep the enrichment worker away from providers."""
    del limit
    return []


@pytest.fixture
def client(home: JotHome, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """App whose runner uses the fake backend with scripted replies."""
    (home.path / "config.toml").write_text(
        '[drawdown]\nbackend = "fake"\n', encoding="utf-8"
    )
    monkeypatch.setattr(EnrichService, "enrich_pending", no_pending)
    FakeBackend.replies = [QUESTIONS]
    with TestClient(create_app(home)) as result:
        yield result
    FakeBackend.replies = []


def settle(client: TestClient, url: str, status: str) -> JsonObject:
    """Poll a task until it reaches ``status`` (runs finish in the background)."""
    deadline = time.monotonic() + 10
    while True:
        detail = Json.obj(client.get(url).json())
        current = Json.obj(detail["task"])["status"]
        if current == status or time.monotonic() > deadline:
            assert current == status
            return detail
        time.sleep(0.02)


def first_question(detail: JsonObject) -> JsonObject:
    """Return the first open question of a task detail."""
    questions = detail["questions"]
    assert isinstance(questions, list)
    return Json.obj(questions[0])


class TestAnswers:
    """Questions surface in task detail; answers resume the asking phase."""

    def test_answer_and_resume(self, client: TestClient) -> None:
        """A needs_input task exposes questions; answering re-plans to approval."""
        task_id = client.post("/api/tasks", json={"text": "Store"}).json()["id"]
        url = f"/api/tasks/{task_id}"
        client.post(url + "/move", json={"status": "ready"})
        assert client.post(url + "/run", json={"backend": None}).status_code == 200
        detail = settle(client, url, "needs_input")
        assert detail["resume_phase"] == "plan"
        transitions = detail["transitions"]
        assert isinstance(transitions, list)
        assert "ready" in transitions
        question = first_question(detail)
        assert question["text"] == "Which DB?"
        assert question["choices"] == ["pg", "lite"]
        assert question["answer"] is None
        assert (
            client.post(url + "/move", json={"status": "planning"}).status_code == 409
        )

        bad = client.post(
            url + "/answers", json={"answers": [{"question_id": 1, "text": "x"}]}
        )
        assert bad.status_code == 409
        run = client.post(
            url + "/answers",
            json={"answers": [{"question_id": question["id"], "text": "pg"}]},
        )
        assert run.status_code == 200
        assert run.json()["phase"] == "plan"
        detail = settle(client, url, "awaiting_approval")
        assert detail["resume_phase"] is None
        assert first_question(detail)["answer"] == "pg"
        again = client.post(url + "/answers", json={"answers": []})
        assert again.status_code == 409
