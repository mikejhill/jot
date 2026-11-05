# Development

## Setup

```bash
git clone https://github.com/mikejhill/jot && cd jot
uv sync
JOT_HOME=.smoke-home uv run jot add "try it"
```

Never point development or tests at your real data home (`~/.jot`). `.smoke-home/` and `.tmp/` are gitignored, and the test fixtures set `JOT_HOME` to a temporary folder.

## Checks

| Command | What it runs |
| --- | --- |
| `uv run poe check` | ruff format check, ruff lint, ty (every rule is an error), pytest with a 90% coverage gate |
| `uv run poe fix` | ruff format and auto-fixable lint |

| `uv run poe e2e` | Playwright browser tests of the web UI against a live server (Chromium; run `uv run playwright install chromium` once) |

Tests are grouped by layer: `tests/core` (pure), `tests/db`, `tests/app` (commands and queries against a real SQLite file), `tests/cli` (Typer runner, including the no-plugins acceptance flow), `tests/server` (FastAPI TestClient), and `tests/e2e` (browser, marked `e2e` and excluded from the default run). E2E tests fail on any browser console error.

The web UI is plain ES modules (Preact and htm, vendored in `src/jot/static/vendor/`), with no build step.

## Schema changes

Add the next version to `Migrations.UPGRADES` in `src/jot/db/migrations.py`, bump `SCHEMA_VERSION`, update `schema.sql`, and add a test that upgrades a database written by the previous version.

## CI

`.github/workflows/ci.yml` runs on every push and PR:

- **Lint & type check:** ruff format and lint, ty.
- **Tests:** pytest with coverage on Linux, macOS, and Windows × Python 3.13 and 3.14.
- **Browser UI tests:** Playwright Chromium e2e suite.
- **Build:** `uv build`, `twine check --strict`, and a wheel smoke test (CLI works; skill, schema, and UI packaged).
