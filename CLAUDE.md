# Claude guidelines for jot (same as AGENTS.md)

- **Workflow:** interactive sessions may commit directly to `main` in the main worktree, unless other work is already in progress there; then use a branch. Larger changes go through a pull request (`gh pr create`, filling in `.github/pull_request_template.md`).
- **Toolchain:** uv only. `uv run poe check` must pass before a PR (ruff format and lint, ty with every rule set to error, pytest with a 90% coverage gate).
- **Code style:** class-based design, a docstring on every module, class, and method, `from __future__ import annotations`, no `Any`, and app errors that subclass `AppError` (`src/jot/exceptions.py`). Match the surrounding code.
- **Data safety:** never use or modify the real data home (`~/.jot`) in tests or manual checks. Use a temporary `JOT_HOME`, such as the gitignored `.smoke-home/`. Schema changes need a new entry in `Migrations.UPGRADES` (`src/jot/db/migrations.py`) plus a test that upgrades an older database.
- **Layering:** `jot.core` is pure (no I/O; imports only `jot.exceptions`, enforced by `tests/test_layers.py`). Every write goes through `jot.app.commands.Commands`, which records an event and bumps the jot version in the same transaction. The core contains no LLM code: AI features belong in plugins (see `docs/architecture.md`).
- **Docs:** repository docs that aren't conventional root files (README, CONTRIBUTING, LICENSE, AGENTS/CLAUDE) go in `docs/`, with images in `docs/images/`.
