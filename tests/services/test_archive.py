"""Export and online backup behaviour."""

from __future__ import annotations

import json
import sqlite3

import pytest

from jot.config import JotHome
from jot.core.models import Task
from jot.db.connection import Database
from jot.db.repository import TaskRepository
from jot.exceptions import AppError
from jot.services.archive import ArchiveService, Output


class TestArchiveService:
    """Exports and backups preserve committed task data."""

    def test_export_and_backup(self, db: Database, home: JotHome) -> None:
        """JSONL, Markdown, and SQLite backups reflect the original task."""
        task = TaskRepository(db).create(
            Task(title="Orbit", raw_input="holistic", labels=["reliability"])
        )
        archive = ArchiveService(db)
        assert json.loads(archive.export("jsonl"))["id"] == task.id
        assert "holistic" in archive.export("md")
        with pytest.raises(AppError, match="format"):
            archive.export("csv")
        path = archive.backup(home.path / "backups")
        with sqlite3.connect(path) as backup:
            assert backup.execute("SELECT title FROM tasks").fetchone()[0] == "Orbit"
        backup.close()
        TaskRepository(db).delete(task.id)
        assert archive.export("jsonl") == ""
        assert (
            json.loads(archive.export("jsonl", include_deleted=True))["deleted_at"]
            is not None
        )

    def test_output(self) -> None:
        """JSON is Unicode-safe and tables support empty and multiline cells."""
        assert Output.json({"title": "日本"}) == '{"title": "日本"}\n'
        assert "ID" in Output.table([], ("id",))
        assert "one two" in Output.table([{"title": "one\ntwo"}], ("title",))
