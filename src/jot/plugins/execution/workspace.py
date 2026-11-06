"""Git worktrees: one isolated checkout and branch per jot.

A jot's work happens on branch ``jot/<id>-<slug>`` in a worktree under the
configured root, never in the user's own checkout. The worktree is created
from the repository's current HEAD, reused across attempts, and removed only
on request.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from jot.exceptions import AppError

SLUG_LENGTH = 40
FALLBACK_NAME = "Jot"
FALLBACK_EMAIL = "jot@localhost"


class WorkspaceError(AppError):
    """A git operation for a jot's worktree failed."""


class Git:
    """Run git in one directory."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    @staticmethod
    def executable() -> str:
        """The git binary, or raise."""
        found = shutil.which("git")
        if found is None:
            raise WorkspaceError("git is not on PATH")
        return found

    def __call__(self, *args: str, check: bool = True) -> str:
        """Run ``git <args>`` and return stdout (stripped)."""
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [self.executable(), *args],
            cwd=self.cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if check and done.returncode != 0:
            detail = (done.stderr or done.stdout).strip()[-500:]
            raise WorkspaceError(f"git {' '.join(args)} failed: {detail}")
        return done.stdout.strip()

    def ok(self, *args: str) -> bool:
        """Whether ``git <args>`` succeeds."""
        try:
            self(*args)
        except WorkspaceError:
            return False
        return True


class Workspace:
    """The worktree and branch for one jot."""

    def __init__(self, repo: Path, root: Path, jot_id: int, title: str) -> None:
        self.repo = repo
        self.name = f"{jot_id}-{self.slug(title)}"
        self.branch = f"jot/{self.name}"
        self.path = root / repo.name / self.name

    @staticmethod
    def slug(title: str) -> str:
        """A short branch-safe slug of a title."""
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        return slug[:SLUG_LENGTH].rstrip("-") or "work"

    @classmethod
    def check_repo(cls, repo: Path) -> Path:
        """Return the repository's top folder, or raise."""
        if not repo.is_dir():
            raise WorkspaceError(f"Repository {repo} does not exist")
        top = Git(repo)("rev-parse", "--show-toplevel")
        return Path(top)

    @property
    def git(self) -> Git:
        """Git in the worktree."""
        return Git(self.path)

    def exists(self) -> bool:
        """Whether the worktree is checked out."""
        return (self.path / ".git").exists()

    def ensure(self) -> str:
        """Create (or reuse) the worktree; return the commit it started from."""
        repo = Git(self.check_repo(self.repo))
        if self.exists():
            return repo("merge-base", "HEAD", self.branch)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if repo.ok("rev-parse", "--verify", "--quiet", f"refs/heads/{self.branch}"):
            repo("worktree", "add", str(self.path), self.branch)
            return repo("merge-base", "HEAD", self.branch)
        base = repo("rev-parse", "HEAD")
        repo("worktree", "add", "-b", self.branch, str(self.path), base)
        return base

    def identity(self) -> list[str]:
        """``-c`` flags for a commit identity when git has none configured."""
        flags: list[str] = []
        if not self.git("config", "user.name", check=False):
            flags += ["-c", f"user.name={FALLBACK_NAME}"]
        if not self.git("config", "user.email", check=False):
            flags += ["-c", f"user.email={FALLBACK_EMAIL}"]
        return flags

    def dirty(self) -> bool:
        """Whether the worktree has uncommitted changes (untracked files count)."""
        return bool(self.git("status", "--porcelain"))

    def untracked(self) -> list[str]:
        """Untracked, unignored files (left out of automatic commits)."""
        out = self.git("ls-files", "--others", "--exclude-standard")
        return out.splitlines() if out else []

    def commit_pending(self, message: str) -> bool:
        """Commit changes to tracked files; return whether a commit was made.

        Untracked files are left alone: they are often build output, and the
        agent is told to commit the files it adds.
        """
        if not self.git("status", "--porcelain", "--untracked-files=no"):
            return False
        self.git("add", "-u")
        self.git(*self.identity(), "commit", "--no-verify", "-q", "-m", message)
        return True

    def head(self) -> str:
        """The worktree's HEAD commit."""
        return self.git("rev-parse", "HEAD")

    def commits(self, base: str) -> list[str]:
        """One line per commit since ``base``, oldest first."""
        out = self.git("log", "--reverse", "--format=%h %s", f"{base}..HEAD")
        return out.splitlines() if out else []

    def diffstat(self, base: str) -> str:
        """``git diff --stat`` since ``base``."""
        return self.git("diff", "--stat", f"{base}..HEAD")

    def remove(self, *, force: bool = False) -> None:
        """Remove the worktree (the branch stays)."""
        if not self.exists():
            raise WorkspaceError(f"No worktree at {self.path}")
        if self.dirty() and not force:
            raise WorkspaceError(
                f"{self.path} has uncommitted changes; use --force to discard them"
            )
        args = ["worktree", "remove", *(["--force"] if force else []), str(self.path)]
        Git(self.check_repo(self.repo))(*args)
