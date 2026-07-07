# CI ephemeral runner — one GitHub job per on-demand rixi box

Provision a rixi box on demand, register it as an **ephemeral** GitHub Actions self-hosted runner,
run exactly **one** job, then tear the box down. A fresh, throwaway machine per job — no
standing runner to patch, no leftover state between jobs.

It reuses the rixi machinery you already have — `gateway.client.GatewayClient` to get a box and
`rixi.Client` to ship + run a task — so there are **no changes to the gateway, bootstrap, or any
provider module**. The only pieces are in this directory:

| file | role |
|---|---|
| `rixi_ci_runner.py` | host driver: mint token → provision → run one job → teardown |
| `run_ephemeral.sh` | box-side: download the runner, `config.sh --ephemeral`, `run.sh` (one job) |
| `pixi.toml` | the box's env + the `ci-runner` task |

## How it works

1. The driver mints a short-lived **runner registration token** from the GitHub API using a PAT
   read from `$GH_PAT` (or `$GITHUB_TOKEN`). The PAT never leaves the driver process; only the
   derived registration token is sent onward, and it travels the **encrypted rixi tunnel** to the
   box (end-to-end encrypted when the resource sets `key_secret`).
2. The driver asks the gateway for the `ci-runner` resource and waits for the box to dial back.
3. It ships this directory (plus a generated `ci.env`) to the box and runs the `ci-runner` task:
   the box downloads the GitHub runner for its arch, registers **ephemeral**, and blocks on one job.
4. GitHub dispatches a queued job whose `runs-on` matches the runner's labels; it runs on the box.
5. The ephemeral runner deregisters itself and exits; the driver tears the box down.

## Catalog entry

Add a resource to the gateway's `rixi.toml` (see `gateway/rixi.toml.example`). Local/free on
Rancher Desktop:

```toml
[resource.ci-runner]
provider = "kubernetes"     # or "hetzner" for a real cloud box
reuse    = false            # a fresh box per CI job
teardown = "on_release"     # destroyed when the driver disconnects
[resource.ci-runner.vars]
kube_context = "rancher-desktop"
image        = "debian:bookworm-slim"
```

## Usage

```bash
# gateway must advertise a dial-back URL the box can reach (see the k8s provider README)
python -m gateway --secret "$S" --config rixi.toml \
  --ws-bind 0.0.0.0:7100 --public-ws-url ws://host.docker.internal:7100 &

export GH_PAT=<a PAT with runner-registration rights on the repo>
PYTHONPATH=<rixi-repo> python ci-runner/rixi_ci_runner.py \
  --repo owner/name --gateway ws://127.0.0.1:7100 --secret "$S" \
  --resource ci-runner --labels rixi-ephemeral
```

Then a workflow job targeting the label runs on the box:

```yaml
jobs:
  build:
    runs-on: [self-hosted, rixi-ephemeral]
    steps:
      - run: echo "hello from a throwaway rixi box"
```

## Notes

- **PAT** — needs permission to create runner registration tokens (a classic PAT with `repo`, or a
  fine-grained token with repo *Administration: read & write*). Keep it in the environment; never
  commit it. Org-level runners use the org registration-token endpoint (swap the URL in the driver).
- **Token lifetime** — the registration token is short-lived and minted per invocation, so it is
  never cached or baked into an image.
- **Arch** — `run_ephemeral.sh` auto-detects x86-64 vs arm64 (Rancher pods are arm64).
- **One job** — `--ephemeral` guarantees the runner exits after a single job; pair it with
  `reuse = false` so every job gets a clean box.
