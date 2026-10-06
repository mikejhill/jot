# Design: agent settings (harnesses, models, pins, loadouts)

Covers Jot tasks #11 (backend and model for every action, pins, "auto"), #13 (harnesses as configurable plugins, with a Settings page), and #21 (choosing which skills, MCP connectors, and plugins load in runs). Status: **approved 2026-10-06** (decisions below).

## Goals

1. Every agent action lets you pick a **harness** and a **model**, with sensible defaults and one-click **pins**. Actions: triage on capture, plan, execute, answer/resume, cleanup, and the upcoming "ask an agent" (#22).
2. Harnesses are **configured, not coded**: enable or disable them, restrict models, add per-phase instructions, and choose what loads in a run (the "loadout").
3. Everything is editable in a **Settings** page and stays plain text in `config.toml`.

## Concepts

| Term | Meaning |
| --- | --- |
| **Harness** | A configured agent runtime: an instance of a harness *kind* (`claude-sdk`, `codex-cli`, `copilot-cli`, `fake`). You can define several of the same kind, e.g. "Claude (lean)" and "Claude (full context)". |
| **Action** | `triage`, `plan`, `execute`, `cleanup`, `assist` (#22). Answer/resume reuses the phase it resumes. |
| **Model policy** | Per harness: `allowed` (if set, only these), `disallowed` (always wins), plus a default model per action. |
| **Loadout** | What loads into a run: skills, MCP connectors, plugins, and custom instructions or project docs. Set per harness and per action, optionally overridden per project. |
| **Pin** | A named `{harness, model}` shortcut shown as a chip next to pickers. Pins are global, with an optional action filter. |
| **Auto** | A picker choice that resolves to a harness and model at run time. See the open question below. |

## Configuration (`config.toml`)

Existing `[triage]`, `[drawdown]`, and `[models.<backend>]` keep working; they map onto the defaults below.

```toml
[harnesses.claude]            # id = "claude"
kind = "claude-sdk"
label = "Claude"
enabled = true
models.allowed = []           # empty = any model not disallowed
models.disallowed = ["opus-4"]
models.default = { triage = "haiku", plan = "opus", execute = "sonnet", cleanup = "haiku", assist = "sonnet" }
instructions = { plan = "Prefer small, reviewable steps.", execute = "" }   # appended per phase

[harnesses.claude.loadout.triage]     # lean by default (current behavior)
skills = []                   # [] = none, "all", or a list of names
mcp = []                      # [] = none (strict), "all", or connector names
plugins = []
[harnesses.claude.loadout.execute]
skills = ["jot", "python-development"]
mcp = []
project_instructions = true   # CLAUDE.md / AGENTS.md from the repo

[harnesses.codex]
kind = "codex-cli"
models.default = { triage = "gpt-6-luna", execute = "gpt-6-astra" }
config = { disable_features = ["browser_use", "image_generation"] }

[actions]                     # which harness each action uses by default
triage = "codex"
plan = "claude"
execute = "claude"
cleanup = "codex"
assist = "claude"

[[pins]]
label = "Opus plan"
harness = "claude"
model = "opus"
actions = ["plan"]
```

**Resolution order for a run:** the explicit pick (UI, CLI, or pin), then `[actions]` with the harness's `models.default`. Choosing **Auto** runs the LLM router (see Decisions). A model that violates the harness policy is rejected with a clear error.

Settings writes `config.toml` with `tomlkit`, so comments and formatting survive edits.

## Loadout per harness kind

| Kind | Skills | MCP | Plugins | Instructions |
| --- | --- | --- | --- | --- |
| `claude-sdk` | `skills=[]`, a list, or `"all"` | `--strict-mcp-config` plus `mcp_servers` subset | `plugins` | `setting_sources` (project and user) |
| `codex-cli` | `--disable` features; `skills` folder toggle | config `mcp_servers` subset | n/a | `project_doc_max_bytes` |
| `copilot-cli` | n/a (loaded from `~/.copilot`) | `--disable-builtin-mcps`, `--disable-mcp-server` | `--plugin-dir` | `--no-custom-instructions` |

A **Discover** button runs one cheap session per harness and lists what's actually available: Claude's init message reports skills, MCP servers, and plugins; Copilot reports skills and MCP. The Settings page then shows real checkboxes, not free text.

## UI

- **Settings** (new nav entry, route `#/settings`):
  - **Harnesses:** a card per harness with enable, model allow and deny chips, default model per action, per-phase instructions, and loadout checkboxes per action from Discover.
  - **Actions:** the default harness per action.
  - **Pins:** add, reorder, and remove, with an action filter.
- **Pickers:** one component everywhere: harness select, model combobox (filtered by policy), an "Auto" entry, and pin chips. The selection is remembered per action.
  - **Capture bar:** a compact `⚙ triage: codex / luna` chip opens the picker, so capture stays one field plus Enter.
  - **Drawer and List:** "Plan with" and "Execute with" use the same component.
- **Runs** record the harness id, model, and a loadout summary, shown on the run card.

## CLI

`--harness` (alias of today's `--backend`) and `--model` on every agent command. `jot harness ls | show <id> | discover <id>`. `jot pins ls`.

## Implementation slices

1. **Config model and resolution:** `HarnessConfig`, `[actions]`, pins, and model policy, with backward compatibility for `[models.*]`. API: `GET/PUT /api/settings`, via tomlkit.
2. **Loadouts in the backends:** apply skills, MCP, plugins, and instructions per action. Discover endpoint.
3. **Picker component plus pins** in the capture bar, drawer, and List. Auto.
4. **Settings page:** forms, validation, Discover-driven checkboxes. Browser tests and screenshots.
5. **Docs** (user guide, architecture).

Each slice ships with unit, UI, and browser tests.

## Decisions (owner, 2026-10-06)

1. **Auto = LLM router.** When a picker is set to Auto, Jot makes one lean structured call (harness and model from `[actions] router`, default the triage harness) with:
   - the task (title, type, criticality, labels, description excerpt),
   - the action, and
   - the candidates: enabled harnesses × allowed models, plus pins.

   Routing guidance comes from a new editable `instructions/routing.md` (e.g. "use opus for critical planning, luna for chores"). The router returns `{harness, model, reason}`. Jot validates the choice against model policy and falls back to the action default on failure. The choice and reason are recorded on the run (a `routing` event) and shown on the run card, and router usage is logged like any other call.
2. **Lean loadouts by default.** Plan, execute, and assist runs load no account connectors, skills, or plugins unless enabled per harness and action. The repo's project instructions (CLAUDE.md / AGENTS.md) stay on. Triage, cleanup, and the router stay fully lean.
3. **Pins are global** with an optional `actions` filter.
4. **No project overrides** for now. Harness and loadout come from the harness and action configuration only. The `projects` config above is out of scope.
