# jot

[![CI](https://github.com/mikejhill/jot/actions/workflows/ci.yml/badge.svg)](https://github.com/mikejhill/jot/actions/workflows/ci.yml)
![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Lightweight task capture with agent drawdown. Jot is a local SQLite task tracker with a CLI, a web UI, and agent skills. You type a quick note, and an LLM turns it into a structured task. When you want the work done, Claude, Codex, or Copilot draws it down with conflict-free claims. Everything runs locally; no hosted service is required.

```text
jot add "orbit api - health check is simple ping; need to make holistic"
  -> project: Orbit API · labels: orbit-api, health-checks, feature, code-change
     criticality: medium · title: "Make Orbit API health check holistic instead of a simple ping"
     description: problem, desired outcome, acceptance criteria
```

## Install

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/). Each agent backend is optional; install whichever ones you use: Claude Code (`claude auth login`), Codex CLI, or GitHub Copilot CLI. Jot uses their existing logins and needs no API keys.

```bash
uv tool install git+https://github.com/mikejhill/jot   # puts `jot` on PATH
jot install-skills --target claude --target codex --target copilot
jot serve --open                                        # http://127.0.0.1:8765
```

From a clone, use `uv tool install --editable .` so code changes apply without reinstalling. A PyPI release is planned.

Data lives in `JOT_HOME` (default `~/.jot`). Point separate installs or contexts at different homes to keep their data apart.

| Path | Purpose |
| --- | --- |
| `jot.db` | SQLite database (WAL). Query it with any SQLite tool, or use `jot export --format jsonl\|md` |
| `config.toml` | Backends and models, default flow, concurrency, isolation, port, cleanup thresholds |
| `instructions/triage.md` | How notes become tasks: label vocabulary, criticality rubric |
| `instructions/drawdown.md` | How agents prioritize, refine, ask questions, plan, and execute |
| `instructions/cleanup.md` | What counts as stale or duplicate |
| `instructions/projects/<slug>.md` | Per-project guidance (repo conventions, test commands) |
| `runs/<id>.jsonl` | Full agent logs |
| `backups/` | `jot backup` snapshots |

Every instruction file can also be edited in the UI (Instructions view).

## Capture

- **UI:** use the capture bar at the top of every view (`Ctrl+K`, then Enter). The task appears instantly and fills in when enrichment finishes.
- **CLI:** `jot add "<note>"` returns in about 100 ms. Enrichment runs in the server's background worker, or immediately with `--wait`. You can pre-fill fields with `--project --label --crit --type --title --repo`.
- **Agents:** the `jot` skill (installed by `jot install-skills`) teaches Claude Code, Codex, and Copilot every Jot workflow: capture, query and groom, drawdown, and cleanup. Its `SKILL.md` routes to one reference file per workflow.

Enrichment reuses existing projects (matched by slug, name, or alias) and existing labels, so labels don't drift into synonyms. A task with clear scope moves to `ready`. An unclear one stays in `inbox` and its open questions are recorded on the task. Set `[triage] backend` in `config.toml` to `claude`, `codex`, or `copilot`.

## Draw down

There are two flows. Choose one per run, or set a default per task, per project (`--default-flow`), or globally.

```text
planned: inbox → ready → planning → awaiting_approval → executing → review → done
direct:  inbox → ready → executing → review → done
either:  planning | executing → needs_input → (answers) → same phase again
```

**Questions from the agent.** Plan and execute runs can return structured questions (`{"id", "text", "choices"?}`). The task then moves to `needs_input`, and the run's lease is released. Each question appears in the UI with its own input: radio buttons when the agent suggested choices, otherwise a text box. Answers are optional. Submitting saves each one as an `answer` event linked to its question, then resumes the same phase (plan or execute). Blank answers leave the decision to the agent. The resumed run sees every question paired with its answer and reaches its own conclusion. If it asks more questions, the task returns to `needs_input`. You can also send the task back to `ready` instead of answering.

`needs_input` is a separate status, not a flag on `awaiting_approval`, because execute runs can ask questions too. It shows which tasks are waiting on you and lets a paused run resume exactly where it stopped.

```bash
jot next -n 5                      # ranked queue: criticality × project priority × age
jot plan 12 --backend claude       # read-only plan -> awaiting_approval (needs_input if it has questions)
jot answer 12 -a 41="Postgres"     # answer question event 41 and resume the run
jot approve 12 --note "answers…"   # execute -> review
jot run 12 --direct --backend codex
jot run --next --project orbit-api
jot send-back 12 "use the existing retry helper"   # -> ready, feedback kept for next run
jot runs 12; jot log <run-id>
```

**Choosing models.** Every action can use a different model. Set defaults per backend and action in `config.toml` (`[models.claude] plan = "opus"`, `execute = "sonnet"`, and so on; `"default"` means the provider's default). Override any single run with `--model` on `jot plan`, `run`, `approve`, `enrich`, and `cleanup scan`, or with the model pickers in the UI. Each run records the model it used (`jot runs`).

**One-click list.** The List view shows Plan / Run now, Answer / Send back, Approve / Send back, Done, or Cancel buttons on every row, depending on status. Use the "Plan with" and "Execute with" pickers above the table to choose backends and models. Expand a row (▸) to read the latest plan (rendered as Markdown), answer open questions, or approve it inline. Presets: Open, Needs attention, Needs input, Ready, In progress, All.

In the UI, open a task to use **Plan first**, **Run now (direct)**, **Approve**, and **Send back**, with a backend picker and a live log. The List view supports bulk actions.

- **Where agents work:** the task's `repo_path`, or else the project's `repo_path`, or else a scratch folder. Execution in a git repo happens in a worktree, `<repo>.jot/<id>-<slug>`, on branch `jot/<id>-<slug>`. The agent commits there and never pushes. You review and merge.
- **No conflicts:** a claim is an atomic conditional update under `BEGIN IMMEDIATE` with a lease that the runner heartbeats. Two agents can never hold the same task. Expired leases are returned to the queue by the server, or by `jot reap`.
- **External agents:** the `jot` skill's drawdown workflow lets an interactive Claude, Codex, or Copilot session use the same protocol: `jot claim`, then `jot comment --kind plan|question|result`, then `jot release --status …` (`needs_input` when blocked on questions).
- **Rendering:** plans, results, descriptions, and timeline comments render as Markdown. The renderer builds DOM nodes directly and never injects HTML, so raw HTML in agent output appears as text. Links are limited to `http(s)` and `mailto`.
- **Permissions:** plan runs are read-only (Claude: read tools only; Codex: `--sandbox read-only`; Copilot: view/grep/glob only). Execute runs have full tool access inside the workspace.

## Find and clean up

```bash
jot ls --project orbit-api --status ready --label security --crit high
jot ls --older-than 60d --status inbox
jot ls --q "health check"          # full-text search (FTS5) over title, description, note, labels
jot cleanup scan --agent           # heuristics + LLM review using cleanup.md
jot cleanup apply 3 --approve 0,2  # only approved items; archive / soft-delete / note
```

The `jot` skill runs the same review conversationally. It also queries the queue and suggests changes to apply with your approval: reprioritize, refine, merge or split tasks, fill in missing fields, or pick the next tasks to run. Nothing changes without item-level approval.

## Swapping agent SDKs

All agent access goes through `jot.agents.base.AgentBackend`, which has two methods: `structured()` for triage and cleanup, and `run()` for plan and execute runs, which yields streamed events. Backends:

- `claude`: Claude Agent SDK
- `codex`: `codex exec --json`
- `copilot`: `copilot -p --output-format json`
- `fake`: offline, for tests

To add a provider, write one class and register it in `jot/agents/registry.py`.

## Development

```bash
git clone https://github.com/mikejhill/jot && cd jot
uv sync
uv run poe check                     # ruff format/lint, ty (strict), pytest + 90% coverage gate
node --test tests/server/ui.test.mjs # UI component tests (no browser needed)
JOT_HOME=.smoke-home uv run jot serve --port 8766   # dev server with throwaway data
```

Layout: `src/jot/{db,core,agents,services,server,static,skills}`. The CLI is in `cli.py` (core commands) and `cli_agents.py` (agent commands). Schema changes are versioned migrations in `db/migrations.py`.

### Contributing

All changes go through pull requests against `main`:

1. Create a branch (`feat/…`, `fix/…`, `chore/…`), or let `jot run` make a `jot/<id>-<slug>` worktree branch.
2. Keep `uv run poe check` green. CI runs lint and strict type checks, tests on Linux, macOS, and Windows (Python 3.13 and 3.14), the UI tests, and a wheel build plus smoke test.
3. Open a PR using the template and merge once CI passes.

Dependabot keeps GitHub Actions and the Python dependencies up to date.

## License

[MIT](LICENSE)
