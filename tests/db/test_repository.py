"""Schema, CRUD, FTS triggers, and parameterized filter verification."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from jot.config import JotHome
from jot.core.models import Clock, Criticality, EventKind, Project, Run, Status, Task
from jot.db.connection import Database
from jot.db.migrations import Migrations
from jot.db.query import TaskQuery
from jot.db.repository import (
    EventRepository,
    ProjectRepository,
    RunRepository,
    TaskRepository,
)
from jot.exceptions import NotFoundError, RepositoryError


class TestRepositories:
    """Repositories retain audit integrity and enforce data validity."""

    def test_schema(self, db: Database) -> None:
        """Every planned table and connection pragma is installed."""
        tables = {
            row[0]
            for row in db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "projects",
            "tasks",
            "labels",
            "task_labels",
            "task_events",
            "runs",
            "cleanup_proposals",
            "tasks_fts",
        } <= tables
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert db.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        Migrations.apply(db.connection)
        db.connection.execute("PRAGMA user_version=3")
        with pytest.raises(RepositoryError, match="newer"):
            Migrations.apply(db.connection)

    def test_projects_and_runs(self, db: Database) -> None:
        """Projects and run metadata support validated CRUD."""
        projects = ProjectRepository(db)
        project = projects.create(
            Project(slug="orbit", name="Orbit", aliases=["orbit api"])
        )
        assert projects.by_slug("orbit").aliases == ["orbit api"]
        assert projects.update(project.id, {"priority": 3}).priority == 3
        with pytest.raises(RepositoryError, match="immutable"):
            projects.update(project.id, {"id": 9})
        with pytest.raises(ValidationError):
            projects.update(project.id, {"priority": 0})
        with pytest.raises(RepositoryError):
            projects.create(Project(slug="orbit", name="Duplicate"))
        task = TaskRepository(db).create(Task(title="run"))
        runs = RunRepository(db)
        run = runs.create(Run(task_id=task.id, backend="codex", phase="plan"))
        assert runs.list() == [run]
        assert (
            runs.update(run.id, {"status": "complete", "summary": "planned"}).summary
            == "planned"
        )
        runs.delete(run.id)
        assert runs.list() == []
        projects.delete(project.id)
        with pytest.raises(NotFoundError):
            projects.get(project.id)
        with pytest.raises(NotFoundError):
            projects.by_slug("missing")

    def test_tasks_and_events(self, db: Database) -> None:
        """Task edits audit changes and soft deletion retains history."""
        tasks = TaskRepository(db)
        task = tasks.create(Task(title="capture", labels=["Test"]))
        assert task.status == Status.INBOX
        assert task.needs_enrichment
        assert task.labels == ["test"]
        assert (
            tasks.update(task.id, {"description": "refined"}).description == "refined"
        )
        events = EventRepository(db)
        history = events.for_task(task.id)
        assert history[0].kind == EventKind.CREATED
        assert events.get(history[0].id) == history[0]
        with pytest.raises(RepositoryError, match="immutable"):
            events.update(history[0].id, {"actor": "tamper"})
        with pytest.raises(RepositoryError, match="immutable"):
            events.delete(history[0].id)
        with pytest.raises(RepositoryError, match="workflow"):
            tasks.update(task.id, {"status": "done"})
        with pytest.raises(RepositoryError, match="inbox"):
            tasks.create(Task(title="skip", status=Status.DONE))
        with pytest.raises(NotFoundError):
            tasks.get(999)
        tasks.delete(task.id)
        assert tasks.list() == []
        assert len(tasks.query(TaskQuery(include_deleted=True))) == 1
        assert events.for_task(task.id)[-1].body["action"] == "soft_delete"

    def test_fts_label_sync(self, db: Database) -> None:
        """FTS follows content, label attachment/removal, rename, and task deletion."""
        tasks = TaskRepository(db)
        task = tasks.create(
            Task(
                title="Health ping",
                raw_input="orbit holistic",
                description="reliability",
            )
        )
        for word in ("health", "orbit", "reliability"):
            assert tasks.query(TaskQuery(q=word))[0].id == task.id
        tasks.label(task.id, "observability")
        assert tasks.query(TaskQuery(q="observability"))[0].id == task.id
        db.connection.execute(
            "UPDATE labels SET name='telemetry' WHERE name='observability'"
        )
        assert tasks.query(TaskQuery(q="observability")) == []
        assert tasks.query(TaskQuery(q="telemetry"))[0].id == task.id
        tasks.label(task.id, "telemetry", remove=True)
        assert tasks.query(TaskQuery(q="telemetry")) == []
        other = tasks.create(Task(title="Other"))
        tasks.label(task.id, "transfer")
        db.connection.execute(
            "UPDATE task_labels SET task_id=? WHERE task_id=?", (other.id, task.id)
        )
        assert [t.id for t in tasks.query(TaskQuery(q="transfer"))] == [other.id]
        tasks.update(
            task.id,
            {
                "title": "heartbeat",
                "description": "resilience",
                "raw_input": "monitoring",
            },
        )
        assert tasks.query(TaskQuery(q="health")) == []
        assert tasks.query(TaskQuery(q="heartbeat"))[0].id == task.id
        with pytest.raises(RepositoryError, match="empty"):
            tasks.label(task.id, " ")
        with pytest.raises(RepositoryError, match="query"):
            tasks.query(TaskQuery(q='"'))
        db.connection.execute("DELETE FROM tasks WHERE id=?", (task.id,))
        assert tasks.query(TaskQuery(q="heartbeat")) == []

    def test_query_filters(self, db: Database) -> None:
        """Filters compose, match every requested label, and resist SQL injection."""
        projects = ProjectRepository(db)
        project = projects.create(Project(slug="orbit", name="Orbit"))
        tasks = TaskRepository(db)
        task = tasks.create(
            Task(
                title="old",
                project_id=project.id,
                criticality=Criticality.HIGH,
                labels=["a", "b"],
                created_at=Clock.now() - timedelta(days=40),
            )
        )
        tasks.create(Task(title="new", labels=["a"]))
        db.connection.execute(
            "UPDATE tasks SET updated_at=? WHERE id=?",
            (Clock.stamp(Clock.now() - timedelta(days=40)), task.id),
        )
        query = TaskQuery(
            project="orbit",
            labels=("a", "b"),
            statuses=(Status.INBOX, Status.READY),
            criticality=Criticality.HIGH,
            older_than=30,
            limit=1,
            order="-created_at",
        )
        assert [t.id for t in tasks.query(query)] == [task.id]
        assert tasks.query(TaskQuery(project="' OR 1=1 --")) == []
        assert tasks.query(TaskQuery(labels=("missing",))) == []
        assert (
            tasks.query(TaskQuery(older_than=30, age_field="created_at"))[0].id
            == task.id
        )
        assert tasks.query(TaskQuery(limit=0)) == []
        assert tasks.query(TaskQuery(order="-id"))[0].title == "new"
        assert len(tasks.query(TaskQuery(order="due_at"))) == 2
        for invalid in (
            TaskQuery(age_field="bad"),
            TaskQuery(older_than=-1),
            TaskQuery(limit=-1),
            TaskQuery(order="id; DROP TABLE tasks"),
        ):
            with pytest.raises(RepositoryError):
                tasks.query(invalid)

    def test_rollback(self, db: Database) -> None:
        """Failed writes roll back content and audit events as a unit."""
        tasks = TaskRepository(db)
        with pytest.raises(RepositoryError):
            self._failed_write(db)
        assert tasks.list() == []
        assert EventRepository(db).list() == []
        with pytest.raises(ValueError, match="abort"):
            self._aborted_write(db)
        assert tasks.list() == []
        with pytest.raises(RepositoryError), db.write():
            db.connection.execute("INSERT INTO task_labels VALUES(999,999)")
        assert not db.connection.in_transaction

    @staticmethod
    def _failed_write(db: Database) -> None:
        """Trigger an integrity error after a successful task insert."""
        with db.write():
            TaskRepository(db).create(Task(title="rolled back"))
            db.connection.execute("INSERT INTO labels(name) VALUES(NULL)")

    @staticmethod
    def _aborted_write(db: Database) -> None:
        """Abort a transaction after an insert using a domain-independent error."""
        with db.write():
            TaskRepository(db).create(Task(title="also rolled back"))
            raise ValueError("abort")


class TestMigrations:
    """Upgrades from older schema versions."""

    def test_upgrade_adds_run_model(self, home: JotHome) -> None:
        """A version-1 database gains runs.model on open."""
        with Database.open(home.database) as database:
            database.connection.execute("ALTER TABLE runs DROP COLUMN model")
            database.connection.execute("PRAGMA user_version=1")
        with Database.open(home.database) as database:
            columns = {
                row[1] for row in database.connection.execute("PRAGMA table_info(runs)")
            }
            version = database.connection.execute("PRAGMA user_version").fetchone()[0]
        assert "model" in columns
        assert version == 2
