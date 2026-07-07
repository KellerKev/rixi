# RIXI Examples

Every example here kills a **real hard problem** with one command — the kind of thing that
normally means Dockerfiles, image registries, env drift, scp round-trips, or firewall tickets.
Each subdirectory is self-contained; the ones with their own `pixi.toml` install with `pixi install`.

## Prerequisites

- [Pixi](https://pixi.sh/) installed; `pip install rixi` for the SDK/CLI examples.
- For the deployment demos, a running RIXI server — see the top-level
  [Quickstart](../README.md#quickstart): `cd server && pixi run python rixi_server.py --port 9000`.
- The `agent-demos` import the agent engine in [`../agent/`](../agent/) (the scripts add it to
  `sys.path` automatically) and expect its deps installed (`cd ../agent && pixi install`).

## The demos

### ⭐ `showcase-gpu-lifecycle/` — the whole lifecycle, end to end
**The problem:** getting from "I have a model idea" to "it's serving behind an API" means renting a
GPU, reproducing your env on it, training, standing up a server, and remembering to shut it all
down. **RIXI:** one walkthrough that provisions a GPU → QLoRA fine-tunes on it → serves the result
behind an OpenAI-compatible API → tears the box down so billing stops. The marquee demo.

```bash
cd showcase-gpu-lifecycle && ./run.sh          # see its README for prerequisites + teardown
```

### ⭐ `../metaflow-rixi/` — `@rixi`, a Metaflow compute backend
**The problem:** you love Metaflow's flow model and want a specific step to run on a box you
control — a GPU, an on-prem server, a gateway-provisioned box. **RIXI:** add `@rixi` to a `@step`
and it runs on a rixi box, artifacts flowing back through a shared S3 datastore while the rest of the
flow stays local — the same seam `@batch`/`@kubernetes` use, one line. The runnable
[`regulatory_extractor`](../metaflow-rixi/examples/regulatory_extractor/) flow trains a real ML model
(predict a product's regulatory profile from its public listing text) on the box in seconds, model
returned via S3 (base 60% → trained 96%); see also the minimal
[`branching_flow`](../metaflow-rixi/examples/branching_flow/).

```bash
pip install -e ../metaflow-rixi rixi          # provides @rixi + the `rixi` command
python ../metaflow-rixi/examples/regulatory_extractor/extractor_flow.py \
    --datastore=s3 --datastore-root=s3://metaflow/ run   # see that folder's README for setup
```

### `finetune-qlora/` — QLoRA fine-tune on a remote GPU
**The problem:** your training env won't reproduce on the rented GPU box. **RIXI:** ships your exact
`.pixi/` env with the code; a complete 4-bit QLoRA fine-tune (sized for a 24 GB GPU) runs on the box
and streams logs back.

```bash
pip install rixi
rixi run --server http://127.0.0.1:9000 --task finetune ./finetune-qlora
```

### `inference-openai/` — serve a model behind an OpenAI-compatible API
**The problem:** you have a model on a GPU box but your tools speak OpenAI. **RIXI:** deploy the
[inference-server](../inference-server/) as a keep-alive task, front it with the [proxy](../proxy/),
and call it with the stock `openai` SDK — LangChain, LlamaIndex, `llm`, anything OpenAI-compatible
works unchanged.

```bash
RIXI_PROXY_URL=http://localhost:8002/v1 pixi run --manifest-path inference-openai/pixi.toml call "haiku about the sea"
```

### `data-pipeline/` — run a data/ETL job near your data
**The problem:** the data (and the RAM) live on a server, not your laptop. **RIXI:** ship a small
Polars ETL (extract → transform → write Parquet) to a bigger box or one close to the data — without
installing the data stack locally. RIXI isn't just for ML.

```bash
rixi run --server http://127.0.0.1:9000 --task pipeline ./data-pipeline
```

### `notebook/` — drive RIXI from Jupyter with the SDK
**The problem:** you live in a notebook but the compute lives elsewhere. **RIXI:** `quickstart.ipynb`
uses `from rixi import Client` to run and stream remote tasks straight from a cell.

```bash
cd notebook && pixi run lab
```

### `hello/` — the quickstart task
The minimal Pixi project the README quickstart deploys: a `pixi.toml` with one `hello` task — the
smallest end-to-end upload → execute → stream loop.

```bash
cd hello && pixi run hello
# or ship it to a server (running on :9000):
rixi run --server http://localhost:9000 --task hello .
```

### `crewai-showcase/` — a remote job that writes results to *your* machine
**The problem:** a job runs on the remote box, but you need its output back on your laptop — cue the
scp round-trip. **RIXI:** a CrewAI + Ollama research crew runs as a remote task and writes its report
**straight to your local filesystem** through the MCP back-channel. The remote job treats your
machine's files as a tool it can call — "as if local", in reverse.

```bash
cd crewai-showcase && pixi install && pixi run demo-research-mcp   # report lands on your machine
```

### `http-backends/` — services for the proxy
Small stdlib and Flask HTTP services you can place behind the [proxy](../proxy/) to exercise the
API-compatibility layer.

```bash
cd http-backends && pixi install && pixi run http-service   # or: flask-service
```

### `agent-demos/` — native RIXI agent orchestration (and platform bridges)
Demonstrations that import the engine in [`../agent/`](../agent/). RIXI has its **own native
multi-step agent orchestration** — no CrewAI/LangChain required: `start_agent.py` runs a
declarative *workflow* from a YAML config, chaining steps that call MCP tools and route
generation to a remote model, passing context between steps. The workflows live in
`agent_config.yaml`:

- **`research_workflow`** — research (web-search MCP tool) → generate from `${research_data}` →
  save. The native equivalent of a CrewAI research crew.
- **`analysis_workflow`** — multi-step gather → analyze → report.
- **`simple_generation`** — single-step generation.

```bash
cd ../agent && pixi install
pixi run python start_agent.py --config ../examples/agent-demos/agent_config.yaml \
    --workflow research_workflow --topic "fusion energy"
```

The demo scripts:

- `simple_agent.py` — a lightweight **native hybrid** agent: local MCP tools + remote compute
  mixed in one workflow.
- `platform_launcher.py` — run the *same* task on the **native** engine or bridge it to CrewAI /
  AutoGen / … (`--platform native` is the built-in RIXI orchestration).
- `platform_comparison_demo.py` — native vs. other platforms, side by side.
- `crewai_integration.py` — a CrewAI ↔ MCP bridge built on the engine.
- `crewai_remote_bridge.py` — run a whole CrewAI crew against a **remote** RIXI inference
  backend (generation routes over the encrypted back-channel to a model served as a task),
  as opposed to `crewai-showcase/` which drives local Ollama models.
- `usage_examples.sh` — prints example `start_agent.py` invocations.
- `agent_config.yaml`, `haiku_config.yaml` — the native workflow/MCP/generation configs.

```bash
cd ../agent && pixi install          # the engine + its deps
cd ../examples/agent-demos
bash usage_examples.sh               # prints example commands
python platform_comparison_demo.py   # needs a running server + an aes.key
```

## How they relate to the components

| Demo | Exercises |
|------|-----------|
| `showcase-gpu-lifecycle` | the full loop: provision → fine-tune → serve (proxy) → teardown |
| `../metaflow-rixi` | `@rixi` Metaflow compute backend — a step runs on a rixi box, artifacts via S3 |
| `finetune-qlora` | remote GPU execution (`rixi` SDK/CLI → server → GPU task) |
| `inference-openai` | `inference-server/` + `proxy/` (OpenAI-compatible serving) |
| `data-pipeline` | remote data/ETL execution (`rixi` SDK/CLI → server) |
| `notebook` | the importable `rixi` SDK from Jupyter |
| `hello` | server + clients (the core remote-execution loop) |
| `crewai-showcase` | a task payload + the client MCP back-channel |
| `http-backends` | the `proxy/` API-compatibility layer |
| `agent-demos` | the `agent/` engine + MCP tool servers + a remote inference backend |
