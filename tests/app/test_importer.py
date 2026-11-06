"""Importing Jot v0.1 databases."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from tests.app.v01 import V01Builder

from jot.app.importer import STATUS_MAP, V01Importer
from jot.app.queries import JotFilter
from jot.core.models import Origin, WriteContext
from jot.exceptions import ConflictError, InvalidInputError, RepositoryError

if TYPE_CHECKING:
    from pathlib import Path

    from jot.app.commands import Commands
    from jot.app.session import Session

IMPORT = WriteContext(actor="mike", origin=Origin.IMPORT, reason="import")


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """A v0.1 database exercising every mapped feature."""
    db = V01Builder(tmp_path / "v01.db")
    jot = db.project("jot", description="Tracker", repo_path="C:/jot", priority=2)
    db.project("old", archived=1)
    db.project("Bad Slug")
    parent = db.task(
        "Build importer",
        description="Port **everything**",
        project_id=jot,
        type="feature",
        criticality="high",
        status="executing",
        claimed_by="agent:claude",
        lease_expires_at="2026-09-03T00:00:00Z",
        lease_prior_status="ready",
        raw_input="build the importer pls",
    )
    child = db.task("Write tests", status="needs_input", parent_id=parent)
    db.task("Inbox idea", needs_enrichment=1)
    db.task("Shipped", status="done", completed_at="2026-09-05T00:00:00", type="bug")
    db.task("Dropped", status="archived")
    gone = db.task("Deleted", deleted_at="2026-09-06T00:00:00+00:00")
    db.task("Orphan", parent_id=gone)
    db.task(
        "Critical bug", criticality="critical", status="blocked", due_at="2026-12-01"
    )
    db.label(parent, "Backend Work")
    db.label(parent, "urgent")
    db.label(child, "9lives")
    db.label(child, "!!!")
    db.event(parent, "created", {"source": "cli"})
    db.event(parent, "comment", {"text": "Looks good"}, actor="mike")
    db.event(parent, "plan", {"text": "1. read\n2. write", "run_id": 1}, "agent:claude")
    db.event(parent, "question", {"text": "Keep ids?"}, actor="agent:claude")
    db.event(parent, "answer", {"text": "Yes", "question": 4}, actor="mike")
    db.event(parent, "status", {"from": "ready", "to": "executing"})
    db.event(parent, "comment", {"text": "   "})
    db.run(parent, cost=0.42, model="opus", input_tokens=10)
    db.cleanup()
    return db.close()


def digest(path: Path) -> str:
    """Hash a file to prove it was not modified."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestImport:
    """A full import keeps ids, content, history, and structure."""

    def test_everything(self, source: Path, session: Session) -> None:
        """Every mapped feature lands where documented."""
        before = digest(source)
        report = V01Importer(source).run(session, IMPORT)
        assert digest(source) == before
        assert (report.jots, report.deleted, report.archived) == (8, 1, 1)
        assert (report.comments, report.links, report.events, report.runs) == (
            4,
            1,
            7,
            1,
        )
        assert report.projects_created == ["jot", "old"]
        assert report.skipped == {"cleanup_proposals": 1}
        assert report.renamed_labels == {
            "Backend Work": "backend-work",
            "9lives": "l-9lives",
            "!!!": "unnamed",
        }
        assert any("Bad Slug" in w for w in report.warnings)
        assert any("parent link to 6" in w for w in report.warnings)
        assert report.statuses["executing -> doing"] == 1

        queries = session.queries
        built = queries.get(1)
        assert (built.title, built.body, built.profile, built.status) == (
            "Build importer",
            "Port **everything**",
            "task",
            "doing",
        )
        assert built.project == "jot"
        assert built.labels == ["backend-work", "executing", "feature", "urgent"]
        legacy = built.metadata["v01"]
        assert isinstance(legacy, dict)
        assert legacy["status"] == "executing"
        assert legacy["criticality"] == "high"
        assert legacy["raw_input"] == "build the importer pls"
        assert legacy["claimed_by"] == "agent:claude"
        assert built.created_at.isoformat() == "2026-09-01T10:00:00+00:00"
        comments = queries.comments(1)
        assert [(c.kind, c.author, c.body) for c in comments] == [
            ("comment", "mike", "Looks good"),
            ("execution.plan", "agent:claude", "1. read\n2. write"),
            ("execution.question", "agent:claude", "Keep ids?"),
            ("execution.answer", "mike", "Yes"),
        ]
        assert comments[0].created_at.isoformat() == "2026-09-02T09:30:00+00:00"
        history = queries.history(1)
        assert [e.kind for e in history] == ["jot.imported", "link.added"]
        imported = history[0]
        assert (imported.actor, imported.origin, imported.reason) == (
            "mike",
            Origin.IMPORT,
            "import",
        )
        events = imported.data["events"]
        assert isinstance(events, list)
        assert len(events) == 7
        runs = imported.data["runs"]
        assert isinstance(runs, list)
        assert runs[0]["cost"] == 0.42  # ty: ignore[not-subscriptable, invalid-argument-type]

    def test_other_tasks_and_projects(self, source: Path, session: Session) -> None:
        """Workflow labels, links, archive, delete, and project details map."""
        V01Importer(source).run(session, IMPORT)
        queries = session.queries
        child = queries.get(2)
        assert (child.status, child.labels) == (
            "doing",
            ["l-9lives", "needs-input", "unnamed"],
        )
        assert [(link.src, link.dst, link.rel) for link in queries.links(2)] == [
            (1, 2, "parent")
        ]
        assert queries.get(3).labels == ["triaging"]
        shipped = queries.get(4)
        assert (shipped.status, shipped.labels) == ("done", ["bug"])
        dropped = queries.get(5, include_deleted=True)
        assert dropped.archived_at is not None
        assert queries.get(6, include_deleted=True).deleted_at is not None
        blocked = queries.get(8)
        assert (blocked.status, blocked.labels) == ("todo", ["blocked"])

        projects = {p.slug: p for p in queries.projects(include_archived=True)}
        assert projects["old"].archived
        project_events = [
            e for e in queries.events(0, 100) if e.kind == "project.imported"
        ]
        assert project_events[0].data["project"]["repo_path"] == "C:/jot"  # ty: ignore[not-subscriptable, invalid-argument-type]

    def test_rank_order(self, source: Path, session: Session) -> None:
        """Open work comes first, by criticality, then age; finished work last."""
        V01Importer(source).run(session, IMPORT)
        ordered = session.queries.jots(
            JotFilter(include_archived=True, include_deleted=True)
        )
        assert [j.id for j in ordered] == [8, 1, 2, 3, 6, 7, 4, 5]

    def test_new_jots_continue_after_imported_ids(
        self, source: Path, session: Session, commands: Commands
    ) -> None:
        """New jots get ids after the highest imported id."""
        V01Importer(source).run(session, IMPORT)
        assert commands.create(title="next").id == 9

    def test_dry_run_writes_nothing(self, source: Path, session: Session) -> None:
        """A dry run reports the same counts and leaves the home empty."""
        report = V01Importer(source).run(session, IMPORT, dry_run=True)
        assert report.dry_run
        assert report.jots == 8
        assert report.projects_created == ["jot", "old"]
        assert session.queries.jots(JotFilter(include_deleted=True)) == []
        assert session.queries.projects(include_archived=True) == []
        assert session.queries.events(0, 10) == []

    def test_existing_project_is_reused(
        self, source: Path, session: Session, commands: Commands
    ) -> None:
        """Projects already in the home are reused, not duplicated."""
        commands.create_project("jot", "Jot")
        report = V01Importer(source).run(session, IMPORT)
        assert report.projects_existing == ["jot"]
        assert session.queries.get(1).project == "jot"


