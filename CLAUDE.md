# Claude guidelines for jot (same as AGENTS.md)

- **Workflow:** every change goes through a pull request against `main`. Never push to `main` directly. Branch as `feat/…`, `fix/…`, or `chore/…`; Jot's runner uses `jot/<id>-<slug>` branches. Open the PR with `gh pr create` and fill in `.github/pull_request_template.md`.
- **Toolchain:** uv only. `uv run poe check` must pass before a PR (ruff format and lint, ty with every rule set to error, pytest with a 90% coverage gate). If you touch `src/jot/static/`, also run `node --test tests/server/ui.test.mjs`.
- **Code style:** class-based design, a docstring on every module, class, and method, `from __future__ import annotations`, no `Any`, and app errors that subclass `AppError` (`src/jot/exceptions.py`). Match the surrounding code.
- **Data safety:** never use or modify the real data home (`~/.jot`) in tests or manual checks. Use a temporary `JOT_HOME`, such as the gitignored `.smoke-home/`. Schema changes need a new version in `src/jot/db/migrations.py` plus a test that upgrades an older database.
- **Agents:** all LLM access goes through `jot.agents.base.AgentBackend`. Add a provider as one class plus an entry in `agents/registry.py`. Tests use `FakeBackend` and never call real CLIs.
