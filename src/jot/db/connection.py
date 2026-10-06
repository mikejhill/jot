"""SQLite configuration and atomic transaction ownership."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType

from jot.db.migrations import Migrations
from jot.exceptions import RepositoryError


class Database:
    """One explicitly closed connection per process or command."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @classmethod
    def open(cls, path: Path) -> Database:
        """Open a WAL database with foreign keys and a five-second busy timeout."""
        try:
            connection = sqlite3.connect(path, timeout=5, isolation_level=None)
        except sqlite3.Error as err:
            raise RepositoryError(f"Cannot open database {path}: {err}") from err
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            Migrations.apply(connection)
        except (sqlite3.Error, RepositoryError) as err:
            connection.close()
            raise RepositoryError(f"Cannot initialize database: {err}") from err
        return cls(connection)

    def __enter__(self) -> Database:
        """Return this database for deterministic cleanup."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection after use."""
        self.close()

    def close(self) -> None:
        """Close the connection."""
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize writers and commit audit events together with mutations."""
        if self.connection.in_transaction:
            yield self.connection
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except (sqlite3.Error, ValueError, RepositoryError) as err:
            self.connection.rollback()
            if isinstance(err, sqlite3.Error):
                raise RepositoryError(f"Database operation failed: {err}") from err
            raise
        finally:
            if self.connection.in_transaction:
                self.connection.rollback()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Commit successful work inside a serialized transaction."""
        nested = self.connection.in_transaction
        with self.transaction() as connection:
            yield connection
            if not nested:
                connection.commit()
