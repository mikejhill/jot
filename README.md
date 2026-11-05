# jot

[![CI](https://github.com/mikejhill/jot/actions/workflows/ci.yml/badge.svg)](https://github.com/mikejhill/jot/actions/workflows/ci.yml)
![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Jot captures, organizes, and retrieves bite-sized information. A "jot" can be a one-line idea or a thorough note. Each one has a profile, a status, a project, labels, a rank, comments, links, history, and free-form metadata. Everything runs locally on SQLite.

Jot's core contains no AI. Automation comes from plugins (see [Plugins](docs/plugins.md)). The built-in `rules` plugin is deterministic. The `harness` plugins run requests through the Claude Code, Codex, or Copilot CLI you already use, with explicit permission levels; LLM triage and agent execution are planned on top of them. Coding agents can already drive Jot through its CLI and the bundled skill.

> **Status:** v0.2 is a rewrite. This release ships the information core, CLI, HTTP API, web UI, and plugin host with a deterministic `rules` plugin. AI plugins follow. The previous agent-drawdown tracker is v0.1.0 (commit `0fae7f4`).

```text
$ jot add "Optimize the sales report model for new global channels" -l sales
1
$ jot add -p task -P jot -t "Ship the plugin host" --stdin < notes.md
2
$ jot ls
    1  note:open       Optimize the sales report model for new global channels #sales
    2  task:todo       Ship the plugin host @jot
$ jot status 2 doing --reason "starting today"
```

## Highlights

- **Fast capture:** a one-line `jot add` becomes the title, and longer text becomes the body. No project, labels, or profile are required.
- **Profiles:** declare statuses (each with a category: open, active, done, cancelled), optional transition rules, and typed fields in `config.toml`. The built-in profiles are `note` and `task`.
- **Rank:** one global order that is separate from urgency. Use `jot rank <id> --top`, `--before <id>`, and so on.
- **Safe for automation:** every write records its actor, origin, correlation ID, and reason. `--if-version` and `--base-version` keep late writers from overwriting newer edits.
- **Web UI and API:** `jot serve` opens a list, board, and detail drawer in the browser over a JSON API (`/api/docs`). Plugins add views, badges, actions, panels, and settings pages through UI slots.
- **Plugins:** entry-point plugins get the change feed in order, exactly once for their database writes, and can run background jobs with retries, keep private state, take validated settings, and add UI.
- **Agent-ready:** every command accepts `--json`, and exit codes are distinct (2 not found, 3 conflict). Install the skill with `jot install-skills`.

## Quick start

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/mikejhill/jot
jot add "my first jot"
jot install-skills --target claude --target codex --target copilot
```

Data lives in `~/.jot` by default, and `JOT_HOME` overrides it. A v0.1 database cannot be opened by v0.2; bring it over with `jot import v01` (see [Moving from v0.1](docs/user-guide.md#moving-from-v01)).

## Documentation

| Guide | What's inside |
| --- | --- |
| [User guide](docs/user-guide.md) | Commands, profiles, fields, metadata, ranking, automation conventions |
| [Plugins](docs/plugins.md) | Using plugins, the rules plugin, writing your own |
| [Architecture](docs/architecture.md) | Core model, command API, events, HTTP API, UI slots, plugin roadmap |
| [Development](docs/development.md) | Setup, checks, CI |
| [Contributing](CONTRIBUTING.md) | Workflow and checks for changes |

## License

[MIT](LICENSE)
