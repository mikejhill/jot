"""Versioned schema installation and upgrades."""

from __future__ import annotations

import sqlite3
from importlib.resources import files
from typing import ClassVar

from jot.exceptions import RepositoryError

SCHEMA_VERSION = 2


class Migrations:
    """Install the current schema; refuse databases this version cannot read.

    ``schema.sql`` is the version 1 base. Each later version adds a packaged
    ``upgrade_<n>.sql`` to ``UPGRADES`` (target version -> script) and a test
    that upgrades a database written by the previous version. A fresh install
    runs the base and every upgrade in one transaction.
    """

    UPGRADES: ClassVar[dict[int, str]] = {2: "upgrade_2.sql"}

    @staticmethod
    def script(name: str) -> str:
        """Return a packaged SQL script."""
        return files("jot.db").joinpath(name).read_text(encoding="utf-8")

    @classmethod
    def schema(cls) -> str:
        """Return the full current schema: the base plus every upgrade."""
        return "\n".join(
            [cls.script("schema.sql")]
            + [cls.script(cls.UPGRADES[target]) for target in sorted(cls.UPGRADES)]
        )

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
                        cls.script(cls.UPGRADES[target])
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
                "Point JOT_HOME at a new folder and run `jot import v01 <path>`."
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
