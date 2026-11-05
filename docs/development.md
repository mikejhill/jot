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

Tests are grouped by layer: `tests/core` (pure), `tests/db`, `tests/app` (commands and queries against a real SQLite file), and `tests/cli` (Typer runner, including the no-plugins acceptance flow).

## Schema changes

Add the next version to `Migrations.UPGRADES` in `src/jot/db/migrations.py`, bump `SCHEMA_VERSION`, update `schema.sql`, and add a test that upgrades a database written by the previous version.

## CI

`.github/workflows/ci.yml` runs on every push and PR:

- **Lint & type check:** ruff format and lint, ty.
- **Tests:** pytest with coverage on Linux, macOS, and Windows × Python 3.13 and 3.14.
- **Build:** `uv build`, `twine check --strict`, and a wheel smoke test (CLI works, skill packaged).
