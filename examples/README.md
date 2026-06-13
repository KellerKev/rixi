# RIXI Examples

Runnable demos that show how the RIXI components fit together. Each subdirectory is
self-contained; the ones with their own `pixi.toml` install independently with `pixi install`.

## Prerequisites

- [Pixi](https://pixi.sh/) installed.
- For the deployment demos (`hello`), a running RIXI server — see the top-level
  [Quickstart](../README.md#quickstart-local): `cd server && pixi run python rixi_server.py --port 9000`.
- The `agent-demos` import the agent engine in [`../agent/`](../agent/) (the scripts add it to
  `sys.path` automatically) and expect its dependencies installed (`cd ../agent && pixi install`).

## The demos

### `hello/` — the quickstart task
The minimal Pixi project the README quickstart deploys: a `pixi.toml` with one `hello` task. Run
it locally, or ship it to a server through a client to see the full upload → execute → stream loop.

```bash
cd hello && pixi run hello
# or deploy it (server running on :9000):
cd hello && pixi run --manifest-path ../../clients/pixi.toml \
  python ../../clients/rixi_client.py --server http://localhost:9000 --task hello --auto-exit
```

### `crewai-showcase/` — optional multi-agent showcase
CrewAI + Ollama research / content / analysis teams running as a task payload, using a local MCP
back-channel for file access. **Not required by the core clients** — its own Pixi project.

```bash
cd crewai-showcase && pixi install && pixi run demo-research
```

### `http-backends/` — services for the proxy
Small stdlib and Flask HTTP services you can place behind the [proxy](../proxy/) to exercise the
API-compatibility layer.

```bash
cd http-backends && pixi install && pixi run http-service   # or: flask-service
```

### `agent-demos/` — driving the agent engine
Demonstrations that import the engine in [`../agent/`](../agent/):

- `platform_launcher.py` — run the same MCP-backed task across orchestration platforms (native /
  CrewAI / AutoGen / …).
- `platform_comparison_demo.py` — side-by-side comparison using `haiku_config.yaml`.
- `simple_agent.py` — a lightweight, hybrid (local MCP + remote inference) agent.
- `crewai_integration.py` — a CrewAI ↔ MCP bridge built on the engine.
- `usage_examples.sh` — prints example `start_agent.py` invocations.
- `agent_config.yaml`, `haiku_config.yaml` — the fully-populated configs these demos consume.

```bash
cd ../agent && pixi install          # the engine + its deps
cd ../examples/agent-demos
bash usage_examples.sh               # prints example commands
python platform_comparison_demo.py   # needs a running server + an aes.key
```

## How they relate to the components

| Demo | Exercises |
|------|-----------|
| `hello` | server + clients (the core remote-execution loop) |
| `crewai-showcase` | a task payload + the client MCP back-channel |
| `http-backends` | the `proxy/` API-compatibility layer |
| `agent-demos` | the `agent/` engine + MCP tool servers + a remote inference backend |
