"""Versioned schema initialization."""

from __future__ import annotations

import sqlite3
from importlib.resources import files

from jot.exceptions import RepositoryError

SCHEMA_VERSION = 1


class Migrations:
    """Apply known schema versions without downgrading newer databases."""

    @staticmethod
    def apply(connection: sqlite3.Connection) -> None:
        """Install version one atomically, including on concurrent first opens."""
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RepositoryError(f"Database version {version} is newer than supported")
        if version == SCHEMA_VERSION:
            return
        schema = files("jot.db").joinpath("schema.sql").read_text(encoding="utf-8")
        connection.executescript(
            "BEGIN IMMEDIATE;\n" + schema + "\nPRAGMA user_version = 1;\nCOMMIT;"
        )
