"""Isolated data homes and database fixtures."""

from __future__ import annotations

import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

import pytest

from jot.config import JotHome
from jot.db.connection import Database


class ReadOnlyCleanup:
    """Remove trees containing read-only files (git objects on Windows)."""

    @staticmethod
    def retry(func: Callable[[str], object], path: str, _exc: BaseException) -> None:
        """Clear the read-only bit and retry the failed removal."""
        Path(path).chmod(stat.S_IWRITE)
        func(path)


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """Create local test paths with inherited Windows sandbox permissions.

    Python's Windows mode 0700 directories exclude the sandbox capability SID;
    pytest's default temporary directory fixture uses that mode.
    """
    path = Path(__file__).resolve().parents[1] / ".tmp" / "tests" / uuid4().hex
    path.mkdir(parents=True)
    yield path
    rmtree(path, onexc=ReadOnlyCleanup.retry)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> JotHome:
    """Initialize an isolated data home for every test."""
    monkeypatch.setenv("JOT_HOME", str(tmp_path))
    result = JotHome.resolve()
    result.initialize()
    return result


@pytest.fixture
def db(home: JotHome) -> Iterator[Database]:
    """Open and close a fresh SQLite database."""
    with Database.open(home.database) as database:
        yield database
