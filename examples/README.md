# RIXI Examples

Runnable demos. Each subdirectory is self-contained; the ones with their own `pixi.toml`
install independently with `pixi install`.

| Example | What it shows | How to run |
|---------|---------------|------------|
| [`hello/`](hello/) | The minimal Pixi task used by the README quickstart — package a directory and run it on a RIXI server. | `cd hello && pixi run hello` (or deploy it via the client, see the top-level README quickstart). |
| [`crewai-showcase/`](crewai-showcase/) | Optional CrewAI + Ollama multi-agent research/content/analysis teams running as a task payload with a local MCP back-channel. **Not required by the core clients.** | `cd crewai-showcase && pixi install && pixi run demo-research` |
| [`http-backends/`](http-backends/) | Small stdlib and Flask HTTP services you can place behind the RIXI proxy. | `cd http-backends && pixi install && pixi run http-service` |
| [`agent-demos/`](agent-demos/) | Drives the agent framework in [`../agent/`](../agent/): a platform launcher, a cross-platform comparison, and a printable list of `start_agent.py` usage examples. | These import the agent modules and configs in `../agent/`; run them from this directory (the scripts add `../agent` to the path) with the agent dependencies installed. `bash agent-demos/usage_examples.sh` prints example commands. |
