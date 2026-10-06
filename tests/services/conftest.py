"""Service fixtures wired to the offline fake agent backend."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from jot.agents.fake import FakeBackend
from jot.config import Config, JotHome

FAKE_CONFIG = """[triage]
backend = "fake"
[drawdown]
backend = "fake"
default_flow = "planned"
[execution]
isolation = "worktree"
"""


@pytest.fixture
def config(home: JotHome) -> Config:
    """Configuration that routes triage and drawdown to the fake backend."""
    (home.path / "config.toml").write_text(FAKE_CONFIG, encoding="utf-8")
    return home.initialize()


@pytest.fixture(autouse=True)
def canned() -> Iterator[None]:
    """Reset the fake backend's canned output around every test."""
    FakeBackend.canned = {}
    FakeBackend.replies = []
    yield
    FakeBackend.canned = {}
    FakeBackend.replies = []


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a committed git repository, skipping when git is unavailable."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        [
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
    ):
        subprocess.run([git, *args], cwd=repo, check=True)  # noqa: S603 - fixed git args
    return repo
