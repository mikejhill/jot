# Clean up Jot tasks (with approval)

Nothing changes without the user approving each item.

1. Read the owner's cleanup rules: `cat "${JOT_HOME:-$HOME/.jot}/instructions/cleanup.md"`.
2. Scan. Heuristics cover stale inbox/ready/done tasks, near-duplicate titles, missing repo paths, and idle projects. `--agent` adds an LLM review that applies cleanup.md:
   ```bash
   jot cleanup scan --agent --json
   ```
3. Show the proposal as a numbered list: index, task id, title, action (`archive`, `delete`, or `review`), and reason. Add your own judgment where it helps, e.g. "#14 looks done given commit abc123".
4. Ask which items to apply. Wait for an explicit answer.
5. Apply only those:
   ```bash
   jot cleanup apply <proposal-id> --approve 0,2,5
   ```
   `archive` moves the task to archived. `delete` soft-deletes it (recoverable; the history is kept). `review` only adds a note.
6. Report what changed. Never hard-delete; suggest `jot backup` before a large cleanup.

To find old tasks by hand: `jot ls --older-than 60d --status inbox --json`, `jot ls --project <slug>`, `jot ls --q "text"`.
