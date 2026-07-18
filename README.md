<p align="center">
  <img src="assets/rixi-logo.svg" alt="RIXI" width="300">
</p>

# RIXI

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/PyPI-rixi-informational.svg" alt="PyPI: rixi">
  <img src="https://img.shields.io/badge/pixi-native-yellow.svg" alt="Pixi-native">
</p>

<p align="center"><b>Run any project on a remote machine as if it were local — your code, your exact environment, and all. Securely. Even air-gapped. Even behind a firewall.</b></p>

You have a laptop and a beefy box somewhere — a cloud GPU, an on-prem server, an air-gapped
cluster. Getting your work to run *there* usually means Dockerfiles, image registries, `rsync`,
SSH tunnels, "works on my machine" env drift, and inbound firewall holes. **RIXI deletes all of
that.** One command ships your project *and* its fully-resolved environment to the box, runs it as
an isolated task, and streams the output back live — and the remote job can even read and write
files on *your* laptop as it runs. It's a secure, self-hostable remote executor: your code runs on
infrastructure **you** own — a cloud GPU, an on-prem server, an air-gapped box — in the exact
environment you tested, with nothing to reproduce and no inbound ports to open.

## The 30-second version

```bash
pip install rixi
```

```python
from rixi import Client

rixi = Client("http://127.0.0.1:9000")     # add token="…" when the server has auth on
print(rixi.run(".", task="train").output)   # package this dir + its env → ship → run → stream back
```

…or from the shell:

```bash
rixi run --server http://127.0.0.1:9000 --task train .
```

