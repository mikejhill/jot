"""Resolve where an agent works and isolate execution in git worktrees."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from jot.exceptions import AppError

logger = logging.getLogger(__name__)

SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
SLUG_LIMIT = 40


class WorkspaceError(AppError):
    """A working directory or worktree could not be prepared."""


@dataclass(frozen=True, slots=True)
class Workspace:
    """Directory an agent runs in, plus worktree details when isolated."""

    cwd: Path
    repo: Path | None = None
    worktree: Path | None = None
    branch: str | None = None


class GitWorkspaces:
    """Create (or reuse) per-task worktrees at ``<root>/<repo>/<id>-<slug>``.

    Each worktree is on a ``jot/<id>-<slug>`` branch. ``root`` defaults to
    ``$JOT_HOME/worktrees``, which keeps worktrees out of project folders.
    """

    executable: str = "git"

    def __init__(self, root: Path) -> None:
        self.root = root

    async def is_repo(self, path: Path) -> bool:
        """Return True when ``path`` is inside a git work tree."""
        code, _ = await self._git(path, "rev-parse", "--is-inside-work-tree")
        return code == 0

    async def ensure_repo(self, path: Path) -> None:
        """Make ``path`` the root of its own git repository if it isn't one.

        Raises:
            WorkspaceError: git could not initialize the repository.
        """
        code, out = await self._git(path, "rev-parse", "--show-toplevel")
        if code == 0 and Path(out.strip()).resolve() == path.resolve():
            return
        code, out = await self._git(path, "init", "-q")
        if code != 0:
            raise WorkspaceError(f"git init failed in {path}: {out}")

    async def toplevel(self, path: Path) -> Path:
        """Return the repository root containing ``path``."""
        code, out = await self._git(path, "rev-parse", "--show-toplevel")
        if code != 0:
            raise WorkspaceError(f"{path} is not a git repository: {out}")
        return Path(out.strip())

    async def worktree(self, repo: Path, task_id: int, title: str) -> Workspace:
        """Return the task's worktree, creating branch and directory if missing.

        Raises:
            WorkspaceError: git refused to create the worktree.
        """
        root = await self.toplevel(repo)
        slug = SLUG_PATTERN.sub("-", title.lower()).strip("-")[:SLUG_LIMIT].strip("-")
        name = f"{task_id}-{slug or 'task'}"
        branch = f"jot/{name}"
        target = await self._repo_folder(root) / name
        if target.exists():
            return Workspace(cwd=target, repo=root, worktree=target, branch=branch)
        target.parent.mkdir(parents=True, exist_ok=True)
        code, _ = await self._git(root, "rev-parse", "--verify", "--quiet", branch)
        args = ["worktree", "add", str(target)]
        args += [branch] if code == 0 else ["-b", branch]
        code, out = await self._git(root, *args)
        if code != 0:
            raise WorkspaceError(f"git worktree add failed: {out}")
        return Workspace(cwd=target, repo=root, worktree=target, branch=branch)

    async def _repo_folder(self, repo: Path) -> Path:
        """Return ``<root>/<repo name>``, disambiguated if another repo owns it."""
        folder = self.root / repo.name
        if not folder.exists() or await self._owned_by(folder, repo):
            return folder
        digest = hashlib.sha256(str(repo).lower().encode()).hexdigest()[:6]
        return self.root / f"{repo.name}-{digest}"

    async def _owned_by(self, folder: Path, repo: Path) -> bool:
        """Return True when the worktrees in ``folder`` belong to ``repo``."""
        children = [child for child in folder.iterdir() if child.is_dir()]
        if not children:
            return True
        _, common = await self._git(children[0], "rev-parse", "--git-common-dir")
        _, expected = await self._git(repo, "rev-parse", "--git-common-dir")
        resolve = (children[0] / common.strip(), repo / expected.strip())
        return resolve[0].resolve() == resolve[1].resolve()

    async def diffstat(self, workspace: Workspace) -> str:
        """Return ``git diff --stat`` of the branch against its fork point."""
        if workspace.repo is None or workspace.branch is None:
            return ""
        _, main_head = await self._git(workspace.repo, "rev-parse", "HEAD")
        _, base = await self._git(
            workspace.cwd, "merge-base", "HEAD", main_head.strip()
        )
        _, committed = await self._git(
            workspace.cwd, "diff", "--stat", f"{base.strip()}...HEAD"
        )
        _, pending = await self._git(workspace.cwd, "status", "--short")
        parts = [committed.strip()]
        if pending.strip():
            parts.append("Uncommitted:\n" + pending.strip())
        return "\n".join(part for part in parts if part)

    async def _git(self, cwd: Path, *args: str) -> tuple[int, str]:
        """Run git and return (exit code, combined output)."""
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                *args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as err:
            raise WorkspaceError(f"cannot run git: {err}") from err
        try:
            out, _ = await process.communicate()
        except BaseException:
            # A cancelled run must not leave git running in its workspace.
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        return process.returncode or 0, out.decode("utf-8", errors="replace")
