"""Human-readable exports and consistent SQLite backups."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from jot.core.models import Clock
from jot.db.connection import Database
from jot.db.query import TaskQuery
from jot.db.repository import TaskRepository
from jot.exceptions import AppError


class ArchiveService:
    """Export task content and snapshot live WAL databases safely."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def export(self, output_format: str, *, include_deleted: bool = False) -> str:
        """Render all selected tasks as JSON Lines or Markdown."""
        tasks = TaskRepository(self._db).query(
            TaskQuery(include_deleted=include_deleted)
        )
        if output_format == "jsonl":
            return "".join(task.model_dump_json() + "\n" for task in tasks)
        if output_format != "md":
            raise AppError("Export format must be jsonl or md")
        sections = ["# Jot tasks\n"]
        sections.extend(
            f"\n## {task.id}: {task.title}\n\nStatus: {task.status}\n"
            f"Criticality: {task.criticality}\nLabels: {', '.join(task.labels)}\n\n"
            f"{task.description or task.raw_input}\n"
            for task in tasks
        )
        return "".join(sections)

    def backup(self, directory: Path) -> Path:
        """Use SQLite's backup API, including committed WAL content."""
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = Clock.now().strftime("%Y%m%dT%H%M%S%fZ")
        path = directory / f"jot-{timestamp}-{uuid4().hex[:8]}.db"
        try:
            with closing(sqlite3.connect(path)) as destination:
                self._db.connection.backup(destination)
        except sqlite3.Error as err:
            raise AppError(f"Backup failed: {err}") from err
        return path


class Output:
    """Serialize machine output and compact plain-text tables."""

    @staticmethod
    def json(value: object) -> str:
        """Return JSON with one trailing newline."""
        return json.dumps(value, ensure_ascii=False) + "\n"

    @staticmethod
    def table(rows: list[dict[str, object]], columns: tuple[str, ...]) -> str:
        """Align selected fields without introducing terminal dependencies."""
        cells = [
            [str(row.get(col, "")).replace("\n", " ") for col in columns]
            for row in rows
        ]
        widths = [
            max(len(col), *(len(row[index]) for row in cells)) if cells else len(col)
            for index, col in enumerate(columns)
        ]
        lines = [
            "  ".join(
                col.upper().ljust(width)
                for col, width in zip(columns, widths, strict=True)
            )
        ]
        lines.extend(
            "  ".join(
                cell.ljust(width) for cell, width in zip(row, widths, strict=True)
            )
            for row in cells
        )
        return "\n".join(lines) + "\n"
