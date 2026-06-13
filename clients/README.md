# RIXI Clients

Command-line clients that package the current directory (a Pixi project) as tar + LZ4, ship it to
a RIXI server to execute, and stream the output back.

- **`rixi_client.py`** — full client: interactive menu, env-var injection, MCP back-channel,
  HTTP reverse proxy, offline-mode bundling, encryption + JWT.
- **`rixi_simple_client.py`** — lightweight client: upload, stream, attach, a minimal Ctrl-C menu.
- **`rixi_transport.py`** — the shared transport both clients use (AES framing, length-prefixed
  streaming, the handshake, the upload/attach helpers); a `Transport` object holds `server_url`,
  `aes_key`, `auth_headers`, `task_id`.

## Run

```bash
pixi install

# Full client (run from the Pixi project you want to ship)
pixi run python rixi_client.py --server http://localhost:9000 --task my_task

# Lightweight client
pixi run python rixi_simple_client.py --server http://localhost:9000
```

## Key flags (full client)

| Flag | Purpose |
|------|---------|
| `--server URL` / `--task NAME` | Server to deploy to + the `pixi.toml` task to run |
| `--attach ID` / `--attach-history ID` | Attach to an existing task's live stream |
| `--aes-key PATH` / `--rotate-secret` | AES-256-GCM encryption + key rotation |
| `--bearer-token` | JWT bearer for authenticated servers |
| `--offline-mode` | Bundle the resolved `.pixi/` env for air-gapped hosts |
| `--validate-dependencies` / `--show-package-size` | Inspect the bundle before sending |
| `--with-mcp` / `--with-external-mcp` | Enable the MCP back-channel / external routing |
| `--server-from-token` | (opt-in) derive the server URL from JWT claims |

## Configuration

Clients read an optional `pixi_remote_config.toml` from the working directory for the server URL
and bearer token. Copy [`../agent/pixi_remote_config.toml.example`](../agent/pixi_remote_config.toml.example)
and fill in real values (never commit them).
