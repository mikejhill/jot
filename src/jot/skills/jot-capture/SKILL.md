---
name: jot-capture
description: Capture ideas, gaps, bugs, and follow-ups into the user's Jot task tracker. Use when the user says "jot this", "add a task", "track this", "remind me to", "note that we should", or when a conversation surfaces follow-up work worth tracking (missing tests, tech debt, TODOs found while working).
---

# Capture tasks into Jot

Jot is the user's local task tracker (SQLite at `$JOT_HOME`, default `~/.jot`), driven by the `jot` CLI.

## Quick capture (preferred)

Pass the note in the user's own words. Jot stores it instantly and enriches it with an LLM in the background (title, description, project, labels, criticality, type):

```bash
jot add "orbit api - health check is simple ping; need to make holistic"
```

Add `--wait` to enrich right away and print the result, and `--json` to get machine-readable output.

## Pre-filled capture

When you already know the details from the conversation, pass them. Enrichment keeps owner-supplied fields:

```bash
jot add "<note>" --project <slug> --label <label> --label <label> \
  --crit low|medium|high|critical --type feature|bug|chore|research|idea \
  --title "<short imperative title>" --repo "<absolute repo path>"
```

- See the known projects with `jot projects ls --json`. Use an existing slug; don't invent near-duplicates.
- Pass `--repo` when the task is about a specific repository you are working in, so agents can work on it later.
- One task per distinct outcome. Split compound notes.

## After capturing

Tell the user the task id and the enriched title (`jot show <id> --json`). Don't start the work unless they ask.
