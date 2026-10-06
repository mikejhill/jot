# User guide

- [Data home](#data-home)
- [Capture](#capture)
- [Draw down](#draw-down)
- [The List view](#the-list-view)
- [Reading runs](#reading-runs)
- [Find and clean up](#find-and-clean-up)

## Data home

Data lives in `JOT_HOME` (default `~/.jot`, i.e. `%USERPROFILE%\.jot` on Windows). Point separate installs or contexts at different homes to keep their data apart.

| Path | Purpose |
| --- | --- |
| `jot.db` | SQLite database (WAL). Query it with any SQLite tool, or use `jot export --format jsonl\|md` |
| `config.toml` | Backends and models, default flow, concurrency, isolation and worktree location, port, cleanup thresholds |
| `instructions/triage.md` | How notes become tasks: label vocabulary, criticality rubric |
| `instructions/drawdown.md` | How agents prioritize, refine, ask questions, plan, and execute (including whether to push and open PRs) |
| `instructions/cleanup.md` | What counts as stale or duplicate |
| `instructions/projects/<slug>.md` | Per-project guidance (repo conventions, test commands) |
| `runs/<id>.jsonl` | Full agent logs: text, thinking, tool calls, per-turn usage |
| `worktrees/<repo>/<id>-<slug>` | Git worktrees for execute runs |
| `backups/` | `jot backup` snapshots |

Every instruction file can also be edited in the UI (Instructions view).

## Capture

- **UI:** use the capture bar at the top of every view (`Ctrl+K`, then Enter). The task appears instantly and fills in when enrichment finishes.
- **CLI:** `jot add "<note>"` returns in about 100 ms. Enrichment runs in the server's background worker, or immediately with `--wait`. Pre-fill fields with `--project --label --crit --type --title --repo`.
- **Agents:** the `jot` skill (installed by `jot install-skills`) teaches Claude Code, Codex, and Copilot every Jot workflow: capture, query and groom, drawdown, and cleanup. Its `SKILL.md` routes to one reference file per workflow.

Enrichment reuses existing projects (matched by slug, name, or alias) and existing labels, so labels don't drift into synonyms. A task with clear scope moves to `ready`. An unclear one stays in `inbox`, and its open questions are recorded on the task. Set `[triage] backend` in `config.toml` to `claude`, `codex`, or `copilot`.

## Draw down

There are two flows. Choose one per run, or set a default per task, per project (`--default-flow`), or globally.

```text
planned: inbox → ready → planning → awaiting_approval → executing → review → done
direct:  inbox → ready → executing → review → done
either:  planning | executing → needs_input → (answers) → same phase again
```

Only runs move a task into `planning` or `executing`. Dragging a card there doesn't start anything, so the board doesn't offer it.

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

**Questions from the agent.** Plan and execute runs can return structured questions (`{"id", "text", "choices"?}`). The task then moves to `needs_input`, and the run's lease is released. Each question appears in the UI with its own input: radio buttons when the agent suggested choices, otherwise a text box. Answers are optional. Submitting resumes the same phase. Blank answers leave the decision to the agent. You can also send the task back to `ready` instead of answering.

**Choosing models.** Every action can use a different model. Set defaults per backend and action in `config.toml`:

```toml
[models.claude]
triage = "haiku"
plan = "opus"
execute = "sonnet"
cleanup = "default"   # "default" = the provider's default
```

Override any single run with `--model` on `jot plan`, `run`, `approve`, `enrich`, and `cleanup scan`, or with the "Plan with" and "Execute with" pickers in the UI. Each run records the model it used.

**Where agents work.** The task's `repo_path`, else the project's `repo_path`, else a scratch folder that is its own git repository. Execution in a git repo happens in a worktree at `~/.jot/worktrees/<repo>/<id>-<slug>` on branch `jot/<id>-<slug>`. Change the location with `[execution] worktree_root`. The agent commits there and stays inside the working directory. It pushes or opens a PR only if your drawdown instructions say to.

**Permissions.** Plan runs are read-only (Claude: read tools only; Codex: `--sandbox read-only`; Copilot: view/grep/glob only). Execute runs have full tool access inside the workspace.

**External agents.** The `jot` skill's drawdown workflow lets an interactive Claude, Codex, or Copilot session use the same protocol: `jot claim`, then `jot comment --kind plan|question|result`, then `jot release --status …`.

## The List view

![List view with row actions and an expanded plan](images/list.png)

Every row has one-click actions for its status: **Plan** and **Run now** (ready), **Answer** or **Approve** and **Send back** (waiting on you), **Done** and **Send back** (review), **Cancel** (running), and **Mark ready** (inbox). The "Plan with" and "Execute with" pickers above the table choose the backend and model for those buttons. Expand a row (▸) to read the latest plan, answer questions, or approve inline. Presets: Open, Needs attention, Needs input, Ready, In progress, All. Select rows to plan, run, or move several at once.

## Reading runs

![Execute run output with a Markdown table, collapsed tool steps, and token usage](images/task-run-output.png)

Open a task to see its runs and timeline:

- **Markdown:** agent messages, results, plans, questions, and comments render as Markdown. Plan runs reply with structured JSON (summary, plan, questions), shown as sections. The renderer builds DOM nodes directly and never injects HTML, so raw HTML in agent output appears as text. Links are limited to `http(s)` and `mailto`.
- **Collapsed steps:** consecutive tool calls and thinking steps fold into one block ("3 tool calls · 1 thinking"). Click to expand.
- **Usage:** after every turn, a line shows the model plus input, output, cache-read, and cache-write tokens, and each run shows its total. Claude's per-turn figures come from the API's final `message_delta` usage. Codex reports per turn (input net of cached input). The Copilot CLI reports only the model and premium requests, not tokens.
- **Timeline:** system events read as sentences ("ready → planning (claim)", "Label added: idea").

**Links and theme.** Every view and task has a shareable link: `#/list`, `#/board`, `#/runs`, and `#/board/task/12` (opens that task's panel). The browser back and forward buttons work. List is the default view. The theme button in the top bar cycles through System (follows your OS), Light, and Dark, and remembers your choice:

![Board in dark mode](images/board-dark.png)

## Find and clean up

```bash
jot ls --project orbit-api --status ready --label security --crit high
jot ls --older-than 60d --status inbox
jot ls --q "health check"          # full-text search (FTS5) over title, description, note, labels
jot cleanup scan --agent           # heuristics + LLM review using cleanup.md
jot cleanup apply 3 --approve 0,2  # only approved items; archive / soft-delete / note
```

The `jot` skill runs the same review conversationally. It also queries the queue and suggests changes to apply with your approval: reprioritize, refine, merge or split tasks, fill in missing fields, or pick the next tasks to run. Nothing changes without item-level approval.
