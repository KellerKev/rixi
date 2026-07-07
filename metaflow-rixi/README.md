# metaflow-rixi — run Metaflow steps on RIXI compute

`@rixi` is a Metaflow **compute backend** (like `@batch` / `@kubernetes`): decorate a step and it
runs on a rixi box instead of your laptop — a rixi server you point at directly, or one the
[gateway](../gateway/) provisions on demand and tears down after.

```python
from metaflow import FlowSpec, step, rixi

class MyFlow(FlowSpec):
    @step
    def start(self):
        self.data = load()
        self.next(self.train)

    @rixi(resource="hetzner-cpu")     # this step runs on a rixi box; the rest stay local
    @step
    def train(self):
        self.model = fit(self.data)   # heavy work, elsewhere
        self.next(self.end)

    @step
    def end(self):
        deploy(self.model)            # back on the laptop, reads the model from the datastore
```

## How it works

Metaflow's runtime reroutes a `@rixi` step through `runtime_step_cli` to an internal `rixi step`
command (the same seam `@kubernetes` uses). That command:

1. builds the ordinary `python flow.py … step <name> …` command,
2. ships the flow's directory to a rixi box (rixi packages code + env and runs it there),
3. runs the step on the box against a **shared S3 datastore**, so Metaflow moves the input and
   output **artifacts** itself — rixi only ships code and runs the command,
4. waits for completion, propagates the step's exit code, and (gateway path) tears the box down.

Because artifacts flow through S3, a downstream local step transparently reads what a remote step
produced. **An S3-compatible datastore is required** (MinIO, Scaleway Object Storage, AWS S3, …).

## Install

```bash
pip install -e path/to/rixi/metaflow-rixi     # provides @rixi + the `rixi` command
pip install rixi                              # the RIXI client SDK (orchestrator side)
```

The **box** doesn't need these on PyPI: `rixi step` bundles the `metaflow-rixi` source into the
upload (`_deps/metaflow-rixi`) so the box installs it from a pixi path dependency — self-contained
and air-gap friendly. The flow project's `pixi.toml` must include `metaflow`, `boto3`, and that
path dep (see [`examples/branching_flow/pixi.toml`](examples/branching_flow/pixi.toml)).

## Why `from metaflow import rixi` works

Metaflow doesn't ship rixi. `@rixi` appears in the `metaflow` namespace only because this package is
installed, via **Metaflow's extension mechanism** (the same one Metaflow's own plugins and
Outerbounds' extensions use):

1. **A namespace package.** `metaflow-rixi` installs files under `metaflow_extensions/rixi/…` with
   **no** `metaflow_extensions/__init__.py`. That makes `metaflow_extensions` a
   [PEP 420](https://peps.python.org/pep-0420/) namespace package — Metaflow's reserved extension
   point, which any number of installed packages can contribute to.
2. **Metaflow scans it at `import metaflow`.** It imports each `metaflow_extensions.*.plugins`
   module and reads its `*_DESC` lists. Ours
   ([`metaflow_extensions/rixi/plugins/__init__.py`](metaflow_extensions/rixi/plugins/__init__.py))
   declares `STEP_DECORATORS_DESC = [("rixi", ".rixi_decorator.RixiDecorator")]` (and a
   `TRAMPOLINE_CLIS_DESC` for the `rixi` CLI), which Metaflow **merges** into its own registries
   alongside the built-in `@batch`/`@kubernetes`.
3. **Decorators are exposed by name.** Metaflow surfaces every registered step decorator as a
   top-level attribute using its `name`. Our decorator sets `name = "rixi"`, so `metaflow.rixi`
   exists and `from metaflow import rixi` resolves to our `RixiDecorator`.

Consequences: it's **install-gated** — with `metaflow-rixi` absent, `from metaflow import rixi`
raises `ImportError` (Metaflow core has never heard of rixi). And the **box** imports the flow too,
so it needs the extension as well — which is exactly why every upload bundles it as
`_deps/metaflow-rixi`.

## Run the example (local, free — MinIO + a local rixi server)

```bash
# 1) an S3-compatible datastore (MinIO in Docker)
docker run -d -p 9100:9000 -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  minio/minio server /data
aws --endpoint-url http://127.0.0.1:9100 s3 mb s3://metaflow   # or create the bucket any way

# 2) a rixi server to act as the box
cd rixi/server && pixi run python rixi_server.py --port 9002 &

# 3) point the datastore at MinIO and run the flow — the `compute` step runs on the box
export METAFLOW_S3_ENDPOINT_URL=http://127.0.0.1:9100
export AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin AWS_DEFAULT_REGION=us-east-1
cd rixi/metaflow-rixi/examples/branching_flow
python branching_flow.py --datastore=s3 --datastore-root=s3://metaflow/ run
```

You'll see `compute` run remotely and `end` read its `result` back through S3.

## Examples

| Example | What it shows |
|---|---|
| [`examples/branching_flow/`](examples/branching_flow/) | the minimal shape — one `@rixi` step, artifacts through S3 |
| [`examples/regulatory_extractor/`](examples/regulatory_extractor/) | **a real ML flow** — train a scikit-learn model on the box (predict a product's regulatory profile from its public listing text), model returned via S3, base-vs-trained A/B + a Metaflow card. Runs in seconds on a CPU; documents the GPU/LLM upgrade path. |

## `@rixi` options

| option | meaning |
|---|---|
| `server` | a rixi server URL to run the step on directly (skips the gateway) |
| `resource` | a gateway resource name to provision/reuse (needs `gateway` + `secret`) |
| `gateway` / `secret` | gateway ws URL + shared secret (or `RIXI_GATEWAY_URL` / `RIXI_GATEWAY_SECRET`) |
| `provider` | gateway provider for an ad-hoc box when no `resource` is given |
| `token` / `aes_key` | JWT bearer token / base64 AES key for the rixi server |
| `task` | pixi task in the flow project that runs a step (default `rixi-step`, injected) |
| `teardown` | tear the box down after the step (gateway path; default `True`) |

## Status & limits

- Works with the direct `server=` path (validated end-to-end: remote step → S3 → local downstream,
  plus exit-code propagation). The gateway `resource=` path is implemented; a live GPU-box run
  needs a **publicly reachable** S3 (the box must reach the datastore).
- MVP uses Metaflow's **local metadata** (single orchestrator host) + the S3 datastore. A metadata
  service and `foreach`/`@parallel` fan-out are future work.
- Each step ships the flow bundle and resolves the pixi env on the box; warm/pre-baked envs are a
  later optimization.
