<p align="center">
  <img src="assets/rixi-logo.svg" alt="RIXI" width="300">
</p>

# RIXI

**RIXI** — *Remote Interaction and Execution Implementation* — is a secure runner for [Pixi](https://pixi.sh/) projects: a client packages a project and ships it to the server over an encrypted, authenticated channel, and the server runs it as an isolated task and streams the output back. Optional components add a multi-agent engine, an API-compatibility proxy, and a pluggable inference backend, with [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) for extensible tool access.

## Architecture

At its core RIXI is a **client** and a **server**. A client packages a Pixi project and ships it
to the server over an encrypted, authenticated channel; the server runs it as an isolated task
and streams the output back.

```
        ┌────────────────────┐
        │       client       │   packages a Pixi project; streams output back
        └─────────┬──────────┘
                  │   upload (tar + LZ4) · HTTP · AES · JWT
                  ▼
        ┌────────────────────┐
        │    rixi_server     │   authenticates, decrypts, manages the task,
        │     (FastAPI)      │   streams stdout/stderr back to the client
        └─────────┬──────────┘
                  │   pixi run <task>
                  ▼
        ┌────────────────────┐
        │    task payload    │   your code, in an isolated subprocess
        └────────────────────┘
```

Everything else is an **optional component** — each its own Pixi project — that plugs into this
core. Mix and match them, or use none:

| Component | Role | How it connects |
|-----------|------|-----------------|
| **clients** | Package a project, deploy it, stream output, manage the task lifecycle | → server, over HTTP (AES + JWT) |
| **server** | Execute uploads as isolated tasks; auth, encryption, streaming, MCP | the core |
| **agent** | Multi-agent engine — calls MCP tools and routes generation to a remote model | runs as a task; uses the server's MCP back-channel |
| **inference-server** | LLM backend (HuggingFace / Ollama) | runs as a task payload; answers the agent's / proxy's generation requests |
| **proxy** | API-compatibility front door (OpenAI / Anthropic / Ollama wire formats) | receives standard API calls, forwards them to a RIXI backend task |

**Request flow:** client authenticates (JWT) → optional AES handshake → packages code (tar + LZ4)
→ uploads to the server → server executes it in an isolated subprocess → real-time output streams
back to the client.

## How the components fit together

A task started with `--keep-alive` stays addressable by its **task id**. Anything that knows the
id can attach to it: the client (to follow or manage it), the proxy (to feed it API requests), or
the agent (to drive a workflow against it). They all talk to the same task through the server.

```
          clients ──┐                            ┌── proxy
          (deploy · │                            │   (/task/{id}/input
           attach · ▼                            ▼    + poll /task/{id}))
        ┌───────────────────────────────────────────┐
        │                rixi_server                │
        │          hosts a keep-alive task,         │
        │         addressable by its task id        │
        └───────────────────────────────────────────┘
           manage) ▲                            ▲
          agent ───┘                            └── attach later
          (/input + /stream)                        (/task/{id}/stream)
```

- **clients** deploy a project and, with `--keep-alive`, hand you a task id to **attach to later**.
- **proxy** points at a running inference task id and exposes OpenAI/Anthropic/Ollama endpoints.
- **agent** points at a task id and runs a config-driven workflow, calling local MCP tools.
- **inference-server** is what typically runs *inside* that keep-alive task as the model backend.

See each component's README ([server](server/README.md), [clients](clients/README.md),
[agent](agent/README.md), [proxy](proxy/README.md), [inference-server](inference-server/README.md))
for its own architecture diagram and details.

## Use cases

RIXI ships a Pixi project — your code **and** its fully-resolved environment — to a remote host and
runs it there, streaming output back. That one primitive covers a lot of ground. Each example
below is a real command sequence (the client is invoked as `pixi run python rixi_client.py …`;
shortened to `rixi-client` here for readability).

```bash
# For readability in the examples below:
alias rixi-client='pixi run --manifest-path /path/to/rixi/clients/pixi.toml python /path/to/rixi/clients/rixi_client.py'
```

### 1. Deploy and run a project on a remote host (one command)

Run the client from the project you want to ship; it packages the current directory and runs the
named `pixi.toml` task on the server, streaming output back.

