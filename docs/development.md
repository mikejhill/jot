# Development

## Setup

```bash
git clone https://github.com/mikejhill/jot && cd jot
uv sync
JOT_HOME=.smoke-home uv run jot serve --port 8766   # dev server with throwaway data
```

Never point development or tests at your real data home (`~/.jot`). `.smoke-home/` and `.tmp/` are gitignored.

## Checks

| Command | What it runs |
| --- | --- |
| `uv run poe check` | ruff format check, ruff lint, ty (every rule is an error), pytest with a 90% coverage gate |
| `node --test tests/server/ui.test.mjs` | UI component tests: renders Preact trees in Node, no browser |
| `uv run poe e2e` | Browser end-to-end tests (Playwright) against a seeded demo workspace |
| `uv run poe screenshots` | Regenerates `docs/images/*.png` from the demo workspace |

The browser tests and the screenshot generator use your installed Chrome (`--browser-channel chrome`), so there's no browser download. To use Playwright's bundled Chromium instead, run `uv run playwright install chromium` and pass `--browser-channel chromium`, or `--channel chromium` for screenshots.

Browser tests live in `tests/e2e/` and are marked `e2e`, so plain `pytest` skips them. They cover:

- board columns and cards, capture with `Ctrl+K`, theme toggle
- run output: Markdown, collapsed tool/thinking steps, per-turn usage and totals
- structured plan rendering and a timeline free of `<pre>` blocks
- List view row actions, inline plan, and populated pickers
- phone-width layout without page-level horizontal scroll
- no console errors on any page

## Screenshots

`src/jot/demo.py` seeds a deterministic workspace (projects, tasks in every status, a plan run with questions, an execute run with a Markdown table, per-turn usage). It serves the workspace on a free port and captures the board (light and dark), the List view, and the task panel. Regenerate the screenshots whenever the UI changes and commit `docs/images/`. CI also uploads a fresh set as a build artifact for review.

## CI

`.github/workflows/ci.yml` runs on every push and PR:

- **Lint & type check:** ruff format and lint, ty.
- **Tests:** pytest with coverage on Linux, macOS, and Windows × Python 3.13 and 3.14.
- **UI tests:** `node --test`.
- **Browser tests:** Playwright e2e on Chrome, plus the screenshot artifact (`screenshots`). Traces upload on failure.
- **Build:** `uv build`, `twine check --strict`, and a wheel install smoke test (the CLI works; the skill and UI files are packaged).

Dependabot keeps GitHub Actions and the Python dependencies current. Publishing to PyPI is planned.
