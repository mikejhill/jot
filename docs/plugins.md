# Plugins

Plugins add behavior to Jot without changing the core. A plugin can react to every change, keep private state, run background jobs, store validated settings, and add UI. Jot ships `rules`, the `harness` family (one interface over coding-agent CLIs), `triage`, which tidies new jots through the harness, and `execution`, which lets agents claim and hand back work (its runner is in progress).

## Using plugins

```bash
jot plugin ls                       # installed plugins, enabled state, lag, last error
jot plugin enable rules             # starts after the newest event (--from-start replays history)
jot plugin config rules             # print settings
jot plugin config rules --set '{"rules": [...]}'
jot plugin config rules --file rules.json   # or --file - for stdin
jot plugin failures rules           # events the plugin gave up on
jot plugin disable rules
jot plugin run                      # foreground worker (--once for one pass)
jot job ls --status failed          # also: jot job cancel <id>, jot job retry <id>
```

Plugins run in a worker. `jot serve` starts one automatically (`--no-worker` turns it off). Without the server, run `jot plugin run`. Several workers can run at once safely.

The web UI's Settings page lists plugins. From there you can enable or disable them, edit their settings as JSON, see lag and errors, and cancel or retry recent jobs.

Enablement and settings changes are recorded as `plugin.enabled`, `plugin.disabled`, and `plugin.configured` events.

A profile's `plugins` list limits which plugins act on its jots. An empty list (the default) allows every enabled plugin.

## Rules

`rules` applies deterministic rules as jots change. Each rule lists the event kinds it listens to (`jot.created` by default) and is evaluated against the jot's current state:

```json
{
  "rules": [
    {
      "name": "invoices",
      "when": {"text": "invoice", "profiles": ["note"]},
      "then": {"add_labels": ["finance"], "project": "admin"}
    },
    {
      "name": "start bugs",
      "when": {"kinds": ["jot.created", "jot.updated"], "pattern": "^Fix\\b", "categories": ["open"]},
      "then": {"status": "doing", "comment": "Picked up by rule"}
    }
  ],
  "ignore_plugins": true
}
```

| `when` key | Matches when |
| --- | --- |
| `kinds` | the event kind is listed (required, default `["jot.created"]`) |
| `profiles`, `categories`, `statuses` | the jot's value is listed (empty means any) |
| `labels` / `without_labels` | the jot has all of / none of these labels |
| `project` | the jot is in this project |
| `text` | title or body contains this text (case-insensitive) |
| `pattern` | title or body matches this regular expression |

