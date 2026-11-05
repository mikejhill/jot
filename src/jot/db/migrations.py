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

    @classmethod
    def apply(cls, connection: sqlite3.Connection) -> None:
        """Install or upgrade the schema atomically."""
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).fetchone():
            raise RepositoryError(
                "This is a Jot v0.1 database and cannot be opened in place. "
                "Point JOT_HOME at a new folder (a v0.1 importer is planned)."
            )
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
        for target in range(version + 1, SCHEMA_VERSION + 1):  # pragma: no cover
            connection.executescript(
                f"BEGIN IMMEDIATE;\n{cls.UPGRADES[target]}\n"
                f"PRAGMA user_version = {target};\nCOMMIT;"
            )
