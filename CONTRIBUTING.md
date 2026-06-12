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

The three components have **no cross-directory imports** and each is deployed on its own.
Modules within `agent/` and `clients/` import their siblings **bare** (e.g.
`from mcp_manager import ...`) and run with the working directory set to the component dir —
keep new modules in the same directory and importable that way; do not introduce packages or
relative imports.

Runnable demos live under [`examples/`](examples/) (one subdirectory per demo, several with
their own `pixi.toml`); see [`examples/README.md`](examples/README.md).

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