```console
$ cd ~/my-api
$ rixi-client --server https://gpu-box:9000 --task serve --keep-alive

📦 Package Statistics:
  Code files: 24
  Compressed package: 1.8 MB
  🌐 Mode: Online (dependencies will be downloaded)

Task ID: 7b3e1c90-2a4f-4d11-9c2a-1f6e8b0a5d33
Status: Extracting to /tmp/tmp8_63uco4
Status: Starting task - downloading dependencies
✨ Pixi task (serve): uvicorn app:main --host 0.0.0.0 --port 8080
INFO:     Uvicorn running on http://0.0.0.0:8080
⏳ Back-channel active. Press Ctrl+C for options menu.
```

### 2. Air-gapped / offline deploy (dependencies bundled)

`pixi install` once to resolve the environment into `.pixi/`, then deploy with `--offline-mode`:
the client bundles `.pixi/` and tags the package with `.offline_metadata.json`, and the server
auto-detects it and runs with `PIXI_OFFLINE=1` — no network on the target.

```console
$ pixi install                       # resolve the env into .pixi/
$ rixi-client --server https://airgapped-host:9000 --task train --offline-mode --validate-dependencies

🔍 Validating offline deployment prerequisites...
✅ Offline prerequisites validated
📁 .pixi folder size: 1924.7 MB

📦 Package Statistics:
  Code files: 31
  Dependency files: 4120
  Dependencies size: 1924.7 MB
  Compressed package: 612.4 MB
  🔒 Mode: Offline (dependencies included)

Task ID: b1d2…   Status: Offline package: dependencies included
Status: Starting task with offline dependencies
```

Inspect the bundle before sending with `--show-package-size` (builds the package, prints the size
breakdown, and exits).

### 3. Long job → detach → attach later

Kick off a training run or a heavy data-crunching job, detach, and reattach from anywhere later.
Press **Ctrl+C** for the options menu; choose **2** to leave the task running and exit.

```console
$ rixi-client --server https://gpu-box:9000 --task train --keep-alive
Task ID: 9f0a…   Status: running
epoch 1/50  loss=2.41
^C
Interrupted! Choose:
1) Terminate remote task and exit
2) Let task continue and exit
3) Restart remote task and exit
4) Redeploy current code to task
5) Continue monitoring
6) Restart task and keep monitoring
Enter 1-6: 2
Task 9f0a… left running.

# …hours later, from your laptop — replay history, then follow live:
$ rixi-client --server https://gpu-box:9000 --attach-history 9f0a…
epoch 1/50  loss=2.41
epoch 27/50 loss=0.38
Status: Process completed
```

(`--attach` follows live only; `--attach-history` replays the captured output first, then follows.)

### 4. Serve inference behind an OpenAI-compatible API

Deploy the [inference-server](inference-server/) as a keep-alive task, then point the
[proxy](proxy/) at its task id — now any OpenAI/Anthropic/Ollama client can call it.

```console
# 1) deploy the model backend as a long-lived task → note the task id
$ cd inference-server
$ rixi-client --server https://gpu-box:9000 --task start --keep-alive
Task ID: 1ce0-inference   Status: running

# 2) put the proxy in front of that task
$ cd ../proxy
$ pixi run proxy -- --backend https://gpu-box:9000 --inference-task 1ce0-inference --port 8002

# 3) call it like any OpenAI endpoint
$ curl http://localhost:8002/v1/chat/completions \
    -H 'content-type: application/json' \
    -d '{"model":"gpt-3.5-turbo","messages":[{"role":"user","content":"haiku about the sea"}]}'
{"choices":[{"message":{"role":"assistant","content":"Salt wind on the waves…"}}], ...}
```

### 5. Drive an agentic workflow against a remote model

The [agent](agent/) engine attaches to a task id and runs a config-driven workflow — calling local
MCP tools (filesystem, web search) and routing generation to the remote model.

```console
$ cd agent
$ pixi run python start_agent.py \
    --server https://gpu-box:9000 --task-id 1ce0-inference \
    --config agent_config.example.yaml --workflow research_workflow --topic "fusion energy"
```

### 6. Use it as a plain deployment tool (ship code + deps, then push updates)

Forget the AI parts — RIXI is also just a one-command deploy tool. It ships your code **and** its
resolved dependencies, lets Pixi install the environment, runs your app's task, and streams it so
you watch it come up live. When you change the code, **push the update to the same running task**
with Ctrl+C → `4` (repackage + `POST /task/{id}/redeploy`) — no re-uploading from scratch.

