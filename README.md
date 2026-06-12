<p align="center">
  <img src="assets/rixi-logo.svg" alt="RIXI" width="300">
</p>

# RIXI

A cloud-native AI agent framework for secure, remote execution of AI workloads. RIXI provides encrypted, authenticated task execution with [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) integration for extensible tool access and a pluggable inference backend.

## Architecture

```
CLIENT LAYER                  SERVER LAYER              COMPUTE COMPONENTS
────────────                  ────────────              ──────────────────
┌──────────────┐              ┌──────────────┐          ┌──────────────────┐
│ rixi_client  │──HTTP/AES───▶│ rixi_server  │◀─────────│ inference-server │
│ (full CLI,   │              │ (FastAPI)    │          │ (LLM backend)    │
│  MCP-capable)│              │              │          └──────────────────┘
└──────────────┘              │ - JWT Auth   │          ┌──────────────────┐
┌──────────────┐              │ - AES-256    │          │ proxy            │
│ simple_client│──Encrypted──▶│ - Task Mgmt  │          │ (API-compat layer)│
│ (lightweight)│   Channel    │ - Streaming  │          └──────────────────┘
└──────────────┘              │ - Compression│          ┌──────────────────┐
                              │ - MCP Support│          │ agent            │
                              └──────────────┘          │ (multi-agent eng)│
                                                        └──────────────────┘
```

The **agent**, **proxy**, and **inference-server** are independent components, each its own Pixi
project; mix and match them (or none of them) with the server + clients core.

**Workflow:** Client authenticates (JWT) -> optional AES handshake -> packages code (tar + LZ4) -> uploads to server -> server executes in isolated subprocess -> real-time output streaming back to client.

## Built on Pixi

RIXI is a remote runner for [Pixi](https://pixi.sh/) projects — the name is a nod to it. A task
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
│   ├── ai_agent_framework.py # Base agent/channel abstractions
│   ├── remote_channel.py     # Sync streaming channel to the server
│   ├── aesgcm.py             # AES-GCM encrypt/decrypt helpers
│   ├── mcp_agent.py          # Config-driven MCP agent with workflows
│   ├── simple_agent.py       # Single-purpose agent
│   ├── mcp_manager.py        # MCP server lifecycle manager
│   ├── mcp_filesystem.py     # Filesystem tool server
│   ├── mcp_web_search.py     # Web search tool server
│   ├── crewai_integration.py # CrewAI + MCP bridge
│   ├── start_agent.py        # Single agent entry point / runner
│   ├── agent_config.example.yaml # Config template (see Configuration)
│   ├── tests/                # pytest suite (run with `pixi run test`)
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