| `then` key | Effect |
| --- | --- |
| `add_labels`, `remove_labels` | change labels |
| `status` | move to this status (ignored if the jot's profile lacks it) |
| `project` | file under a project (`""` clears it) |
| `fields` | set profile fields |
| `comment` | add a comment of kind `rules.note` |

Only differences are written, so a rule that already holds changes nothing. Writes are attributed to `plugin:rules` with the reason `rule: <name>`. A rule's own writes never re-trigger it. With `ignore_plugins` (the default), changes made by other plugins don't trigger rules either.

## Harness

The harness plugins run AI requests through a coding-agent CLI you have already installed and signed in to. Jot stores no API keys. `harness` is a router that provides the `harness@1` service; each backend plugin provides `harness.backend@1` and needs the router:

```bash
jot plugin enable harness           # first: backends require it
jot plugin enable harness-claude    # also harness-codex, harness-copilot, harness-fake
jot harness ls                      # enabled backends, availability, permission flags
jot harness check claude            # one tiny structured call, end to end
```

| Backend | Program | Usage reported |
| --- | --- | --- |
| `harness-claude` | `claude -p --output-format stream-json` | tokens and cost |
| `harness-codex` | `codex exec --json` | tokens (input net of cache) |
| `harness-copilot` | `copilot -p --output-format json` | premium requests; tokens estimated |
| `harness-fake` | none (offline, deterministic) | fixed |

Backend settings (`jot plugin config harness-claude --set ...`): `command` (program and fixed arguments, default the CLI's name on `PATH`), `model`, `extra_args`. The fake takes `canned` (values merged into every answer), `reply`, and `fail`.

### Modes

- **Structured**: a tool-less prompt answered with one JSON object matching a JSON Schema. It runs in a throwaway folder with each CLI's lean options (no session saved, no MCP, no project instructions) and checks the schema's required keys.
- **Run**: an agentic session in a working directory, streamed as `text`, `thinking`, `tool`, `usage`, and `error` events. The last event is always `result`, with the session id for resuming.

### Permissions

Every run asks for one of three levels. Each backend maps them to its own flags explicitly (shown by `jot harness ls`):

| Level | Claude Code | Codex | Copilot |
| --- | --- | --- | --- |
| `read_only` | `dontAsk`, tools Read, Grep, Glob, WebSearch, WebFetch | `--sandbox read-only` | tools view, glob, grep, web_fetch |
| `workspace_write` | `dontAsk`, the above plus Edit, Write, MultiEdit | `--sandbox workspace-write` | the above plus edit, create; `--allow-all-tools` |
| `full` | `bypassPermissions` | `--dangerously-bypass-approvals-and-sandbox` | `--allow-all-tools` |

No level grants a shell except `full`. Use `full` only in isolated workspaces.

### Routing and policy

`jot plugin config harness --set '{...}'`:

```json
{
  "default": "claude",
  "routes": {"triage": {"harness": "codex", "model": "gpt-5-mini"}},
  "allowed_models": [],
  "blocked_models": ["some-expensive-model"],
  "max_permission": "workspace_write"
}
```

A request's backend is, in order: the one the caller names, the action's route, `default`, then the first enabled backend whose CLI is installed. The model is the caller's, else the route's, else the CLI's own default. Blocked models are refused; when `allowed_models` is set, any explicit model must be listed. Runs above `max_permission` (default `workspace_write`) are refused, so `full` must be opted into.

### Using it from a plugin

```python
from jot.plugins.harness import SERVICE, HarnessService
from jot.plugins.harness.api import StructuredRequest


class TriagePlugin(Plugin):
    requires = frozenset({SERVICE})

    def run(self, job, context):
        harness = context.service(SERVICE)
        result = harness.structured(
            StructuredRequest(prompt=..., output_schema=SCHEMA), action="triage"
        )
        return result.value
```

## Triage

`triage` tidies each new jot with the harness (action `triage`). Capture never waits for it: the jot is saved at once, labelled `triaging`, and a background job does the rest.

```bash
jot plugin enable harness && jot plugin enable harness-claude && jot plugin enable triage
jot add "safari login times out, maybe the cookie"   # triaged by the worker
jot triage run 12          # queue again
jot triage run 12 --now    # triage here and wait
```

The model proposes a title, a Markdown body, a project, labels, values for empty profile fields, a confidence, and questions. Triage then writes only what is safe, in one change attributed to `plugin:triage`:

| Proposal | Applied when |
| --- | --- |
| title | `rewrite_title` is on and you haven't edited the title since capture (the original is kept in `metadata.triage.original_title`) |
| body | the jot has no body and you haven't edited it |
| project | the jot has none; an existing slug, or a new project only with `create_projects` |
| labels | always added (normalized, at most `max_labels`, existing labels kept) |
| profile fields | the field is declared, still empty, and you haven't edited fields |
| questions | each becomes a `triage.question` comment |

The jot ends up labelled `triaged`, plus `needs-input` when confidence is below `min_confidence` or there are questions. `metadata.triage` records the confidence, questions, and model. If the harness keeps failing (`max_attempts`, default 3), the jot is labelled `triage-failed` with a `triage.note` comment, and is otherwise left as captured.

Settings: `profiles` (which profiles to triage, empty means all), `guidance` (instructions to the model), `min_confidence` (0.6), `rewrite_title` (true), `create_projects` (false), `max_labels` (5), `max_attempts` (3). Pick the backend and model with a harness route: `{"routes": {"triage": {"harness": "claude", "model": "..."}}}`.

## Execution

`execution` lets agents work jots safely: external agents in their own sessions use claims, and Jot's own runner plans and executes jots through the harness. See the [design](https://claude.ai/artifact/Hz6i9sk6T5gPLSLgWw3gMm) for the full plan; this release ships claims and the runner (the autonomy contract, UI, and drawdown come next).

### Claims (external agents)

```bash
jot plugin enable execution
jot work next -n 5                               # ready jots in rank order
jot work claim 12 --agent bot-1 [--direct]       # exit 3 if another agent holds it
jot work heartbeat 12 --agent bot-1              # extend the lease
jot work release 12 --agent bot-1 --phase awaiting_approval --note "plan..."
jot work approve 12                              # owner: let the plan run
jot work state 12
```

A claim is stored in `metadata.execution` with the agent, lease expiry, and a generation that rises when another agent takes over an expired claim. Claims are written in one serialized transaction with a version check, so two agents racing for a jot get one winner. The default lease is 30 minutes (`lease_seconds`).

| Phase | Label | Status category | Who acts |
| --- | --- | --- | --- |
| idle | | open | an agent may claim |
| planning, executing | | active | the claiming agent |
| needs_input | `needs-input` | active | owner answers |
| awaiting_approval | `awaiting-approval` | active | owner runs `jot work approve` |
| approved | `approved` | active | an agent may claim (goes to executing) |
| review | `review` | active | owner accepts or sends back |
| done | | done | |

`jot work next` lists jots that are idle, approved, or running with an expired lease, not blocked by an open jot through a `blocks` link, and in a profile that allows `execution`. Jots imported from v0.1 with `review`, `awaiting-approval` or `needs-input` labels start in those phases.

### The runner

```bash
jot plugin enable harness && jot plugin enable harness-claude
jot work repos --set web=~/src/web               # project -> repository (or --from-import for v0.1 paths)
jot work repos --default ~/src/scratch           # repository for jots without a mapped project
jot work run 12 [--direct] [--now]               # plan (or execute directly); --now waits here
jot work answer 12 <question comment id> "text"  # resumes once every question is answered
jot work approve 12                              # runner plans queue execution
jot work send-back 12 "smaller change please"    # re-plan a plan, or rework a result
jot work retry 12 ; jot work cancel 12
jot work log 12                                  # the latest run's events
jot work clean 12 [--force]                      # remove the worktree; the branch stays
```

Without `--now`, runs are plugin jobs, picked up by `jot serve` or `jot plugin run`.

- **Plan:** the agent runs at `read_only` in the repository (action `execution.plan`) and answers with a summary, a plan, and questions. Questions become `execution.question` comments and the jot moves to `needs-input`; otherwise the plan is an `execution.plan` comment, its revision (plan comment plus a hash of the title and body) is stored, and the jot moves to `awaiting-approval`.
- **Execute:** the agent runs in a git worktree at `<worktree_root>/<repo>/<id>-<slug>` (default root `<JOT_HOME>/worktrees`) on branch `jot/<id>-<slug>`, created from the repository's HEAD and reused across attempts (action `execution.run`). It runs at `workspace_write`, or `full` (with a shell, so it can run tests) for projects listed in `allow_shell`; the harness router's `max_permission` must allow that. Changes to tracked files it leaves uncommitted are committed for it; untracked files are listed in the result, not committed. The `execution.result` comment carries the outcome, verification, branch, commits, and diffstat, and the jot moves to `review`. Rework resumes the same agent session when the harness supports it.
- **Failures:** harness and git errors retry with backoff (3 attempts); the last failure moves the jot to `needs-input` with an `execution.note`. A job whose claim was lost or cancelled stops without writing.
- **Logs:** every event of a run is written to `<JOT_HOME>/plugins/execution/runs/<jot>-<phase>-<job>-<time>.jsonl`.
- **Settings:** `repos`, `default_repo`, `worktree_root`, `allow_shell`, `plan_guidance`, `execute_guidance`, `lease_seconds`. Nothing is pushed or published; the guidance tells the agent so.

## Writing a plugin

A plugin is a `jot.plugins.api.Plugin` subclass published under the `jot.plugins` entry-point group:

```toml
# pyproject.toml of your package
[project.entry-points."jot.plugins"]
digest = "jot_digest:DigestPlugin"
```

```python
from typing import override

from pydantic import BaseModel

from jot.plugins.api import Job, Plugin


class DigestSettings(BaseModel):
    label: str = "digest"


class DigestPlugin(Plugin):
    id = "digest"  # lowercase, [a-z][a-z0-9_.-]*
    name = "Digest"
    version = "1"
    description = "Queues a summary job for labelled jots."
    events = frozenset({"jot.created", "jot.updated"})  # empty = every kind
    settings_model = DigestSettings

    @override
    def handle(self, event, context):
        jot = context.queries.get(event.jot_id)
        if context.settings.label in jot.labels:
            context.enqueue("summarize", jot_id=jot.id, dedupe_key=f"jot-{jot.id}")

    @override
    def run(self, job: Job, context):
        context.heartbeat()  # extend the lease during long work
        context.commands("digest").update(job.jot_id, ...)
        return {"ok": True}  # stored as the job's result
```

### Contract

| Piece | Behavior |
| --- | --- |
| `handle(event, context)` | Called once per event, in order. It runs inside the transaction that advances the plugin's cursor, so its writes and its progress commit together. Keep it fast and put slow work in jobs. |
| Failures | If `handle` raises, its writes roll back and the cursor stays. Delivery backs off (2 s, 4 s, … up to 5 min). After 5 failures the event is recorded (`jot plugin failures`) and skipped. |
| Filtering | Events from the plugin's own actor are skipped (`skip_own`). So are events for jots whose profile excludes the plugin, and events where `applies_to(jot)` is false. By default `applies_to` excludes deleted jots. |
| `run(job, context)` | Runs outside any transaction under a lease (60 s by default; `heartbeat()` extends it). An exception retries the job with backoff until `max_attempts`, then marks it failed. A run that loses its lease cannot record a result. |
| `context.commands(reason)` | Normal `Commands`, attributed to `plugin:<id>` with origin `plugin`. They use the triggering event's or job's correlation id. Use `base_version` to avoid overwriting newer user edits. |
| `context.state` | Private JSON key-value storage (`get`, `set`, `keys`; setting `None` deletes). |
| `context.enqueue(kind, payload, jot_id=, delay=, dedupe_key=, max_attempts=)` | Queue a job. A queued or running job with the same `dedupe_key` is reused. |
| `context.settings` | An instance of `settings_model`, validated on save. |
| Metadata | Store plugin data on jots under a metadata namespace named after the plugin (`metadata.<id>`). |
| Services | `provides` and `requires` list service names with a major version (`harness@1`). A plugin can't be enabled until every required service has an enabled provider, and a provider can't be disabled while an enabled plugin depends on it. `service(name, context)` returns the provided object; consumers call `context.service(name)` (the first enabled provider by id) or `context.services(name)` (all of them). |
| `listens` | `False` skips the event feed entirely, for plugins that only provide services. |
| `commands(cli)` | Return a Typer app to mount as `jot <id> ...` (or `jot <command> ...` when the class sets `command`). `cli.host(json=...)` opens the data home with a plugin host; `cli.emit(value, text)` prints JSON or text. |
| `api` | Targets host API `1`. A mismatch makes the plugin show as broken. |

### UI

`ui_module()` can return the source of an ES module. The UI loads it from `/api/plugins/<id>/ui.js` when the plugin is enabled. If it exports `default function setup(jot)`, the UI calls it with the extension API: `jot.register(slot, item)`, `jot.html`, `jot.h`, `jot.Component`, and `jot.api.request(path, method, body)`. See [Architecture](architecture.md#http-api-and-web-ui) for the slots. A module stays loaded until the page reloads.

### Testing

Build a catalog from your classes and drive the host directly, with a fake clock:

```python
host = PluginHost(session, PluginCatalog.of(DigestPlugin), clock=lambda: 0.0)
host.enable("digest", WriteContext(actor="test"))
host.tick()
```
