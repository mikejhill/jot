# Draw down Jot tasks

Jot (`jot` CLI, data in `$JOT_HOME`, default `~/.jot`) holds the user's tasks. Several agents may work the list at once, so **always claim before working** and **never touch a task claimed by someone else**.

## 1. Read the owner's guidance first

```bash
cat "${JOT_HOME:-$HOME/.jot}/instructions/drawdown.md"
cat "${JOT_HOME:-$HOME/.jot}/instructions/projects/<project-slug>.md"   # if it exists
```

Follow it. It overrides the defaults below.

## 2. Choose a task

```bash
jot next -n 5 --json            # ranked ready tasks (criticality x project priority x age)
jot show <id> --json            # full detail + event history (comments, plans, answers)
```

If the user named a task, use that one.

## 3. Choose how to run it

**Option A: hand it to Jot's runner (non-interactive, recommended for batch work).** Jot claims the task, runs Claude, Codex, or Copilot headlessly, and records everything:

```bash
jot plan <id> --backend claude      # planned flow: read-only plan -> awaiting_approval (needs_input if it has questions)
jot answer <id> -a <event-id>=<text> # answer a needs_input task's questions; the same phase resumes
jot approve <id> --note "answers"   # after the user approves: executes in a git worktree -> review
jot run <id> --direct               # direct flow: skip planning, execute now -> review
jot run --next --project <slug>     # take the top ready task
jot runs <id>; jot log <run-id>     # inspect
```

**Option B: work it yourself in this session.** Claim with a lease, keep it alive, and record your work as events:

```bash
AGENT="claude-session-$(date +%s)"                 # stable name for this session
jot claim <id> --agent "$AGENT" --lease-seconds 1800 --json   # planned flow -> planning
jot claim <id> --agent "$AGENT" --direct --json               # direct flow -> executing
```

If the claim fails (another agent holds it, or the task isn't ready), stop and pick another task.

- **Planned flow (default):** investigate read-only, then post the plan and any questions, and release to `awaiting_approval`. Then **stop** and wait for the user's approval:
  ```bash
  jot comment <id> --kind plan --actor "$AGENT" "<markdown plan: steps, files, verification, rollback>"
  jot comment <id> --kind question --actor "$AGENT" "<one open question>"
  jot release <id> --agent "$AGENT" --status awaiting_approval
  ```
  Once approved, run `jot claim <id> --agent "$AGENT"` (awaiting_approval -> executing) and continue below.
- **Executing:** do the work. In git repositories, work on a branch (`jot/<id>-<slug>`). Verify it, commit, and don't push unless asked. Then:
  ```bash
  jot comment <id> --kind result --actor "$AGENT" "<what changed, how verified, branch, follow-ups>"
  jot release <id> --agent "$AGENT" --status review
  ```
- **Blocked on the owner (either flow):** post each question and release to `needs_input`. Then **stop**:
  ```bash
  jot comment <id> --kind question --actor "$AGENT" "<one open question>"
  jot release <id> --agent "$AGENT" --status needs_input
  ```
  Their answers arrive as `answer` events (`jot show <id> --json` lists `questions` with each `answer`). `jot claim <id> --agent "$AGENT"` resumes the phase you paused (planning or executing).
- If you must abandon the task: `jot release <id> --agent "$AGENT"` (it goes back to its prior status), with a comment explaining why.

Long work: re-run `jot claim` from another agent fails while your lease is live. Expired leases are returned to the queue by `jot reap`.

## 4. Report

Give the user the task id, its new status, and a one-line outcome. Point out any open questions waiting on them.
