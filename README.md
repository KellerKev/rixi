<p align="center">
  <img src="assets/rixi-logo.svg" alt="RIXI" width="300">
</p>

# RIXI

A cloud-native AI agent framework for secure, remote execution of AI workloads. RIXI provides encrypted, authenticated task execution with [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) integration for extensible tool access and a pluggable inference backend.

## Architecture

```
CLIENT LAYER                  SERVER LAYER              AGENT LAYER
────────────                  ────────────              ───────────
┌──────────────┐              ┌──────────────┐          ┌──────────────────┐
│ rixi_client  │──HTTP/AES───▶│ rixi_server  │◀─────────│ inference_server │
│ (full CLI,   │              │ (FastAPI)    │          │ (LLM backend)    │
│  MCP-capable)│              │              │          └──────────────────┘
└──────────────┘              │ - JWT Auth   │          ┌──────────────────┐
┌──────────────┐              │ - AES-256    │          │ proxy_server     │
│ simple_client│──Encrypted──▶│ - Task Mgmt  │          │ (API-compat layer)│
│ (lightweight)│   Channel    │ - Streaming  │          └──────────────────┘
└──────────────┘              │ - Compression│          ┌──────────────────┐
                              │ - MCP Support│          │ crewai_integration│
                              └──────────────┘          │ (multi-agent)    │
                                                        └──────────────────┘
```

**Workflow:** Client authenticates (JWT) -> optional AES handshake -> packages code (tar + LZ4) -> uploads to server -> server executes in isolated subprocess -> real-time output streaming back to client.

## Directory Structure

```
rixi/
├── server/              # FastAPI task execution server
│   ├── rixi_server.py   # Main server: auth, encryption, task management
│   └── pixi.toml        # Server dependencies
├── clients/             # Client implementations
│   ├── rixi_client.py        # Full-featured CLI client (MCP-capable)
│   ├── rixi_simple_client.py # Lightweight client
│   ├── main.py               # CrewAI showcase client
│   └── pixi.toml             # Client dependencies
├── agent/               # Agent framework, tools & inference
│   ├── ai_agent_framework.py # Base agent/channel abstractions
│   ├── agent.py / client.py  # Core agent + client utilities
│   ├── mcp_agent.py          # Config-driven MCP agent with workflows
│   ├── mcp_manager.py        # MCP server lifecycle manager
│   ├── mcp_filesystem.py     # Filesystem tool server
│   ├── mcp_web_search.py     # Web search tool server
│   ├── inference_server.py   # Pluggable LLM inference backend
│   ├── proxy_server.py       # API-compatibility proxy
│   ├── enhanced_proxy_wrapper.py # Extended proxy with metrics
│   ├── crewai_integration.py # CrewAI + MCP bridge
│   ├── simple_agent.py       # Single-purpose agent
│   ├── start_agent.py        # Agent entry point
│   ├── start_agent_mcp.py    # MCP agent entry point
│   ├── *.yaml                # Sample configs (see Configuration)
│   ├── examples/             # Standalone demo scripts
│   ├── tests/                # Test scripts
│   └── pixi.toml             # Agent dependencies
├── install-rixi.sh      # One-command remote installer (Linux/macOS over SSH)
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
| **Pluggable agents** | Pluggable inference backend with optional CrewAI multi-agent integration |
| **API Compatibility** | API-compatible proxy layer for routing to external providers |
| **LZ4 Compression** | Efficient code packaging and transfer |
| **Configuration-driven** | YAML-based agent workflows and tool definitions |

## Getting Started

### Prerequisites

- Python 3.13 (managed automatically by Pixi)
- [Pixi](https://pixi.sh/) package manager (recommended) or pip
- _(optional)_ an LLM backend for inference — local or remote

### Quickstart (local)

Start a server on `localhost` — no auth or keys required by default:

```bash
cd server && pixi install
pixi run python rixi_server.py --port 9000
```

Verify it's up:

```bash
curl http://localhost:9000/health
```

Then connect from another terminal. The full client packages the current directory, ships it
to the server to execute, streams output back, and opens an interactive menu:

```bash
cd clients && pixi install
pixi run python rixi_client.py --server http://localhost:9000 --task hello
```

The server runs **without** JWT auth or AES encryption by default (convenient for local dev);
enable them with `--public-key` / `--aes-key` for remote or production use.

### Server Setup

```bash
cd server
pixi install
pixi run python rixi_server.py --port 9000
```

The server starts a FastAPI application that manages task execution, authentication, and encrypted communication. It listens on `0.0.0.0:<port>`; common flags: `--public-key` / `--jwks-url` (JWT auth), `--aes-key` (encryption), `--log-level`.

### Remote Install (one command)

[`install-rixi.sh`](install-rixi.sh) provisions the server onto a fresh **Linux or macOS** host over SSH from your machine. It installs Pixi if missing, copies `server/`, installs dependencies, and registers a user-level service (systemd `--user` on Linux, a launchd LaunchAgent on macOS) that auto-restarts — no root needed on the target.

```bash
# Key auth, default port 9000
./install-rixi.sh --host deploy@10.0.0.5 --key ~/.ssh/id_ed25519

# Password auth (needs sshpass), custom listen port
./install-rixi.sh --host root@box --ask-pass --rixi-port 8443

# Forward any rixi_server.py flags after "--"
./install-rixi.sh --host me@host --key ~/.ssh/id_rsa -- --log-level DEBUG --key-secret s3cr3t
```

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

# Full-featured client with interactive menu
pixi run python rixi_client.py --server http://localhost:9000 --task my_task

# Lightweight client
pixi run python rixi_simple_client.py --server http://localhost:9000
```

### Agent Setup

```bash
cd agent
pixi install

# Start MCP-enabled agent
pixi run python start_agent_mcp.py

# Start basic agent
pixi run python start_agent.py
```

### Configuration

Agent behavior is driven by YAML configuration files:

- **`agent_config.yaml`** - MCP servers, workflows, and tool definitions
- **`haiku_config.yaml`** - Haiku generation prompts and post-processors
- **`mcp_config.yaml`** - MCP feature toggles
- **`proxy_config.yaml`** - Enterprise proxy deployment templates

## Security

- **AES-256-GCM** encryption for data in transit (12-byte nonce + ciphertext)
- **JWT** token authentication with configurable verification
- **Key rotation** via handshake protocol
- **Subprocess isolation** for task execution
- Sensitive files (`*.secret`, `*.key`) are excluded from version control

## Deployment Modes

1. **Local Development** - All components on localhost
2. **Remote Server** - Server on cloud, clients connect via HTTP
3. **Encrypted** - AES-256 + JWT for production
4. **Hybrid** - Local MCP tools + remote inference compute
5. **Multi-provider** - Route to different LLM backends

## Dependencies

**Core** (server + clients):

- **FastAPI / Uvicorn** - HTTP server framework
- **cryptography / PyJWT** - AES-256 encryption and JWT authentication
- **LZ4** - Compression
- **PyYAML / tomli** - Configuration

**Optional** (agent integrations, not required by the server or core clients):

- **CrewAI** - multi-agent orchestration (used by the CrewAI integration/showcase only)
- **transformers / PyTorch / accelerate** - local model inference

## License

[MIT](LICENSE) © [Kevin Keller](https://kevinkeller.org)
