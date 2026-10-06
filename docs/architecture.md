# Architecture

## Code layout

```text
src/jot/
  cli.py, cli_agents.py   Typer CLI (core commands, agent commands)
  harnesses.py            typed harness/model/loadout/pin policy
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
- `task_events`: append-only history. Created, enriched, status, comment, plan, question, answer, approval, run_log, result, routing.
- `runs`: backend, harness ID, nullable JSON loadout summary, model, phase, status, worktree/branch, summary, and token totals (input, output, cache read/write, premium requests).
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

## Agent settings and routing

`Config` resolves named `HarnessConfig` instances: explicit picks, then `[actions]` and the selected harness's action model default, then the provider default. Legacy provider tables produce implicit instances. `ModelPolicy` enforces allow/deny lists with deny precedence, and disabled instances cannot execute. Pins are global with optional action filters. Harness and loadout policies have no project overrides.

`BackendRegistry.configured` maps a harness kind to an `AgentBackend` and attaches the action's loadout and appended instructions. Structured phases are forced lean. Plan, execute, and assist loadouts default to no account capabilities with repo instructions enabled. Discovery stays behind the backend facade: Claude init messages, Copilot loaded-capability events, and an empty best-effort Codex inventory.

`SettingsStore` exposes effective settings and performs validated, atomic TOML writes with tomlkit. `GET/PUT /api/settings` and `POST /api/settings/harnesses/{id}/discover` expose these operations. PUT updates all shared service snapshots; already-created agents retain their snapshot. Capture persists the triage pick in its creation event so a later enrichment worker uses the same choice.

`Router` handles the reserved Auto pick using one structured backend call and `instructions/routing.md`. Its configured harness defaults to triage. Candidates include enabled instances, known permitted models, configured defaults, and action-visible pins. Output must name a candidate and pass model policy. Any routing failure uses the action default. A `routing` task event records the explanation and reported usage; a separate JSONL log preserves the call outcome. Agent runs copy their routing explanation into their run log.

Schema v5 adds nullable `runs.harness` and `runs.loadout`, and rebuilds the task-event constraint to admit `routing`, retaining existing event IDs and history. The shared UI picker is used for capture, drawer, and List actions; Settings uses the same policy-filtered model suggestions.
