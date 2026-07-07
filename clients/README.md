# RIXI Clients

Command-line clients that package the current directory (a Pixi project) as tar + LZ4, ship it to
a RIXI server to execute, and stream the output back.

- **`rixi_client.py`** — full client: interactive menu, env-var injection, MCP back-channel,
  HTTP reverse proxy, offline-mode bundling, encryption + JWT.
- **`rixi_simple_client.py`** — lightweight client: upload, stream, attach, a minimal Ctrl-C menu.
- **`rixi_transport.py`** — the shared transport both clients use (AES framing, length-prefixed
  streaming, the handshake, the upload/attach helpers); a `Transport` object holds `server_url`,
  `aes_key`, `auth_headers`, `task_id`.

## Architecture

```
   your project dir                          rixi_server
   (a pixi.toml)                              :9000
   ┌──────────────┐                        ┌──────────────┐
   │ rixi_client  │ ── package + POST ────▶│  /upload     │
   │              │    /upload             │              │
   │ live output  │ ◀── /task/{id}/stream ─│  task <id>   │
   │ Ctrl+C menu  │ ── /restart /redeploy ▶│              │
   └──────────────┘    /input · DELETE     └──────────────┘
```

The client packages the **current directory**, so you run it from the project you want to ship.
With `--keep-alive` the server keeps the task addressable by its id, and you can reconnect later
with `--attach` / `--attach-history`.

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
| `--header 'K: V'` / `--headers-file PATH` | Extra HTTP headers on every request (see below) |

## Custom request headers

Send arbitrary HTTP headers on **every** request to the server — handy when RIXI sits behind an
auth proxy, API gateway, or load balancer that expects custom headers. Headers come from three
sources (low→high precedence: config < file < CLI), and values support `${env:VAR}` and
`${file:path}` expansion so external programs can supply secrets via the environment or a file
without editing commands:

```bash
# 1) ad-hoc, repeatable
pixi run python rixi_client.py --server … --task deploy \
  --header 'X-Tenant: acme' --header "X-Trace-Id: $(uuidgen)"

# 2) a JSON template an external program writes (auto-loaded if named rixi_headers.json)
cp rixi_headers.example.json rixi_headers.json   # then your tooling fills it in
#   { "X-Tenant": "acme", "Authorization": "Bearer ${env:RIXI_TOKEN}" }
RIXI_TOKEN=… pixi run python rixi_client.py --server … --task deploy

# 3) a [config.headers] table in pixi_remote_config.toml (see the .example)

# preview what would be sent (secrets masked) without connecting:
pixi run python rixi_client.py --show-headers
```

Per-request headers (the `Authorization` from `--bearer-token`, `Content-Type`) still take
precedence, so this is purely additive. `rixi_headers.json` is gitignored (it may hold secrets);
the lightweight client supports `--header` / `--headers-file` too.

## Deploy, detach, attach

A normal deploy prints package stats, a task id, and the live stream:

```console
$ pixi run python rixi_client.py --server http://localhost:9000 --task hello --keep-alive

📦 Package Statistics:
  Code files: 3
  Compressed package: 0.0 MB
  🌐 Mode: Online (dependencies will be downloaded)

Task ID: c4c63123-b4d6-4e73-84ac-8789b7e5d9d4
Status: Extracting to /tmp/tmp15e7ox_z
Hello from RIXI! Running on Python 3.14.6.
Status: Process completed
```

Press **Ctrl+C** any time for the options menu:

```text
Interrupted! Choose:
1) Terminate remote task and exit
2) Let task continue and exit
3) Restart remote task and exit
4) Redeploy current code to task
5) Continue monitoring
6) Restart task and keep monitoring
Enter 1-6:
```

Reconnect to a kept-alive task later — `--attach` follows live output only; `--attach-history`
replays the captured output first, then follows:

```console
$ pixi run python rixi_client.py --server http://localhost:9000 --attach-history c4c63123-…
```

`--keep-alive` holds the back-channel open until you exit; `--timeout N` exits N seconds after the
task completes; `--auto-exit` exits as soon as operations finish.

## Configuration

Clients read an optional `pixi_remote_config.toml` from the working directory for the server URL
and bearer token. Copy [`../agent/pixi_remote_config.toml.example`](../agent/pixi_remote_config.toml.example)
and fill in real values (never commit them).
