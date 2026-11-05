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
