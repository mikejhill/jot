"""Schema installation and version guards."""

from __future__ import annotations

import sqlite3

import pytest

from jot.config import JotHome
from jot.db.connection import Database
from jot.db.migrations import SCHEMA_VERSION
from jot.exceptions import RepositoryError


class TestMigrations:
    """Fresh installs, reopening, and refusal of unknown databases."""

    @staticmethod
    def duplicate_labels(db: Database) -> None:
        """Insert one label, then fail on a duplicate in the same transaction."""
        with db.write() as conn:
            conn.execute("INSERT INTO labels (name) VALUES ('a')")
            conn.execute("INSERT INTO labels (name) VALUES ('a')")

    def test_install_and_reopen(self, home: JotHome) -> None:
        """The schema installs once at the current version."""
        home.path.mkdir(parents=True, exist_ok=True)
        with Database.open(home.database) as db:
            version = db.connection.execute("PRAGMA user_version").fetchone()[0]
            assert version == SCHEMA_VERSION
        with Database.open(home.database) as db:
            assert db.connection.execute("SELECT count(*) FROM jots").fetchone()[0] == 0

    def test_newer_database(self, home: JotHome) -> None:
        """A database from a newer Jot is refused."""
        home.path.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(home.database)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.close()
        with pytest.raises(RepositoryError, match="newer than supported"):
            Database.open(home.database)

    def test_v01_database(self, home: JotHome) -> None:
        """A v0.1 task database is refused with guidance."""
        home.path.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(home.database)
        connection.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY)")
        connection.execute("PRAGMA user_version = 5")
        connection.close()
        with pytest.raises(RepositoryError, match=r"v0\.1 database"):
            Database.open(home.database)

    def test_unopenable(self, home: JotHome) -> None:
        """A path that cannot be opened fails cleanly."""
        with pytest.raises(RepositoryError, match="Cannot open"):
            Database.open(home.path / "missing" / "jot.db")

    def test_transaction_rolls_back(self, home: JotHome) -> None:
        """A failed write leaves nothing behind and is reported."""
        home.path.mkdir(parents=True, exist_ok=True)
        with Database.open(home.database) as db:
            with pytest.raises(RepositoryError):
                self.duplicate_labels(db)
            assert (
                db.connection.execute("SELECT count(*) FROM labels").fetchone()[0] == 0
            )


class TestConcurrentInstall:
    """Many first opens at once install the schema exactly once."""

    def test_parallel_opens(self, home: JotHome) -> None:
        """Every concurrent opener succeeds on a brand-new database."""
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

        home.path.mkdir(parents=True, exist_ok=True)

        def open_once(_: int) -> int:
            with Database.open(home.database) as db:
                return int(db.connection.execute("PRAGMA user_version").fetchone()[0])

        with ThreadPoolExecutor(max_workers=32) as pool:
            assert set(pool.map(open_once, range(64))) == {SCHEMA_VERSION}

    def test_statements(self) -> None:
        """Scripts split on complete statements; comments and tails survive."""
        from jot.db.migrations import Migrations  # noqa: PLC0415

        script = (
            "-- note\nCREATE TABLE a (x);\n"
            "CREATE TRIGGER t AFTER INSERT ON a BEGIN\n  SELECT 1;\nEND;\nSELECT 2"
        )
        assert Migrations.statements(script) == [
            "CREATE TABLE a (x);",
            "CREATE TRIGGER t AFTER INSERT ON a BEGIN\n  SELECT 1;\nEND;",
            "SELECT 2",
        ]
