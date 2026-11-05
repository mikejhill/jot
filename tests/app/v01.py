"""Build Jot v0.1 databases for importer tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).parent / "fixtures" / "v01_schema.sql"
T0 = "2026-09-01T10:00:00+00:00"


class V01Builder:
    """Write rows into a v0.1 schema (user_version 5)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.executescript(SCHEMA.read_text(encoding="utf-8"))
        self.connection.execute("PRAGMA user_version = 5")

    def project(self, slug: str, **values: object) -> int:
        """Insert a project."""
        row = {"slug": slug, "name": slug.title(), **values}
        return self._insert("projects", row)

    def task(self, title: str, **values: object) -> int:
        """Insert a task with sensible defaults."""
        row = {
            "title": title,
            "type": "idea",
            "criticality": "medium",
            "status": "inbox",
            "source": "cli",
            "created_at": T0,
            "updated_at": T0,
            **values,
        }
        return self._insert("tasks", row)

    def label(self, task_id: int, name: str) -> None:
        """Attach a label."""
        self.connection.execute(
            "INSERT OR IGNORE INTO labels (name) VALUES (?)", (name,)
        )
        self.connection.execute(
            "INSERT INTO task_labels (task_id, label_id)"
            " SELECT ?, id FROM labels WHERE name = ?",
            (task_id, name),
        )

    def event(
        self, task_id: int, kind: str, body: dict[str, object], actor: str = "user"
    ) -> None:
        """Append a task event."""
        self._insert(
            "task_events",
            {
                "task_id": task_id,
                "ts": "2026-09-02T09:30:00Z",
                "actor": actor,
                "kind": kind,
                "body": json.dumps(body),
            },
        )

    def run(self, task_id: int, **values: object) -> None:
        """Record an agent run."""
        row = {
            "task_id": task_id,
            "backend": "claude",
            "phase": "execute",
            "status": "succeeded",
            "started_at": T0,
            **values,
        }
        self._insert("runs", row)

    def cleanup(self) -> None:
        """Record a cleanup proposal (not imported)."""
        self._insert(
            "cleanup_proposals", {"created_at": T0, "items": "[]", "status": "open"}
        )

    def close(self) -> Path:
        """Commit and close; return the path."""
        self.connection.commit()
        self.connection.close()
        return self.path

    def _insert(self, table: str, row: dict[str, object]) -> int:
        columns = ", ".join(row)
        marks = ", ".join("?" * len(row))
        cursor = self.connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({marks})",  # noqa: S608 - test data
            tuple(row.values()),
        )
        return int(cursor.lastrowid or 0)