```console
# one command ships code + full Pixi env, installs, and runs the app — you watch it come up
$ cd ~/my-api
$ rixi-client --server https://prod-box:9000 --task serve --keep-alive
Task ID: ab12c…   Status: running
✨ Pixi task (serve): uvicorn app:main --host 0.0.0.0 --port 8080
INFO:     Uvicorn running on http://0.0.0.0:8080         # live, and you're watching it

# edited the code? push it to the SAME task — no teardown, no re-setup:
^C
Interrupted! Choose:
1) Terminate remote task and exit
2) Let task continue and exit
3) Restart remote task and exit
4) Redeploy current code to task
5) Continue monitoring
6) Restart task and keep monitoring
Enter 1-6: 4
Task redeployed.
```

Combine with `--offline-mode` (use case 2) to ship the same app — env and all — to a host with no
internet at all.

## Built on Pixi

RIXI is a remote runner for [Pixi](https://pixi.sh/) projects — *Remote Interaction and Execution
Implementation*, a name that also nods to Pixi. A task
is just a directory with a `pixi.toml`: the client packages it, the server unpacks it and runs
`pixi run <task>` in an isolated subprocess, streaming the output back. Because Pixi resolves a
project's complete environment into a local `.pixi/` folder, that same environment can be shipped
*with* the code — which is what makes [air-gapped deployment](#air-gapped--offline-deployment)
possible.

## Directory Structure

```
rixi/
├── server/              # FastAPI task execution server
│   ├── rixi_server.py   # Main server: auth, encryption, task management
│   └── pixi.toml        # Server dependencies
├── clients/             # Runner clients
│   ├── rixi_client.py        # Full-featured CLI client (MCP-capable)
│   ├── rixi_simple_client.py # Lightweight client
│   ├── rixi_transport.py     # Shared transport (encryption, streaming, handshake)
│   └── pixi.toml             # Client dependencies
├── agent/               # Multi-agent engine: framework, MCP tools & workflows
│   ├── ai_agent_framework.py # Core: crypto, RemoteChannel, Agent base classes
│   ├── mcp_manager.py        # MCP server lifecycle, tool dispatch, config loader
│   ├── mcp_servers.py        # MCP tool servers (filesystem + web search)
│   ├── mcp_agent.py          # ConfigurableMCPAgent: the workflow engine
│   ├── start_agent.py        # Single agent entry point / runner
│   ├── agent_config.example.yaml # Config template (see Configuration)
│   ├── tests/                # pytest suite (run with `pixi run test`)
│   ├── README.md             # Agent engine guide
│   └── pixi.toml             # Agent dependencies
├── proxy/               # API-compatibility layer (OpenAI/Anthropic/Ollama → RIXI backend)
│   ├── proxy.py · api_formats.py
│   ├── proxy_config.example.yaml
│   └── pixi.toml
├── inference-server/    # Pluggable LLM inference backend (HuggingFace / Ollama)
│   ├── inference_server.py
│   └── pixi.toml
├── examples/            # Runnable demos (see examples/README.md)
│   ├── hello/                # Minimal Pixi task used by the quickstart
│   ├── crewai-showcase/      # Optional CrewAI + Ollama multi-agent showcase
│   ├── http-backends/        # Example HTTP services for use behind the proxy
│   └── agent-demos/          # Agent-framework demos + their populated configs
├── install-rixi.sh      # One-command remote installer (Linux/macOS over SSH)
├── CONTRIBUTING.md      # Dev setup, tests, linting
├── SECURITY.md          # Vulnerability reporting & hardening guide
└── README.md
```

## Documentation

Each component has its own guide, and the examples have a walkthrough:

- [`server/README.md`](server/README.md) — the task-execution server, flags, secure defaults
- [`clients/README.md`](clients/README.md) — the runner clients + shared transport
- [`agent/README.md`](agent/README.md) — the multi-agent engine + MCP tool servers
- [`proxy/README.md`](proxy/README.md) — the API-compatibility proxy
- [`inference-server/README.md`](inference-server/README.md) — the LLM inference backend
- [`examples/README.md`](examples/README.md) — **how to run every example** (`hello`, `crewai-showcase`, `http-backends`, `agent-demos`)

## Key Features

| Feature | Description |
|---------|-------------|
| **Encrypted Communication** | AES-256-GCM encryption with key rotation via handshake protocol |
| **JWT Authentication** | Configurable public key or JWKS URL validation |
| **Task Lifecycle** | Upload, execute, monitor, stream, restart, terminate, redeploy |
| **Real-time Streaming** | Length-prefixed encrypted frames for live output |
| **MCP Integration** | Extensible tool access (filesystem, web search, custom) |
| **Pixi-native** | Tasks are Pixi projects; the server runs `pixi run <task>` in an isolated subprocess |
| **Air-gapped / Offline** | Bundle the resolved `.pixi/` environment with the code for dependency-free execution on disconnected hosts |
| **Pluggable agents** | Pluggable inference backend with optional CrewAI multi-agent integration |
| **API Compatibility** | API-compatible proxy layer for routing to external providers |
| **LZ4 Compression** | Efficient code packaging and transfer |
| **Configuration-driven** | YAML-based agent workflows and tool definitions |

## Getting Started

### Prerequisites

- Python — resolved per-project by Pixi (server ≥ 3.12, agent ≥ 3.13); no system Python needed
- [Pixi](https://pixi.sh/) package manager (recommended) or pip
- _(optional)_ an LLM backend for inference — local or remote

### Quickstart (local)

Start a server on loopback — no auth or keys required by default:

```bash
cd server && pixi install
pixi run python rixi_server.py --port 9000
```

Verify it's up:

```bash
curl http://localhost:9000/health
```

Then connect from another terminal. Package the bundled `examples/hello` Pixi project, ship it
to the server to execute, and stream the output back:

```bash
cd clients && pixi install
cd ../examples/hello
pixi run --manifest-path ../../clients/pixi.toml \
  python ../../clients/rixi_client.py --server http://localhost:9000 --task hello --auto-exit
```

By default the server binds **`127.0.0.1`** and runs **without** JWT auth or AES encryption
(convenient for local dev). To listen on a public interface you must enable auth
(`--public-key` / `--jwks-url`) or explicitly pass `--insecure`; binding a non-loopback host
with auth disabled is refused otherwise. Add `--aes-key` for encryption in production.

### Server Setup

```bash
cd server
pixi install
pixi run python rixi_server.py --port 9000
```

The server starts a FastAPI application that manages task execution, authentication, and encrypted communication. It listens on `127.0.0.1:<port>` by default; common flags: `--host` (bind address), `--insecure` (allow non-loopback bind without auth), `--public-key` / `--jwks-url` (JWT auth), `--aes-key` (encryption), `--max-upload-mb` (upload size cap), `--log-level`. Secrets can be supplied out-of-band via the `RIXI_KEY_SECRET` and `RIXI_AES_KEY` environment variables instead of CLI flags, so they don't appear in `ps` or shell history.

### Remote Install (one command)

[`install-rixi.sh`](install-rixi.sh) provisions the server onto a fresh **Linux or macOS** host over SSH from your machine. It installs Pixi if missing, copies `server/`, installs dependencies, and registers a user-level service (systemd `--user` on Linux, a launchd LaunchAgent on macOS) that auto-restarts — no root needed on the target.

```bash
# Key auth, default port 9000
./install-rixi.sh --host deploy@10.0.0.5 --key ~/.ssh/id_ed25519

# Password auth (needs sshpass), custom listen port
./install-rixi.sh --host root@box --ask-pass --rixi-port 8443

# Forward any rixi_server.py flags after "--"
./install-rixi.sh --host me@host --key ~/.ssh/id_rsa -- --log-level DEBUG
```

Secrets like `--key-secret` / `--aes-key` are detected among the forwarded args and written to a
mode-`600` env file on the target (referenced via `EnvironmentFile=` / launchd `EnvironmentVariables`)
rather than baked into the service definition, so they stay out of `ps` and world-readable unit files.

| Option | Purpose |
|--------|---------|
| `--host user@host` | Target SSH endpoint (or use `--user`) |
| `--ssh-port N` | SSH port (default 22) |
| `--key PATH` / `--password P` / `--ask-pass` | Auth: key file, inline password, or prompt |
| `--rixi-port N` | Port the server listens on (default 9000) |
| `--remote-dir DIR` | Install location (default `~/rixi`) |
| `--service-name NAME` | Service/unit name (default `rixi`) |
| `--no-start` | Install only; don't start the service |
| `-- <args>` | Everything after `--` is forwarded to `rixi_server.py` |

Run `./install-rixi.sh --help` for the full list. Password auth requires `sshpass` on your machine (`brew install sshpass` / `apt install sshpass`).

**Targets with no `curl`/`wget`:** the script probes the target for a downloader. If neither is present (and pixi isn't already installed), it downloads the self-contained `pixi` binary for the target's OS/arch on *your* machine and pushes it over SSH — pixi carries its own HTTPS client, so the target needs no downloader to finish setup. This requires `curl` or `wget` on your control machine instead.

### Client Usage

```bash
cd clients
pixi install

# Full-featured client with interactive menu (run from the Pixi project you want to ship)
pixi run python rixi_client.py --server http://localhost:9000 --task my_task

# Lightweight client
pixi run python rixi_simple_client.py --server http://localhost:9000
```

The full client packages the **current directory** (which must be a Pixi project) and runs the
named `--task` from its `pixi.toml` on the server. The CrewAI/LangChain showcase is **not**
required for normal client use — it lives as a standalone, optional demo in
[`examples/crewai-showcase/`](examples/crewai-showcase/).

### Agent, Proxy & Inference Setup

The agent engine, the API-compatibility proxy, and the inference backend are three independent
Pixi projects — install whichever you need:

```bash
# Multi-agent engine (single entry point; defaults to agent_config.example.yaml)
cd agent && pixi install && pixi run agent

# API-compatibility proxy (OpenAI/Anthropic/Ollama → RIXI backend)
cd proxy && pixi install && pixi run proxy -- --config proxy_config.example.yaml

# Inference backend (HuggingFace / Ollama)
cd inference-server && pixi install && pixi run start
```

### Configuration

Each component ships a small **`*.example.yaml` template** next to its code; copy it and fill in
real values:

- **`agent/agent_config.example.yaml`** - MCP servers, workflows, and tool definitions (default for `start_agent.py`)
- **`proxy/proxy_config.example.yaml`** - Proxy backends, model mapping, and deployment settings

Fully-populated demo configs (multiple workflows, the haiku pipeline) live with the demos that
use them in [`examples/agent-demos/`](examples/agent-demos/). The inference server is configured
entirely by environment variables (see [`inference-server/README.md`](inference-server/README.md)).

Clients read a `pixi_remote_config.toml` from the working directory for the server URL and
bearer token; copy [`agent/pixi_remote_config.toml.example`](agent/pixi_remote_config.toml.example)
and fill in real values (never commit them).

## Air-gapped / Offline Deployment

For targets with no internet access, the client can bundle the project's **resolved Pixi
environment** (`.pixi/`) into the upload, so the server runs the task without fetching or
solving any dependencies:

```bash
# package the .pixi environment together with the code, then deploy
pixi run python rixi_client.py --server https://host:9000 --task mytask --offline-mode

# inspect before sending
pixi run python rixi_client.py ... --validate-dependencies   # check .pixi is present & valid
pixi run python rixi_client.py ... --show-package-size       # size breakdown of the bundle
```

The bundle is tagged with an `.offline_metadata.json` marker; the server **auto-detects** offline
packages (by that marker or a bundled `.pixi/` folder) and skips dependency resolution entirely —
giving fully air-gapped, reproducible execution on disconnected hosts.

## Security

- **AES-256-GCM** encryption for data in transit (12-byte nonce + ciphertext)
- **JWT** token authentication with a pinned algorithm allow-list (no header-driven `alg`)
- **Key rotation** via handshake protocol (constant-time secret comparison)
- **Subprocess isolation** for task execution
- **Loopback-by-default** bind; non-loopback requires auth or an explicit `--insecure` opt-in
- **Hardened extraction**: uploaded archives are unpacked with path-traversal/symlink filtering, and task names are validated before reaching a shell
- **Upload caps** on both transferred and decompressed bytes (`--max-upload-mb`)
- Sensitive files (`*.secret`, `*.key`, `*.pem`, `logs/`) are excluded from version control; secrets can be passed via `RIXI_KEY_SECRET` / `RIXI_AES_KEY` env vars

See [SECURITY.md](SECURITY.md) for the vulnerability-reporting process and a deployment-hardening checklist.

## Deployment Modes

1. **Local Development** - All components on localhost
2. **Remote Server** - Server on cloud, clients connect via HTTP
3. **Encrypted** - AES-256 + JWT for production
4. **Hybrid** - Local MCP tools + remote inference compute
5. **Multi-provider** - Route to different LLM backends
6. **Air-gapped / Offline** - Dependencies bundled with the code; runs on hosts with no internet

## Dependencies

**Core** (server + clients):

- **FastAPI / Uvicorn** - HTTP server framework
- **cryptography / PyJWT** - AES-256 encryption and JWT authentication
- **LZ4** - Compression
- **PyYAML / tomli** - Configuration

**Optional** (separate components, not required by the server or core clients):

- **FastAPI / aiohttp** - the `proxy/` API-compatibility layer
- **transformers / PyTorch / accelerate** - the `inference-server/` model backend
- **CrewAI** - multi-agent orchestration (the `agent/` engine + the CrewAI showcase)

## License

[MIT](LICENSE) © [Kevin Keller](https://kevinkeller.org)
