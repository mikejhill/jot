---
name: jot
description: Work with the user's Jot information store via the `jot` CLI. Use for any Jot interaction - capturing notes, ideas, tasks, or follow-ups ("jot this", "remind me to", "add a task"); finding, listing, or summarizing jots; grooming (re-rank, relabel, re-status, link, comment); or picking the next jots to work on.
---

# Jot

Jot stores bite-sized pieces of information ("jots") in local SQLite at `$JOT_HOME` (default `~/.jot`). Use only the `jot` CLI. Add `--json` whenever you parse output; `jot <command> --help` lists every option.

## Model

- **Jot:** `id`, `title`, `body` (markdown), `profile`, `status`, `category`, `project`, `labels`, `rank`, `metadata`, `version`, timestamps.
- **Profile:** declares a jot's statuses and fields. Built-ins: `note` (open, done) and `task` (todo, doing, done, wont_do). `jot profile ls` shows what this user has.
- **Category:** every status maps to `open`, `active`, `done`, or `cancelled`. Filter by category (`-c open`) to work across profiles.
- **Rank:** one global order, independent of urgency. `jot ls` returns rank order; top = most important to the user.
- **Metadata:** `fields.*` holds profile fields (`-f name=value`); other namespaces hold free-form or tool data (`-m ns=<json>`).
- **Comments, links, history:** comments are a collection; links are typed (`parent`, `relates`, `blocks`, `duplicates`); every change is recorded with actor and reason.

## Rules

- **Identify yourself on writes:** `--actor agent:<name>` (or set `JOT_ACTOR`) and a short `--reason`.
- **Never overwrite newer edits:** read with `--json`, then write with `--if-version <version>`. On exit code 3 (conflict), re-read and retry or ask.
- **Read freely; change with consent.** Capturing what the user asked to capture needs no extra confirmation. Edits, status moves, re-ranking, deletion, and archiving need the user's OK for those specific changes.
- **Preserve the source.** Put summaries in a comment or metadata; don't silently replace a jot's body.

## Commands

```bash
jot add "<one line>" [-p task] [-P project] [-l label] [-f field=value] [-t title]   # one line -> title
jot add --stdin -t "<title>" < notes.md                # longer body
jot ls [-p profile] [-s status] [-c open|active|done|cancelled] [-P project] [-l label] [-q text] [-n 10] [--archived] --json
jot show <id> --with comments,links,history --json
jot edit <id> [-t title] [-b body] [-P project|''] [-p profile] [-f k=v|k=] [-m ns=json|ns=] --if-version N
jot status <id> <status> --if-version N
jot label <id> -a add -r remove
jot rank <id> --top | --bottom | --before <id> | --after <id>
jot comment <id> "<text>" [--kind ns.kind] [--reply-to <comment id>]
jot link <src> <dst> --rel parent|relates|blocks|duplicates
jot archive|unarchive|rm|restore <id>
jot project add <slug> "<name>" ; jot project ls
jot events --after <event id> --json                   # change feed for scripts
jot plugin ls --json ; jot plugin enable|disable <id>  # plugins (ask before enabling)
jot plugin config <id> [--set '<json>']                # settings
jot job ls [--status failed] --json                    # plugin background jobs
```

Exit codes: 0 ok, 1 invalid input or config, 2 not found, 3 version conflict.

## Common flows

- **Capture:** one `jot add` per idea. Keep it short; let the user's words stand. Add project/labels only when obvious.
- **What's next:** `jot ls -c open -c active -n 10 --json`, then summarize the top few in rank order.
- **Groom:** propose changes as a list (id, change, reason), apply after approval with `--if-version`.
- **Work an item:** read it with `show --with comments,links`, move it to an `active` status, comment progress, and move it to a `done` status when the user accepts the result.
