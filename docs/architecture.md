# Architecture

Jot is an information system with optional automation. The acceptance test for the core is that you can capture and manage jots with no model credentials, no harness installed, and no automation running.

## Layers

```text
plugins             rules · harness + harness-claude/codex/copilot/fake · triage · execution claims, runner, autonomy contract, UI (shipped) · drawdown (planned)
                         │ use only the plugin API: commands, queries, event feed, jobs, state, UI slots
host                jot.plugins  discovery, durable delivery, jobs, settings, state, worker
core                jot.core     records, profiles, rank            (pure, no I/O)
                    jot.db       SQLite connection, schema, migrations, row mapping
                    jot.app      Commands (writes), Queries (reads), Session
interfaces          jot.cli      Typer CLI
                    jot.server   FastAPI HTTP API + static web UI
```

`jot.core` imports nothing from the other packages. Every interface goes through `jot.app`, so the CLI, the HTTP API, the UI, and plugins share one set of validated commands.

## Code layout

```text
src/jot/
  core/models.py     Jot, Project, Comment, Link, Event, WriteContext, StatusCategory
  core/profiles.py   Profile, StatusSpec, FieldSpec, ProfileRegistry, built-ins (note, task)
  core/rank.py       fractional base-36 rank keys
  db/                connection (WAL, transactions), schema.sql, migrations, Store
  app/commands.py    every write: one transaction, one event, version bump
  app/queries.py     filters, full-text search, detail, event feed
  app/session.py     opens a home + config + database
  plugins/api.py     Plugin base class, Job, API_VERSION (the plugin contract)
  plugins/host.py    PluginCatalog (entry points), PluginHost (admin, delivery, jobs), Worker
  plugins/context.py PluginContext and PluginState handed to plugin code
  plugins/store.py   SQL for plugin rows, failures, state, and jobs
  plugins/rules/     the built-in rules plugin and its UI module
  cli/               commands and text/JSON output (cli/plugins.py: plugin and job groups)
  server/            FastAPI app, request schemas, uvicorn launcher
  static/            web UI (Preact + htm, no build step)
  skills/jot/        agent skill for driving the CLI
```

## Data model

| Table | Holds |
| --- | --- |
| `jots` | title, body, profile, status, category (denormalized from the profile), project, rank, metadata JSON, version, created_by, timestamps (archived and deleted are soft) |
| `projects` | slug, name, description, archived |
| `labels`, `jot_labels` | label names and assignments |
| `comments` | author, body, kind (`comment` or a plugin kind such as `execution.question`), reply_to, edited/deleted timestamps |
| `links` | src, dst, rel (`parent`, `relates`, `blocks`, `duplicates`, or a dotted plugin relation) |
| `events` | append-only history: kind, jot, version, actor, origin, correlation_id, reason, data |
| `plugins` | per-plugin enabled flag, settings JSON, event cursor, failure count, retry time, last error |
| `plugin_failures` | events a plugin skipped after repeated errors |
| `plugin_state` | private key-value JSON per plugin |
| `jobs` | plugin, kind, jot, payload, dedupe key, status, attempts, lease, result or error |
| `jots_fts` | FTS5 over title, body, and live comments, kept in sync by triggers |

Metadata namespaces: `fields` holds profile fields, validated and typed by the profile. Every other top-level key is a namespace owned by the user (`user`) or by a plugin (`triage`, `execution`). Namespaces merge key by key, and `null` removes one.

## Profiles

A profile declares statuses, an initial status, optional transitions (an empty list allows any move), typed fields (`string`, `number`, `boolean`, `date`, `enum`), and applicable plugins. Profiles never run code. Each status has a category (`open`, `active`, `done`, `cancelled`), so queries and agents can work across profiles. Changing a jot's profile maps its status by id, then by category, then to the initial status.

## Writes, versions, and events

`Commands` performs every write in one `BEGIN IMMEDIATE` transaction that changes the rows and appends an event. Each change to a jot bumps `version`, and the event records `{"changes": {field: {"from", "to"}}}`.

- `expected_version`: fail with `ConflictError` (CLI exit 3) if the jot changed at all.
- `base_version`: fail only if a field being written changed after that version. Metadata is tracked per namespace (`metadata.triage`). This lets a late plugin write its own namespace without overwriting a user's newer edits.
- `WriteContext` (actor, origin, correlation_id, reason) is stored on every event. Plugins will use `origin` and `actor` to skip their own changes.

The `events` table is also the change feed (`Queries.events(after=id)`, `jot events --after`, `GET /api/events`). Writers serialize on `BEGIN IMMEDIATE`, so event ids commit in increasing order and a cursor never skips one.

## Plugin host

The host delivers the feed to each enabled plugin with its own cursor. Each event is handled inside the write transaction that advances the cursor, so a plugin's writes and its progress commit or roll back together. The plugin's database effects happen exactly once; anything else it does (a job, a call to another system) is at-least-once. Failures back off exponentially, and after five tries the event goes to `plugin_failures` and is skipped. Slow work goes into jobs, which run outside transactions under renewable leases, with retries and dedupe keys.

Plugins never touch tables directly. They get a `PluginContext` with attributed commands, queries, settings, private state, and job control. See [Plugins](plugins.md) for the contract and the rules plugin.

## HTTP API and web UI

`jot.server` is a thin FastAPI layer over the same `Session`, `Commands`, and `Queries` the CLI uses. Each request opens its own session, so the server holds no state beyond the data home. Write context comes from `X-Jot-*` headers. `AppError` subclasses map to 404, 409, and 422.

The UI (`src/jot/static/`) is Preact plus htm with no build step. It follows the change feed by polling `/api/events`. Extensions register UI through `window.jot.register(slot, item)`:

| Slot | Item |
| --- | --- |
| `views` | `{id, label, render}`: a top-level view next to List and Board |
| `jot.badges` | `({jot}) => vnode`: shown on list rows and cards |
| `jot.actions` | `({jot, refresh}) => vnode`: buttons in the drawer header |
| `jot.panels` | `({detail, refresh}) => vnode`: sections in the drawer |
| `settings.pages` | `{id, label, render}`: sections on the settings page |

The plugin host serves each enabled plugin's UI module at `/api/plugins/<id>/ui.js`. The UI imports it at startup, and it calls this registry through `setup(jot)`. Modules call back into their plugin through `POST /api/plugins/<id>/actions/<name>` (see [Plugins](plugins.md#actions)).

## Roadmap

| Stage | Scope |
| --- | --- |
| 1 | Core model, profiles, command API, CLI, skill |
| 2 | HTTP API and a web UI shell with extension slots |
| 3 | Plugin host: entry points, durable event delivery, jobs, plugin storage and settings, UI slots; first plugin is deterministic (`rules`) |
| 4 | v0.1 importer (`jot import v01`) |
| 5 | Harness contract plugin plus Claude, Codex, Copilot, and fake implementations; plugin services (`provides`/`requires`) and plugin CLI commands |
| 6 | Triage plugin (`triaging` → `triaged`, never overwriting newer user edits) |
| 7 | Cutover from v0.1 |
| 8 (8a to 8d in this release) | Execution plugin ([design](https://claude.ai/artifact/Hz6i9sk6T5gPLSLgWw3gMm)): 8a claims, 8b runner, 8c autonomy contract, 8d UI and HTTP actions, 8e drawdown |
