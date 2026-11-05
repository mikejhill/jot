# Contributing

## Workflow

- Commit directly to `main` or use a feature branch (`feat/…`, `fix/…`, `chore/…`). If someone else is already working in the main worktree, use a branch.
- Larger changes go through pull requests. Use the [PR template](.github/pull_request_template.md).

## Before you push

```bash
uv run poe check   # format, lint, strict types, tests + coverage
```

Code conventions (class-based design, docstrings, strict typing, data safety, migrations) are in [AGENTS.md](AGENTS.md). Setup, tests, and CI are in [docs/development.md](docs/development.md).

Repository docs that aren't conventional root files (README, CONTRIBUTING, LICENSE, AGENTS/CLAUDE) belong in `docs/`.
