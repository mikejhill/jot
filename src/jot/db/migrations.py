"""Versioned schema initialization and upgrades."""

from __future__ import annotations

import sqlite3
from importlib.resources import files

from jot.exceptions import RepositoryError

SCHEMA_VERSION = 2
UPGRADES = {2: "ALTER TABLE runs ADD COLUMN model TEXT;"}


class Migrations:
    """Apply known schema versions without downgrading newer databases."""

    @staticmethod
    def apply(connection: sqlite3.Connection) -> None:
        """Install or upgrade the schema atomically, including on concurrent opens."""
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RepositoryError(f"Database version {version} is newer than supported")
        if version == SCHEMA_VERSION:
            return
        if version == 0:
            schema = files("jot.db").joinpath("schema.sql").read_text(encoding="utf-8")
            connection.executescript(
                f"BEGIN IMMEDIATE;\n{schema}\n"
                f"PRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
            )
            return
        for target in range(version + 1, SCHEMA_VERSION + 1):
            connection.executescript(
                f"BEGIN IMMEDIATE;\n{UPGRADES[target]}\n"
                f"PRAGMA user_version = {target};\nCOMMIT;"
            )