class TestSourceIsUntouched:
    """The v0.1 database and its folder are never written."""

    def test_wal_database_gets_no_side_files(
        self, source: Path, session: Session
    ) -> None:
        """WAL-mode sources are read from a copy: no -shm or -wal appears."""
        sqlite3.connect(source).execute("PRAGMA journal_mode=WAL").connection.close()
        before = sorted(p.name for p in source.parent.iterdir())
        digest_before = digest(source)
        V01Importer(source).run(session, IMPORT, dry_run=True)
        assert sorted(p.name for p in source.parent.iterdir()) == before
        assert digest(source) == digest_before

    def test_uncheckpointed_wal_is_read(self, source: Path, session: Session) -> None:
        """Committed changes still in the WAL are included."""
        writer = sqlite3.connect(source)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE tasks SET title = 'Renamed in WAL' WHERE id = 1")
        writer.commit()
        try:
            assert (source.parent / f"{source.name}-wal").stat().st_size > 0
            V01Importer(source).run(session, IMPORT)
        finally:
            writer.close()
        assert session.queries.get(1).title == "Renamed in WAL"


class TestRefusals:
    """Imports that would lose or mix data are refused, atomically."""

    def test_non_empty_home(
        self, source: Path, session: Session, commands: Commands
    ) -> None:
        """A home that already has jots is refused."""
        commands.create(title="mine")
        with pytest.raises(ConflictError, match="already has jots"):
            V01Importer(source).run(session, IMPORT)

    def test_missing_and_foreign_files(self, tmp_path: Path, session: Session) -> None:
        """Missing files, non-databases, and other databases are refused."""
        with pytest.raises(InvalidInputError, match=r"No v0\.1 database"):
            V01Importer(tmp_path / "nope.db").run(session, IMPORT)
        other = tmp_path / "other.db"
        sqlite3.connect(other).execute("CREATE TABLE x (y)").connection.close()
        with pytest.raises(InvalidInputError, match=r"not a Jot v0\.1"):
            V01Importer(other).run(session, IMPORT)
        junk = tmp_path / "junk.db"
        junk.write_text("not sqlite")
        with pytest.raises(RepositoryError, match="Cannot read"):
            V01Importer(junk).run(session, IMPORT)

    def test_profile_without_statuses(self, source: Path, session: Session) -> None:
        """The target profile must have every mapped status."""
        with pytest.raises(InvalidInputError):
            V01Importer(source, profile="note").run(session, IMPORT)

    def test_failure_rolls_everything_back(
        self, tmp_path: Path, session: Session
    ) -> None:
        """One bad task aborts the whole import."""
        db = V01Builder(tmp_path / "bad.db")
        db.project("p")
        db.task("fine")
        db.task("")
        with pytest.raises(InvalidInputError, match="no text"):
            V01Importer(db.close()).run(session, IMPORT)
        assert session.queries.jots(JotFilter(include_deleted=True)) == []
        assert session.queries.projects() == []


class TestMapping:
    """Pure mapping helpers."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("ok", "ok"),
            ("Two Words", "two-words"),
            ("a/b", "a-b"),
            ("1st", "l-1st"),
            ("--", "unnamed"),
        ],
    )
    def test_label(self, name: str, expected: str) -> None:
        """Labels become valid names."""
        assert V01Importer.label(name) == expected

    def test_every_v01_status_is_mapped(self) -> None:
        """All eleven v0.1 statuses have a target."""
        assert len(STATUS_MAP) == 11

    def test_bad_values(self, tmp_path: Path) -> None:
        """Unparseable timestamps fall back; non-integer ids are refused."""
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        assert V01Importer._time("garbage", moment) == moment
        assert V01Importer._time(None, moment) == moment
        with pytest.raises(RepositoryError):
            V01Importer._int("1")
        assert V01Importer(tmp_path / "x.db").profile == "task"
