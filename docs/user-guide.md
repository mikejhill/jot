# User guide

## Capture

```bash
jot add "Call the vendor about renewal"            # one line -> title
jot add -t "Q3 planning" --stdin < notes.md        # title + markdown body
jot add -p task -P work -l finance -f due=2026-12-01 "Renew vendor contract"
```

A jot needs only text. Projects, labels, profile, status, and fields are optional. New jots go to the bottom of the rank order.

## Find

```bash
jot ls                                  # live jots in rank order (archived/deleted hidden)
jot ls -c open -c active -n 10          # by status category, across profiles
jot ls -p task -s doing                 # by profile and status
jot ls -P work -l finance -q renew      # project, label, full-text (title, body, comments)
jot ls --archived --sort updated --json
jot show 12 --with comments,links,history
jot history 12
```

## Organize

| Change | Command |
| --- | --- |
| Status | `jot status 12 done` |
| Title, body, project, profile | `jot edit 12 -t "New title" -b "Body" -P work -p task` (`-P ''` clears the project) |
| Fields | `jot edit 12 -f due=2026-12-01` (`-f due=` removes it) |
| Metadata | `jot edit 12 -m user='{"source": "phone"}'` (`-m user=` removes the namespace) |
| Labels | `jot label 12 -a urgent -r someday` |
| Rank | `jot rank 12 --top`, `--bottom`, `--before 7`, `--after 7` |
| Comments | `jot comment 12 "text"`, `jot comment-edit 3 "text"`, `jot comment-rm 3` |
| Links | `jot link 3 12 --rel parent` (3 is the parent of 12), `jot unlink 3 12 --rel parent` |
| Archive, delete | `jot archive 12`, `jot unarchive 12`, `jot rm 12`, `jot restore 12` |
| Projects | `jot project add work "Work"`, `jot project ls`, `jot project edit work --archive` |

Deleting is soft, and history is always kept. Edited and deleted comment text stays in history.

## Profiles

`jot profile ls` shows the available profiles. Built-ins:

| Profile | Statuses (category) |
| --- | --- |
| `note` (default) | open (open), done (done) |
| `task` | todo (open), doing (active), done (done), wont_do (cancelled) |

Define your own in `$JOT_HOME/config.toml`:

```toml
default_profile = "note"

[profiles.bug]
initial = "new"
statuses = [
  { id = "new", category = "open" },
  { id = "fixing", category = "active" },
  { id = "fixed", category = "done" },
  { id = "invalid", category = "cancelled" },
]
transitions = [["new", "fixing"], ["fixing", "fixed"], ["new", "invalid"]]   # [] = any move

[profiles.bug.fields.severity]
type = "enum"            # string, number, boolean, date, enum
options = ["low", "high"]
required = false
```

Redefining `note` or `task` overrides the built-in. When a jot changes profile, its status keeps the same id if possible, otherwise the first status in the same category, otherwise the initial status.

## Automation conventions

Scripts and agents should:

- Read with `--json`.
- Identify themselves with `--actor agent:<name>` (or `JOT_ACTOR`) and give a `--reason`.
- Write with `--if-version N` (fail on any change) or `--base-version N` (fail only if a field being written changed). A conflict exits 3.
- Group related writes with `JOT_CORRELATION_ID`.
- Follow changes with `jot events --after <id> --json`.

Exit codes: 0 ok, 1 invalid input or config, 2 not found, 3 conflict.

Install the agent skill with `jot install-skills --target claude` (also `codex`, `copilot`, or `--dest <folder>`).

## Web UI and HTTP API

```bash
jot serve                    # http://127.0.0.1:8765
jot serve --port 9000 --open # and open a browser
```

The UI has a capture box, filters (search, category, profile, project, label, archived), a list and a board grouped by status category, a detail drawer (title, body, status, profile, project, fields, labels, comments, links, history), and settings. Other clients' changes appear within about 3 seconds. Concurrent edits to the same jot show a conflict notice instead of overwriting.

The API lives under `/api` (interactive docs at `/api/docs`). Writes accept these headers:

| Header | Meaning |
| --- | --- |
| `X-Jot-Actor` | who is writing (default `user`) |
| `X-Jot-Origin` | `api` (default) or `ui` |
| `X-Jot-Reason` | free-text reason stored on the event |
| `X-Jot-Correlation-Id` | groups related writes |

Status codes: 404 not found, 409 conflict, 422 invalid input. `PATCH /api/jots/{id}` accepts `expected_version` and `base_version` like the CLI.

## Plugins

Plugins add automation such as rules. They are off until you enable them:

```bash
jot plugin ls
jot plugin enable rules
jot plugin config rules --file rules.json
```

`jot serve` runs enabled plugins in the background, or you can use `jot plugin run`. See [Plugins](plugins.md).

## Data home

`$JOT_HOME` (default `~/.jot`) holds `config.toml` and `jot.db` (SQLite, WAL). v0.2 refuses to open a v0.1 database; import it instead (below).

## Moving from v0.1

The importer reads a v0.1 `jot.db` without changing it and fills an empty v0.2 home. Stop any v0.1 server or agents first so the file is not mid-write.

```bash
JOT_HOME=~/.jot-v2 jot import v01 ~/.jot/jot.db --dry-run   # report only
JOT_HOME=~/.jot-v2 jot import v01 ~/.jot/jot.db
```

| v0.1 | v0.2 |
| --- | --- |
| task (same id, so `#26` stays `#26`) | jot with the `task` profile (`--profile` to choose another with todo/doing/done/wont_do) |
| inbox, ready | `todo` (inbox tasks still needing enrichment get the label `triaging`) |
| planning, needs_input, awaiting_approval, executing, review | `doing` plus a label of the old state (`needs-input`, `awaiting-approval`, …) |
| blocked | `todo` plus the label `blocked` |
| done, wont_do | `done`, `wont_do` |
| archived | `todo`, archived |
| type (other than idea) | a label (`bug`, `feature`, …) |
| labels | labels, lowercased with dashes (renames are listed in the report) |
| comment, plan, question, answer, approval, result events | comments with the original author and time (`execution.plan`, `execution.question`, …) |
| parent | a `parent` link |
| criticality, flow, due date, repo path, raw input, lease, completion time | the jot's `v01` metadata namespace |
| full event log, runs (cost, tokens, model) | the `jot.imported` event's data |
| projects | projects; repo path, aliases, and priority go in a `project.imported` event |
| cleanup proposals | not imported (counted in the report) |

Open work is ranked first, by criticality and then age; finished work goes last. The import runs in one transaction, so it either completes or changes nothing. It refuses a home that already has jots.
