"""Open a data home once and hand out commands and queries bound to it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from jot.app.commands import Commands
from jot.app.queries import Queries
from jot.config import JotHome
from jot.db.connection import Database

if TYPE_CHECKING:
    from types import TracebackType

    from jot.core.models import WriteContext
    from jot.core.profiles import ProfileRegistry


class Session:
    """One data home, its settings, profiles, and an open database."""

    def __init__(
        self, home: JotHome, database: Database, profiles: ProfileRegistry
    ) -> None:
        self.home = home
        self.db = database
        self.profiles = profiles
        self.queries = Queries(database)

    @classmethod
    def open(cls, home: JotHome | None = None) -> Session:
        """Resolve and initialize the home, load profiles, and open the database."""
        home = home or JotHome.resolve()
        profiles = home.initialize().registry()
        return cls(home, Database.open(home.database), profiles)

    def commands(self, context: WriteContext) -> Commands:
        """Return a command handler that writes as ``context``."""
        return Commands(self.db, self.profiles, context)

    def close(self) -> None:
        """Close the database."""
        self.db.close()

    def __enter__(self) -> Self:
        """Return this session for deterministic cleanup."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the database after use."""
        self.close()
