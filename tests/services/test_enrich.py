"""Enrichment applies triage output, promotes confident tasks, and falls back."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from jot.agents.base import AgentError, JsonObject, JsonValue
from jot.agents.fake import FakeBackend
from jot.config import Config, JotHome
from jot.core.models import Criticality, EventKind, Project, Status, Task, TaskType
from jot.db.connection import Database
from jot.db.repository import ProjectRepository, TaskRepository
from jot.services.bus import EventBus, Topic
from jot.services.enrich import EnrichService, Triage

NOTE = "orbit api - health check is simple ping; need to make holistic"


def triage(**overrides: JsonValue) -> JsonObject:
    """Return a complete canned triage object."""
    data: JsonObject = {
        "title": "Add functional health checks",
        "description": "Check internals.",
        "project_slug": "",
        "new_project_name": "Orbit API",
        "labels": ["Orbit API", "health checks", "feature"],
        "criticality": "medium",
        "type": "feature",
        "confidence": 0.9,
        "questions": ["Which dependencies?"],
    }
    data.update(overrides)
    return data


class TestEnrich:
    """EnrichService behaviour with the fake backend."""

    @staticmethod
    def capture(db: Database, **fields: object) -> Task:
        """Insert an inbox task like `jot add`."""
        values: dict[str, object] = {"title": NOTE, "raw_input": NOTE}
        values.update(fields)
        return TaskRepository(db).create(Task.model_validate(values))

    def test_enrich(self, db: Database, home: JotHome, config: Config) -> None:
        """Fields, labels, new project, questions, and promotion are applied."""
        FakeBackend.canned = triage()
        bus = EventBus()
        task = self.capture(db)
        result = asyncio.run(EnrichService(db, home, config, bus).enrich(task.id))
        assert result.title == "Add functional health checks"
        assert result.status is Status.READY
        assert result.type is TaskType.FEATURE
        assert result.labels == ["feature", "health-checks", "orbit-api"]
        assert ProjectRepository(db).by_slug("orbit-api").id == result.project_id
        kinds = [e.kind for e in TaskRepository(db).events.for_task(task.id)]
        assert EventKind.QUESTION in kinds

    def test_existing_project_and_prefill(
        self, db: Database, home: JotHome, config: Config
    ) -> None:
        """Owner-supplied titles are kept; known slugs are matched."""
        project = ProjectRepository(db).create(Project(slug="orbit", name="Orbit"))
        FakeBackend.canned = triage(project_slug="orbit", confidence=0.2)
        task = self.capture(db, title="My title")
        result = asyncio.run(EnrichService(db, home, config).enrich(task.id))
        assert result.title == "My title"
        assert result.project_id == project.id
        assert result.status is Status.INBOX

    def test_new_project_reuses_slug(
        self, db: Database, home: JotHome, config: Config
    ) -> None:
        """A 'new' project whose slug exists reuses it; empty names are ignored."""
        project = ProjectRepository(db).create(Project(slug="orbit-api", name="R"))
        FakeBackend.canned = triage()
        task = self.capture(db)
        assert asyncio.run(
            EnrichService(db, home, config).enrich(task.id)
        ).project_id == (project.id)
        FakeBackend.canned = triage(new_project_name="!!!")
        other = self.capture(db)
        assert (
            asyncio.run(EnrichService(db, home, config).enrich(other.id)).project_id
            is None
        )

    def test_parse_defaults(self) -> None:
        """Bad fields fall back to safe defaults."""
        parsed = Triage.parse(
            {"criticality": "huge", "type": "?", "labels": "x", "confidence": "hi"},
            "fallback",
        )
        assert parsed.title == "fallback"
        assert parsed.criticality is Criticality.MEDIUM
        assert parsed.type is TaskType.IDEA
        assert parsed.labels == ()
        assert parsed.confidence == 0.0

    def test_pending_with_fallback(
        self,
        db: Database,
        home: JotHome,
        config: Config,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing backend leaves a comment, heuristics, and no retry flag."""
        ProjectRepository(db).create(
            Project(slug="orbit", name="Orbit", aliases=["orbit api"])
        )

        async def fail(
            self: FakeBackend, system: str, prompt: str, schema: JsonObject
        ) -> AsyncIterator[JsonObject]:
            del self, system, prompt, schema
            raise AgentError("offline")

        monkeypatch.setattr(FakeBackend, "structured", fail)
        long_note = NOTE + " " + "x" * 100
        task = self.capture(db, title=long_note, raw_input=long_note)
        moved = self.capture(db)
        TaskRepository(db).update(moved.id, {"needs_enrichment": False})
        bus = EventBus()
        [result] = asyncio.run(EnrichService(db, home, config, bus).enrich_pending())
        assert result.id == task.id
        assert not result.needs_enrichment
        assert result.project_id is not None
        assert result.title.endswith("…")
        events = TaskRepository(db).events.for_task(task.id)
        assert any(e.kind is EventKind.COMMENT for e in events)

    def test_prompt_lists_context(
        self, db: Database, home: JotHome, config: Config
    ) -> None:
        """The prompt carries guidance, projects with aliases, labels, prefill."""
        ProjectRepository(db).create(
            Project(slug="orbit", name="Orbit", aliases=["orb"], description="AI")
        )
        task = self.capture(db, labels=["ops"], title="T")
        service = EnrichService(db, home, config)
        prompt = service._prompt(task)
        assert "Criticality rubric" in prompt
        assert "aliases: orb" in prompt
        assert "labels: ops" in prompt
        assert Topic.TASK.value == "task"
