"""Isolated data homes, sessions, and command fixtures."""

from __future__ import annotations

import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

import pytest

from jot.app.commands import Commands
from jot.app.session import Session
from jot.config import JotHome
from jot.core.models import Origin, WriteContext

TEST_ROOT = Path(__file__).resolve().parents[1] / ".tmp" / "tests"


class ReadOnlyCleanup:
    """Remove trees containing read-only files on Windows."""

    @staticmethod
    def retry(func: Callable[[str], object], path: str, _exc: BaseException) -> None:
        """Clear the read-only bit and retry the failed removal."""
        Path(path).chmod(stat.S_IWRITE)
        func(path)


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """Create local test paths with inherited Windows sandbox permissions."""
    path = TEST_ROOT / uuid4().hex
    path.mkdir(parents=True)
    yield path
    rmtree(path, onexc=ReadOnlyCleanup.retry)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> JotHome:
    """Point JOT_HOME at an empty folder for every test."""
    monkeypatch.setenv("JOT_HOME", str(tmp_path))
    monkeypatch.delenv("JOT_ACTOR", raising=False)
    monkeypatch.delenv("JOT_CORRELATION_ID", raising=False)
    return JotHome.resolve()


@pytest.fixture
def session(home: JotHome) -> Iterator[Session]:
    """Open a session on the isolated home."""
    with Session.open(home) as opened:
        yield opened


@pytest.fixture
def commands(session: Session) -> Commands:
    """Commands writing as the user through the CLI."""
    return session.commands(WriteContext(actor="user", origin=Origin.CLI))
