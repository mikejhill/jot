# Architecture

## Code layout

```text
src/jot/
  cli.py, cli_agents.py   Typer CLI (core commands, agent commands)
  config.py               JOT_HOME, config.toml, default instruction files
  core/                   models, workflow (status machine, claims, leases), prioritize
  db/                     SQLite connection, schema.sql, versioned migrations, repositories
  agents/                 provider facade + Claude / Codex / Copilot / fake backends
  services/               enrich, runner, cleanup, workspaces, event bus, instructions
  server/                 FastAPI app, routes, SSE stream, uvicorn launcher
  static/                 web UI: Preact + htm (vendored, no build step), Markdown renderer
  skills/jot/             agent skill: SKILL.md + references/
  demo.py                 demo workspace, test server, screenshot generator
```

## Data model

SQLite in WAL mode, with schema versions applied by `db/migrations.py` (`PRAGMA user_version`). Upgrades run in one transaction. `ADD COLUMN` steps are idempotent, and table rebuilds keep row IDs and history.

- `tasks`: content, status, flow, lease fields (`claimed_by`, `lease_expires_at`, `lease_prior_status`).
- `task_events`: append-only history. Created, enriched, status, comment, plan, question, answer, approval, run_log, result.
- `runs`: backend, model, phase, status, worktree/branch, summary, and token totals (input, output, cache read/write, premium requests).
- `projects`, `labels`, `task_labels`, `cleanup_proposals`, and `tasks_fts` (FTS5, kept in sync by triggers).

Full run logs live outside the database in `runs/<id>.jsonl`.

## Claims and leases

A claim is a conditional `UPDATE … WHERE status=? AND claimed_by IS NULL` under `BEGIN IMMEDIATE`; zero rows updated means another agent won. The runner heartbeats the lease while it works and releases it to the next status, or to the prior status if the run fails or is cancelled. Expired leases are reaped by the server's background worker or by `jot reap`. Only claims move a task into `planning` or `executing`.

## Agent backends

All LLM access goes through `jot.agents.base.AgentBackend`:

- `structured(system, prompt, schema)` returns one JSON object (triage, cleanup).
- `run(RunRequest)` streams `AgentEvent`s (text, thinking, tool, usage, error) and ends with a `result`.

| Backend | Transport | Usage reporting |
| --- | --- | --- |
| `claude` | Claude Agent SDK | Per turn from the API `message_delta` stream event (exact), plus the result total |
| `codex` | `codex exec --json` | Per turn from `turn.completed` (input net of cached input) |
| `copilot` | `copilot -p --output-format json` | Model per turn and premium requests; the CLI exposes no token counts |
| `fake` | In-process | Scripted; used by tests and the demo |

To swap or add an SDK, write one `AgentBackend` subclass and register it in `agents/registry.py` (plus model suggestions for the UI picker). Models resolve per action: an explicit `--model`, else `[models.<backend>].<action>` in `config.toml`, else the provider's default.

## Runs

`services/runner.py` claims the task, resolves the workspace (task or project repo, else a scratch folder that is its own git repo), and creates a worktree for execute runs in git repos. It then streams the backend's events to the run log and the in-process event bus, which feeds the UI over SSE. On finish it records the plan, questions, or result as task events and stores the token totals. Prompts combine the owner's `drawdown.md` with project instructions, the task, and its history.

## Web server

`jot serve` runs uvicorn with a launcher that closes the event bus on Ctrl+C, so open SSE streams end immediately. A 3-second graceful-shutdown timeout is the backstop. Static UI files are served with `Cache-Control: no-cache`, so browsers always revalidate after an update.
