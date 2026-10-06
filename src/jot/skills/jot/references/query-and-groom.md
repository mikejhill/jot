# Query and groom the Jot queue

Jot (`jot` CLI; data in `$JOT_HOME`, default `~/.jot`) is the user's local task tracker. Read freely. Change tasks only after the user approves the specific changes.

## 1. Query

Pass `--json` for anything you plan to parse.

```bash
jot ls --json                                   # open tasks (excludes deleted)
jot ls --q "health check" --json                # full-text: title, description, note, labels
jot ls --project <slug> --status ready --status inbox --json
jot ls --label <label> --crit high --json       # repeat --label to require all of them
jot ls --older-than 30d --json                  # stale (by updated_at; --age-field created_at)
jot next -n 10 --json                           # ranked queue (criticality x project priority x age)
jot show <id> --json                            # full detail + history: plans, questions, answers, results
jot projects ls --json
```

Tasks about a theme often lack a project or label. Combine a full-text search with a project filter. For example, for jot itself: `jot ls --q jot --json`.

## 2. Analyze

Summarize what you found as a compact table: id, status, criticality, title. Then recommend. Typical findings:

- **Priority:** criticality that doesn't match the impact or urgency in the description; the top 3 to do next, and why.
- **Clarity:** vague titles or descriptions with no acceptance criteria; tasks stuck in `inbox` that need one answer to become ready.
- **Structure:** duplicates or overlapping tasks to merge; oversized tasks to split; dependencies (do A before B).
- **Hygiene:** missing project, labels, or `--repo` path (agents need the repo path to work on code); stale tasks (hand off to [cleanup.md](cleanup.md)).
- **Batching:** related tasks that one agent run could handle together.

Base each recommendation on the task text and history (`jot show`). Don't guess.

## 3. Apply (after the user approves)

Present the changes as a numbered list and apply only the ones the user approves:

```bash
jot edit <id> --title "..." --description "..." --crit high --type feature \
  --project <slug> --repo "<abs path>" --flow planned|direct --due-at 2026-11-01T00:00:00Z
jot label add <id> <label>; jot label rm <id> <label>
jot projects edit <slug> --priority 2            # weights the whole project in `jot next`
jot move <id> ready|blocked|wont_do|archived     # not planning/executing: those come from runs
jot comment <id> --kind comment "why this changed / link to related #id"
jot add "<new task>" --project <slug> --label ... --title "..."   # for splits; then archive the original
```

- **Merge:** keep the stronger task, fold the other's details into its description with `edit`, comment "merged from #N", then `jot move <N> archived`.
- **Split:** `jot add` one task per outcome, comment on each new task with the original task's id, then archive the original.
- Tasks with an active claim (`claimed_by` set) can't be edited. Leave them alone.

## 4. Pick and apply enhancements (hand off to drawdown)

When the user wants the chosen tasks done, use [drawdown.md](drawdown.md), or Jot's runner:

```bash
jot plan <id> [--backend claude --model opus]     # read-only plan -> awaiting_approval (needs_input if it has questions)
jot answer <id> -a <question-event-id>=<text>     # answer a needs_input task; the run resumes
jot approve <id> --note "answers" [--model sonnet]
jot run <id> --direct                              # small, clear tasks
```

If the task is about a codebase that is open in this session (e.g. jot itself), you may instead work it in the session using the claim protocol in [drawdown.md](drawdown.md). Claim the task first so no other agent picks it up.