That single call packaged the current directory (code **and** its resolved Pixi environment),
uploaded it over an encrypted channel, ran the `train` task on the remote box in an isolated
subprocess, and streamed stdout back to you. No Dockerfile. No image push. No dependency drift.
Start a server locally in one line to try it — see [Quickstart](#quickstart).

## Hard problems RIXI makes easy

Each of these is normally a project of its own. In RIXI each is one command — because they all fall
out of a single primitive: *ship a project + its environment to a box and run it there.*

| The hard problem | How RIXI makes it easy |
|---|---|
| **"It runs on my laptop but the env won't reproduce on the GPU box."** | RIXI ships your **exact resolved environment** (`.pixi/`) with the code. What you tested is what runs. |
| **"The box is air-gapped — no internet to install anything."** | `--offline-mode` bundles the resolved environment into the upload; the server runs it with zero network. Fully reproducible on disconnected hosts. |
| **"The box is behind a firewall with no inbound ports."** | The box dials **out** over an encrypted reverse tunnel; you then drive it as `http://localhost:…`. No inbound holes, ever. |
| **"The remote job needs to read/write files on my laptop."** | An MCP back-channel exposes your **local** filesystem to the remote task as a tool — so a job running on the GPU box writes its results straight back to your machine. |
| **"My laptop chokes on this compile / test suite."** | Any `pixi.toml` task runs on the remote box, one command, streamed live. It's not just for ML — it's for any heavy workload. |
| **"I want throwaway GPU boxes, spun up on demand and gone when idle."** | Declare boxes by name in TOML; the gateway provisions them on demand (OpenTofu) and **auto-tears-down** on idle / TTL / release. |
| **"Serve my fine-tuned model behind an API my tools already speak."** | Deploy the model as a keep-alive task, put the proxy in front, and call it from **OpenAI, Anthropic, or Ollama** clients — one model, three API surfaces. |
| **"Kick off a long run, close my laptop, check back later."** | `--keep-alive` hands you a task id; reattach from anywhere and replay the output, or push a code change to the *running* task without tearing it down. |
| **"I want a Metaflow step to run on my own box — a GPU, an on-prem server, whatever I can reach."** | Add `@rixi` to a step — it runs on a rixi box (one you point at, or gateway-provisioned), artifacts flow through S3, and the rest of the flow stays local. See [Metaflow: `@rixi`](#metaflow-compute-backend-rixi). |
| **"Give me the cheapest box that meets a spec, and cap what the fleet can spend."** | Request a box by capability (`--gpu L4 --min-ram 24`) and the gateway picks the cheapest match; `max_eur_per_hour` / `max_fleet_eur` bound the spend. See [the gateway](#scale-it-up-the-gateway). |
| **"I want a clean, throwaway CI runner for each job."** | The [CI runner](#a-throwaway-ci-runner-per-job) provisions a box, registers it as an ephemeral GitHub Actions runner, runs one job, and tears the box down. |

## What makes RIXI different

Plenty of tools run code on remote machines. What's genuinely unique here:

- **It ships your *exact* environment, not a recipe.** Because Pixi resolves the whole env into a
  local `.pixi/` folder, RIXI can send that folder *with* the code — which is also what makes true
  **air-gapped** execution possible. No Dockerfile, no registry, no "solve deps on the server and
  hope."
- **Remote feels local — in both directions.** Output streams back live; you detach and reattach by
  task id; you redeploy into a running task. And the remote job can reach *back* to your machine's
  files through the MCP back-channel.
- **Reaches machines nothing else can.** Outbound-only reverse tunnels + a rendezvous gateway mean
  firewalled and NAT'd boxes need **no inbound ports** — they dial out to you.
- **Secure by default, self-hosted by default.** End-to-end AES-256-GCM, pinned-algorithm JWT auth,
  a tighten-only policy floor, hardened archive extraction — on infrastructure you own, with no
  vendor account.

**RIXI at a glance:**

| Capability | What it means |
|---|---|
| **Self-hosted, no vendor account** | runs entirely on infrastructure you own |
| **Ships your resolved env** (Pixi/conda) | the `.pixi/` folder travels with the code — nothing to rebuild on the box |
| **Reaches firewalled / NAT'd boxes** | boxes dial out over a reverse tunnel — no inbound ports to open |
| **Air-gapped / offline bundle** | the resolved env rides along in the upload; zero network on the target |
| **On-demand provisioning + auto-teardown** | declare boxes in TOML; the gateway spins them up and reclaims them |
| **Cheapest-box scheduling + budget caps** | request the cheapest box matching a spec; cap the fleet's hourly and total spend |
| **Multi-API inference front door** | serve a model as a task, call it from OpenAI / Anthropic / Ollama clients |
| **Drive a long-lived task over HTTP** | write to its stdin, proxy HTTP into it, hot-redeploy code — no downtime |
| **Remote job reaches back to local files** | the MCP back-channel exposes your laptop's files to the task |

RIXI's niche: a secure remote **executor you fully control** — strongest when boxes are firewalled,
environments must reproduce exactly, or the network is air-gapped. It's not a hosted SaaS and not a
DAG orchestrator; pair it with your scheduler of choice.

## See it work

Real command sequences. Simple runs use the lightweight **`rixi`** CLI (`pip install rixi`).
Advanced lifecycle features — offline bundling, attach-history, the interactive redeploy menu —
use the **full client** in [`clients/`](clients/), shown here as `rixi-client`:

```bash
alias rixi-client='pixi run --manifest-path /path/to/rixi/clients/pixi.toml python /path/to/rixi/clients/rixi_client.py'
```

### Train on a remote GPU, detach, reattach later

The pain it removes: no more babysitting an SSH session for a six-hour run.

```console
$ rixi-client --server https://gpu-box:9000 --task train --keep-alive
Task ID: 9f0a…   Status: running
epoch 1/50  loss=2.41
^C   →  choose "2) Let task continue and exit"

# hours later, from anywhere — replay history, then follow live:
$ rixi-client --server https://gpu-box:9000 --attach-history 9f0a…
epoch 27/50 loss=0.38
Status: Process completed
```

### Deploy to an air-gapped box (env bundled, zero network)

The pain it removes: no internet on the target, no dependency solving, no drift.

```console
$ pixi install                       # resolve the env into .pixi/ once
$ rixi-client --server https://airgapped-host:9000 --task run --offline-mode --validate-dependencies
📁 .pixi folder size: 1924.7 MB   🔒 Mode: Offline (dependencies included)
Status: Starting task with offline dependencies
```

The client tags the bundle; the server auto-detects it and runs with `PIXI_OFFLINE=1`. Nothing is
fetched or solved on the target.

### Reach a server behind a firewall

The pain it removes: no inbound ports, no VPN, no port-forwarding tickets.

```console
# reachable host (your laptop): accept the tunnel, expose a local port
$ RIXI_TUNNEL_SECRET=… pixi run listen -- --bind 127.0.0.1:9100 --ws-bind 0.0.0.0:7000
# firewalled host (next to the server): dial OUT and forward to the local rixi server
$ RIXI_TUNNEL_SECRET=… pixi run connect -- --to ws://laptop:7000 --target 127.0.0.1:9000
# now drive the firewalled server as if it were local:
$ rixi run --server http://127.0.0.1:9100 --task hello .
Hello from RIXI!
```

### Let a remote job write results back to your laptop

The pain it removes: no scp round-trips to collect outputs — the remote job delivers them locally.

A CrewAI research crew runs on the remote box, generates a report, and writes it **to your local
filesystem** through the MCP back-channel — see
[`examples/crewai-showcase/`](examples/crewai-showcase/). The remote task treats your machine's
files as a tool it can call.

### Provision → fine-tune → serve → tear down

The full lifecycle, end to end, in the flagship walkthrough:
[`examples/showcase-gpu-lifecycle/`](examples/showcase-gpu-lifecycle/) — spin up a GPU, QLoRA
fine-tune on it, serve the result behind an OpenAI-compatible API, then destroy the box so billing
stops.

More runnable examples (inference, data pipelines, notebooks, native agent orchestration) in
[`examples/`](examples/README.md).

## Metaflow compute backend: `@rixi`

RIXI ships a **Metaflow compute backend**. Decorate a step with `@rixi` and *that step* runs on a
box you control — a server you point at, or one the [gateway](gateway/) provisions on demand and
tears down after — while the rest of the flow stays local. It uses the same seam `@batch` and
`@kubernetes` use, so it's **one line**:

```python
from metaflow import FlowSpec, step, rixi

class TrainFlow(FlowSpec):
    @step
    def start(self):
        self.data = load()            # local
        self.next(self.train)

    @rixi(resource="hetzner-cpu")     # ← this step runs on a rixi box; the rest stay local
    @step
    def train(self):
        self.model = fit(self.data)   # heavy work, elsewhere — on infra you own
        self.next(self.end)

    @step
    def end(self):
        deploy(self.model)            # back on your laptop, reads the model from the datastore
```

Artifacts move through a shared **S3 datastore**, so Metaflow itself carries inputs and results
between steps — a downstream local step transparently reads what the remote step produced. Point
`@rixi` at your Mac, a rented GPU, or a gateway-provisioned box; pay by the second; tear it down. It
brings Metaflow's flow model to compute you run yourself — including firewalled or air-gapped boxes,
with the exact same environment reproduced on them. The idea is simple: the **right machine for each
step**, with direct access to it.

- **Runnable sample (seconds, on a CPU):**
  [`metaflow-rixi/examples/regulatory_extractor/`](metaflow-rixi/examples/regulatory_extractor/) —
  a real ML flow that predicts a product's **regulatory profile from its public listing text**;
  the `train` step runs on a rixi box by changing one line, and the model returns via S3
  (base 60% → trained 96% on held-out listings).
- **How it works + more flows:** [`metaflow-rixi/`](metaflow-rixi/) — the `@rixi` decorator (a
  pip-installed [Metaflow extension](metaflow-rixi/README.md#why-from-metaflow-import-rixi-works),
  nothing forked), the minimal branching flow, and a Metaflow step running inside a Kubernetes pod.

## Scale it up: the gateway

For a fleet instead of a single box, the [`gateway/`](gateway/) is a control plane — all open
source, in this repo. Your laptop and every compute box dial **out** to one rendezvous host, so the
gateway is the only machine that needs a public IP. It makes "get a box, use it, stop paying" a
declarative loop:

**Provisioning & lifecycle**
- **On-demand provisioning across providers** — declare boxes by name in TOML; the gateway stands
  them up with OpenTofu. Providers: **Hetzner** (CPU), **Scaleway** (CPU + L4/L40S/H100 GPUs), and
  **Kubernetes** — which runs a rixi box as a **pod** on your own cluster via kubeconfig, no cloud
  account (great on Rancher/k3s): *resource ≠ VM*. A **custom-module** hatch lets you bring your own
  OpenTofu.
- **Auto-teardown** — `idle` / `ttl` / `on_release` / `manual`; a reaper reclaims boxes so billing
  stops. **Warm pools** (`prewarm`) keep a box ready for a no-cold-start first request.
- **Capacity retry** — on a stock-out, fall back across regions and instance types before giving up.

**Cost & scheduling**
- **Capability-based scheduling** — ask for the *cheapest* box matching a spec (GPU, RAM, vCPU,
  arch) and the gateway picks it.
- **Native cost tracking** — per-provider pricing in `offerings.toml` (auto-refreshed from provider
  APIs); live running cost and per-box lifetime cost via the admin API (`/api/costs`).
- **Budget caps** — bound the fleet's burn rate (`max_eur_per_hour`) and cumulative spend
  (`max_fleet_eur`), enforced at provision time.
- **Spot / preemptible** — wiring to request spot capacity, fall back to on-demand, and self-heal on
  preemption. (No shipped provider offers spot yet; the path is ready for an AWS/GCP module.)

**Security & governance**
- **Brokered end-to-end encryption** — it routes client↔box traffic but sees only ciphertext.
- **Policy floor, JWT RBAC & audit** — an admin floor users can only *tighten*, per-role and
  per-resource access rules, one-time provisioning tokens, and a queryable **DuckDB + OTLP** audit
  trail, with an admin API and web console.
- **Encrypted OpenTofu state at rest** (`RIXI_STATE_PASSPHRASE`), with optional remote state
  backends for a highly-available gateway.

Drive it from Python with `GatewayClient` — request a box by name or by capability, wait for it, use
it, release it. See [`gateway/README.md`](gateway/README.md).

## A throwaway CI runner per job

[`ci-runner/`](ci-runner/) turns a rixi box into an **ephemeral GitHub Actions runner**: it
provisions a box, registers it as a self-hosted runner, runs exactly one job, then tears the box
down — a clean machine per job, with no standing runner to maintain. It's built entirely on
`GatewayClient` + the rixi SDK (no gateway changes), mints a short-lived runner token per run, and
works on a real cloud box or a local Kubernetes pod (free on Rancher).

## Serve models & long-lived services

A keep-alive task can be a **service**, not just a batch run. Drive a running task over HTTP — write
to its **stdin**, **proxy HTTP straight into it**, and **hot-redeploy new code into it** without
tearing it down. Put the [`proxy/`](proxy/) in front and the same served model answers **OpenAI**,
**Anthropic**, and **Ollama** API calls — one model, three API surfaces — with response caching,
model-name mapping, and metrics. The [`inference-server/`](inference-server/) is a ready-made worker
(HuggingFace or Ollama backends, automatic device placement) you deploy as that task.

## Agent orchestration

RIXI includes a native, config-driven **agent engine** ([`agent/`](agent/)): declarative multi-step
**YAML workflows** that chain MCP tool calls and route generation to a model served as a remote
task — no external framework required. It ships **MCP tool servers** (filesystem, web search), runs
**hybrid** local-tool + remote-generation workflows, and bridges to **CrewAI** and **AutoGen** when
you'd rather drive those. Tool access travels over MCP, hardened for the network by SMCP (below).

## Secure by default

- **End-to-end AES-256-GCM** for data in transit; optional **TLS** (`--tls-cert`/`--tls-key`).
- **RSA→AES key handshake** — a client negotiates a per-session AES-256 key over an ephemeral RSA
  keypair (`--key-secret`), with **use-bounded key rotation** — no shared key to pre-distribute.
- **JWT auth** with a pinned algorithm allow-list (no header-driven `alg` — no algorithm-confusion),
  and a separate authorization gate on log retrieval.
- **Hardened reverse-tunnel crypto** — a per-deployment KDF salt, 600k-round PBKDF2 + HKDF-split
  keys, and an HMAC auth proof (the shared secret is never sent).
- **Loopback-by-default** bind; a non-loopback bind refuses to start without auth unless you
  explicitly opt into `--insecure`.
- **Hardened extraction** — uploads are unpacked with path-traversal/symlink filtering, task names
  are validated, and decompression size is capped.
- Secrets go via `RIXI_KEY_SECRET` / `RIXI_AES_KEY` env vars, never on the command line.

Full details and hardening checklist in [SECURITY.md](SECURITY.md).

## Secure MCP (SMCP)

[MCP](https://modelcontextprotocol.io/) is a clean way to give an agent tools, but it's designed for
a local, trusted transport (typically stdio on the same machine). **SMCP — [Secure
MCP](https://github.com/KellerKev/smcp)** — is a companion project that keeps MCP's tool model
exactly as-is and wraps it in a security layer so it works safely *over a network* and *between
agents*: authentication, per-message encryption, and signed payloads, with tiered modes from a
simple API key up to JWT + AES-256 and audited multi-agent (A2A) coordination. Same tools, now
runnable across machines.

RIXI's agent uses SMCP for exactly that networked case: WebSocket transport, Fernet (AES) on every
payload, `api_key` → JWT sessions, and an HMAC-SHA256 signature per message — so the same tools are
shareable between agents over an untrusted link. The handshake negotiates the protocol version by
MAJOR component (a future `3.x` interoperates with `3.0`; an incompatible `2.x`/`4.x` peer is
rejected). See [`agent/README.md`](agent/README.md#smcp-secure-mcp) for RIXI's implementation, and
[**github.com/KellerKev/smcp**](https://github.com/KellerKev/smcp) for the standalone SMCP project.

**A2A federation (full peer).** RIXI is a first-class SMCP federation peer — it can both *send*
(forward a client-authorized task to a downstream node with the user's identity) and *receive*
(`federated_key_exchange` / `federated_forward`, invoked over `tool_invoke`). Federation is off by
default; when enabled it uses signed forwarding proofs (shared-secret HMAC or per-node RSA-PSS/PS256
with algorithm pinning), forward-secret P-256 ECDH session keys, AES-256-GCM session encryption
bound to the session id, and RS256 client-token verification. The crypto is self-contained in
[`agent/smcp_federation.py`](agent/smcp_federation.py) and verified byte-for-byte against the shared
cross-language conformance vectors (the same file malgra's Rust implementation checks against).

## Quickstart

Start a server on loopback (no auth needed for local dev), then ship the bundled `hello` example
to it:

```bash
# terminal 1 — the server
cd server && pixi install && pixi run python rixi_server.py --port 9000

# terminal 2 — ship examples/hello to it
pip install rixi
rixi run --server http://127.0.0.1:9000 --task hello examples/hello
```

Prefer a container? `docker build -t rixi-server . && docker run --rm -p 9000:9000 -e RIXI_ALLOW_INSECURE=1 rixi-server`
(see [`Dockerfile`](Dockerfile) for authenticated/TLS options).

## Learn more

- **[README_second.md](README_second.md)** — the **full reference**: every server/client flag,
  custom headers, offline internals, the complete architecture diagrams, and deployment modes.
- **[examples/README.md](examples/README.md)** — every runnable example, framed by the problem it
  solves (fine-tune, inference, data pipelines, notebooks, native + CrewAI agent orchestration).
- Component guides: [server](server/README.md) · [clients](clients/README.md) ·
  [agent](agent/README.md) · [proxy](proxy/README.md) ·
  [inference-server](inference-server/README.md) · [tunnel](tunnel/README.md) ·
  [gateway](gateway/README.md) · [metaflow-rixi](metaflow-rixi/README.md) ·
  [ci-runner](ci-runner/README.md)
- [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md)

## License

[MIT](LICENSE) © [Kevin Keller](https://kevinkeller.org)
