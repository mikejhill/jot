"""Versioned schema installation and upgrades."""

from __future__ import annotations

import sqlite3
from importlib.resources import files

from jot.exceptions import RepositoryError

SCHEMA_VERSION = 1


class Migrations:
    """Install the current schema; refuse databases this version cannot read.

    Later versions add an entry to ``UPGRADES`` (target version -> script) and
    a test that upgrades a database written by the previous version.
    """

    UPGRADES: dict[int, str] = {}  # noqa: RUF012 - filled by later schema versions

    @staticmethod
    def schema() -> str:
        """Return the current full schema script."""
        return files("jot.db").joinpath("schema.sql").read_text(encoding="utf-8")

    @staticmethod
    def version(connection: sqlite3.Connection) -> int:
        """Return the database's schema version."""
        return int(connection.execute("PRAGMA user_version").fetchone()[0])

    @classmethod
    def apply(cls, connection: sqlite3.Connection) -> None:
        """Install or upgrade the schema atomically, safe under concurrent opens.

        The version is re-read under the write lock, so when several processes
        or requests open a new database at once, exactly one installs it.
        """
        cls._reject_v01(connection)
        if cls._current(connection):
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            version = cls.version(connection)
            if not cls._current(connection):
                scripts = (
                    [cls.schema()]
                    if version == 0
                    else [
                        cls.UPGRADES[target]
                        for target in range(version + 1, SCHEMA_VERSION + 1)
                    ]
                )
                for script in scripts:
                    for statement in cls.statements(script):
                        connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    @classmethod
    def _current(cls, connection: sqlite3.Connection) -> bool:
        """Return True when no work is needed; refuse newer databases."""
        version = cls.version(connection)
        if version > SCHEMA_VERSION:
            raise RepositoryError(f"Database version {version} is newer than supported")
        return version == SCHEMA_VERSION

    @staticmethod
    def _reject_v01(connection: sqlite3.Connection) -> None:
        """Refuse a v0.1 task database with guidance."""
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).fetchone():
            raise RepositoryError(
                "This is a Jot v0.1 database and cannot be opened in place. "
                "Point JOT_HOME at a new folder (a v0.1 importer is planned)."
            )

    @staticmethod
    def statements(script: str) -> list[str]:
        """Split a script into complete statements (trigger bodies stay whole)."""
        statements: list[str] = []
        pending = ""
        for line in script.splitlines(keepends=True):
            if not pending and line.lstrip().startswith("--"):
                continue
            pending += line
            if sqlite3.complete_statement(pending):
                statements.append(pending.strip())
                pending = ""
        if pending.strip():
            statements.append(pending.strip())
        return statements
