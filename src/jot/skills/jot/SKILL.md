---
name: jot
description: Work with the user's Jot task tracker via the `jot` CLI. Use for any Jot interaction, including capturing ideas or follow-ups ("jot this", "add a task", "remind me to"), querying or summarizing tasks ("what's in my queue", "tasks about X"), grooming the queue (reprioritize, refine, merge, split, pick what's next), drawing down work (plan, approve, execute a task, or work the next one), and cleaning up stale or duplicate tasks.
---

# Jot

Jot is the user's local task tracker. Data lives in SQLite at `$JOT_HOME` (default `~/.jot`). Interact with it only through the `jot` CLI. Every command accepts `--json`; use it whenever you parse output. `jot <command> --help` shows all options.

## Pick the workflow, then read its reference

| User intent | Read |
| --- | --- |
| Capture a note, idea, bug, or follow-up | [references/capture.md](references/capture.md) |
| Find, list, search, or summarize tasks; reprioritize, refine, merge, split; choose what's next | [references/query-and-groom.md](references/query-and-groom.md) |
| Plan, approve, execute, or work a task (yourself or via Jot's runner) | [references/drawdown.md](references/drawdown.md) |
| Find and remove stale or duplicate tasks | [references/cleanup.md](references/cleanup.md) |

Read only the reference you need. A request can chain workflows: "look at my jot enhancements and do the best one" is query-and-groom, then drawdown.

## Essentials (always apply)

- **Model of a task:** title, description, raw note, project (slug), labels, criticality (low/medium/high/critical), type (feature/bug/chore/research/idea), status, optional repo path, and an event history (comments, plans, questions, answers, results).
- **Statuses:** `inbox → ready → planning → awaiting_approval → executing → review → done` (planned flow). The direct flow skips planning and approval. A run (plan or execute) that needs the owner's input parks in `needs_input` until they answer, then resumes the same phase. Other statuses: `blocked`, `wont_do`, `archived`. Only runs or claims move a task into `planning` or `executing`; never `jot move` it there.
- **Read freely. Change with consent:** edits, moves, merges, cleanup, and executing work need the user's approval for those specific changes. Capturing a task the user asked for needs no extra confirmation.
- **Never touch a task someone else has claimed** (`claimed_by` set). Claim before you work on a task yourself.
- **Owner guidance:** `$JOT_HOME/instructions/{triage,drawdown,cleanup}.md` and `instructions/projects/<slug>.md` override the defaults in these references. Read the relevant file before drawdown or cleanup.
- **Quick reference:**
  ```bash
  jot add "<note>" [--wait]          # capture (enriched by an LLM)
  jot ls [filters] --json            # query; jot show <id> --json for detail
  jot next -n 5 --json               # ranked ready queue
  jot plan|run|approve <id> [--backend claude|codex|copilot] [--model <id>]
  jot cleanup scan [--agent]
  jot serve --open                   # web UI at http://127.0.0.1:8765
  ```
