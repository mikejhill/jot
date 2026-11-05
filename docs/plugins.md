# Plugins

Plugins add behavior to Jot without changing the core. A plugin can react to every change, keep private state, run background jobs, store validated settings, and add UI. Jot ships one plugin, `rules`. AI features such as triage and execution are planned as plugins too.

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
