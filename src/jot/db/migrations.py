"""Versioned schema initialization and upgrades."""

from __future__ import annotations

import sqlite3
from importlib.resources import files

from jot.exceptions import RepositoryError

SCHEMA_VERSION = 3
TASKS_TABLE = "CREATE TABLE IF NOT EXISTS tasks ("


class Migrations:
    """Apply known schema versions without downgrading newer databases."""

    @staticmethod
    def schema() -> str:
        """Return the current full schema script."""
        return files("jot.db").joinpath("schema.sql").read_text(encoding="utf-8")

    @classmethod
    def rebuild_tasks(cls) -> str:
        """Recreate ``tasks`` from the current schema, keeping rows and ids.

        SQLite cannot alter CHECK constraints, so the table is copied into a new
        definition; re-running the idempotent schema restores indexes and triggers.
        """
        schema = cls.schema()
        start = schema.index(TASKS_TABLE)
        end = schema.index("\n);", start) + len("\n);")
        create = schema[start:end].replace(TASKS_TABLE, "CREATE TABLE tasks_new (")
        copy = (
            "INSERT INTO tasks_new SELECT * FROM tasks;\n"
            "DROP TABLE tasks;\nALTER TABLE tasks_new RENAME TO tasks;"
        )
        return "\n".join((create, copy, schema))

    @classmethod
    def upgrades(cls) -> dict[int, str]:
        """Return the script that upgrades the previous version to each version."""
        return {
            2: "ALTER TABLE runs ADD COLUMN model TEXT;",
            3: cls.rebuild_tasks(),  # adds the needs_input status
        }

    @classmethod
    def apply(cls, connection: sqlite3.Connection) -> None:
        """Install or upgrade the schema atomically, including on concurrent opens."""
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RepositoryError(f"Database version {version} is newer than supported")
        if version == SCHEMA_VERSION:
            return
        if version == 0:
            connection.executescript(
                f"BEGIN IMMEDIATE;\n{cls.schema()}\n"
                f"PRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
            )
            return
        upgrades = cls.upgrades()
        for target in range(version + 1, SCHEMA_VERSION + 1):
            cls._upgrade(connection, target, upgrades[target])

    @staticmethod
    def _upgrade(connection: sqlite3.Connection, target: int, script: str) -> None:
        """Run one upgrade with foreign keys off so table rebuilds keep history.

        The version is re-read under the write lock so a concurrent opener that
        already upgraded is not upgraded twice.
        """
        enforced = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute("PRAGMA user_version").fetchone()[0]
                if current < target:
                    # executescript() would commit first, so run statements singly.
                    for statement in Migrations._statements(script):
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {target}")
                Migrations._check_keys(connection, target)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.execute(f"PRAGMA foreign_keys = {int(enforced)}")

    @staticmethod
    def _statements(script: str) -> list[str]:
        """Split a script into complete statements (trigger bodies stay whole)."""
        statements: list[str] = []
        pending = ""
        for line in script.splitlines(keepends=True):
            pending += line
            if sqlite3.complete_statement(pending):
                statements.append(pending.strip())
                pending = ""
        if pending.strip():
            statements.append(pending.strip())
        return statements

    @staticmethod
    def _check_keys(connection: sqlite3.Connection, target: int) -> None:
        """Reject an upgrade that left dangling references."""
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise RepositoryError(f"Upgrade to v{target} broke foreign keys")
