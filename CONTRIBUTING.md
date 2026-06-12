# Contributing to RIXI

Thanks for your interest in contributing!

## Development Setup

RIXI is split into independent [Pixi](https://pixi.sh) projects — `server/`,
`clients/`, and `agent/` each have their own `pixi.toml`. Set up only the
component you are working on:

```bash
cd server   # or clients/ or agent/
pixi install
```

## Tests

Tests live in `agent/tests/` and use pytest:

```bash
cd agent
pixi run test
```

## Linting

- Python: `ruff check .` from the repo root (config in `ruff.toml`).
- Installer: `shellcheck install-rixi.sh`.

CI runs both on every push and pull request.

## Pull Requests

- Open PRs against the `main` branch.
- Keep changes focused; include a short description of what and why.
- Make sure `ruff check .`, `shellcheck install-rixi.sh`, and the agent test
  suite pass before requesting review.
