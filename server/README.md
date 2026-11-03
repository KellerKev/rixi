# RIXI Server

The FastAPI task-execution server: it accepts an uploaded Pixi project (tar + LZ4), unpacks it
into an isolated temp dir, runs `pixi run <task>` in a subprocess, and streams the output back —
with optional JWT auth and AES-256-GCM encryption.

## Architecture

```
   ┌──────────────┐
   │    client    │
   └──────┬───────┘
          │  POST /upload  (tar+LZ4, task_name, keep_alive)
          ▼
   ┌──────────────┐
   │ rixi_server  │   auth · decrypt · extract · manage the task
   │  (FastAPI)   │   stream stdout/stderr via /task/{id}/stream
   └──────┬───────┘
          │  pixi run <task>
          ▼
   ┌──────────────┐
   │   isolated   │
   │  subprocess  │
   └──────────────┘
```

## Run

```bash
pixi install
pixi run python rixi_server.py --port 9000
curl http://localhost:9000/health
```

## Secure by default

- Binds **`127.0.0.1`** by default. To listen on a public interface you must either enable JWT
  auth (`--public-key` / `--jwks-url`) or pass `--insecure` explicitly.
- `/health` is the only unauthenticated endpoint. `/status` needs a token because it carries recent
  task output.
- When many servers trust one issuer (a fleet), scope each server's tokens with `--audience` so a
  token for one box cannot run code on another.
- Uploaded archives are extracted with path-traversal/symlink filtering; task names are validated
  before reaching a shell; uploads are size-capped (`--max-upload-mb`, default 2048).

## Key flags

| Flag | Purpose |
|------|---------|
| `--host` / `--port` | Listen address (default `127.0.0.1:9000`) |
| `--insecure` | Allow a non-loopback bind without auth |
| `--public-key PATH` / `--jwks-url URL` | JWT verification (pinned alg allow-list) |
| `--audience AUD` | Require `aud` = AUD (and an `exp`), so a token minted for another server that trusts the same issuer is refused |
| `--required-claim NAME=VALUE` | Require a claim to match (repeatable), e.g. `tenant=42` |
| `--require-exp` | Refuse tokens with no `exp` (implied by `--audience`) |
| `--revoked-jti-file PATH` | Revoked token ids, one per line, re-read on change; tokens without a `jti` are then refused |
| `--aes-key PATH` / `--gen-aes` | AES-256-GCM encryption (use/generate a key) |
| `--key-secret` / `--key-secret-uses` | Handshake secret for key rotation |
| `--max-upload-mb` | Hard cap on transferred + decompressed bytes |
| `--log-level` / `--log-dir` | Structured JSON logging |

Secrets can be supplied out-of-band via the `RIXI_KEY_SECRET` and `RIXI_AES_KEY` environment
variables instead of CLI flags, so they don't appear in `ps` or shell history.

## Commands and files (sandboxed environments)

An agent drives a box with two primitives, both confined to one workspace root
(`--workspace`, default `/workspace`) and needing the same token as everything else:

| Endpoint | Purpose |
|----------|---------|
| `POST /exec` `{cmd, cwd?, env?, timeout?, stdin?}` | Run a command (a string goes through `bash -lc`, a list runs without a shell); streams `{"stdout"}`/`{"stderr"}` frames, then `{"exit_code", "status", "duration_s"}`. Killed at `timeout` (default 600 s). |
| `POST /exec` `{…, background: true}` | Start it in the background and answer `{"task_id"}` at once; it is a task from then on (attach, stdin, restart, kill). `timeout` is optional here. |
| `PUT /files?path=` | Write a file (parents created, written atomically) |
| `GET /files?path=` · `GET /files/list?path=` · `DELETE /files?path=` | Read, list, delete |

Paths that resolve outside the workspace — `..`, absolute paths elsewhere, symlinks pointing
out — are refused. With an AES key, bodies and responses are sealed (`X-Rixi-Encrypted: 1`); with
`--require-encryption`, plaintext bodies are refused. Running commands count as busy in
`/health` (only tasks whose process is still running count — a finished task kept for its
output does not keep a box busy), and every authenticated request bumps `activity`, so a box an
agent is driving with short commands is not mistaken for idle.

Preview URLs (on a box with its own name and Caddy in front, as hosted boxes have;
`--preview-domain`): `POST /previews {port, public?}` serves the port at
`https://<port>.<name>/` and returns a share link; `GET /previews`, `DELETE /previews/{port}`.
Caddy gets certificates on demand only for opened ports (`/preview/ask`) and checks every
request (`/preview/auth`); both answer only to Caddy, which holds the box's gate secret.

From the SDK: `Client.exec()`, `exec_stream()`, `write_file()`, `read_file()`, `list_files()`,
`delete_file()`. From the CLI: `rixi exec`, `rixi put`, `rixi get`, `rixi ls`.

## Task lifecycle & attaching later

A task uploaded with the `keep_alive=true` form field stays registered under a generated **task
id** after the upload response returns, so clients (and the proxy/agent) can reconnect to it:

| Endpoint | Purpose |
|----------|---------|
| `POST /upload` | Upload a packaged project (`file`, `task_name`, `keep_alive`); spawns the task and streams extraction + execution. With `detach=true` it answers `{"task_id"}` at once and the task runs on, kept |
| `GET /tasks` | Every task by id: status (refreshed), exit code, `kind` (`task` or `exec`), name |
| `GET /task/{id}` | Task status JSON: `status`, `exit_code`, `recent_output`, `deployment_type`, `package_stats` |
| `GET /task/{id}/stream` | Live output — replays the backlog (bounded `output_lines` deque) then follows; `?history=false` follows only new output |
| `GET /task/{id}/output` | Full captured output history (non-streaming) |
| `POST /task/{id}/input` | Write JSON/text to the task's stdin (how the proxy/agent feed a task); `?raw=1` writes the body as-is, for programs that read plain lines |
| `POST /task/{id}/restart` · `POST /task/{id}/redeploy` · `DELETE /task/{id}` | Restart in place · push new code · terminate |
| `ANY /task/{id}/proxy/{path}` | Forward an HTTP request to the task (for tasks that speak RIXI's request envelope, e.g. `examples/http-backends`) |
| `GET /health` · `GET /tasks` · `GET /deployment/stats` | Server health · list tasks · offline/online stats |
| `POST /handshake` · `POST /handshake/finish` | AES key exchange / rotation |

Output is sent as length-prefixed frames (AES-encrypted when a key is set), carrying JSON objects
like `{"status": "..."}`, `{"output": "...\n"}`, and `{"stderr": "...\n"}`.

## Example

```console
$ pixi run python rixi_server.py --port 9000
$ curl -s localhost:9000/health
{"status":"healthy","features":["http_proxy","aes_encryption","jwt_auth","mcp_support","offline_deployment"],...}

# after a client deploys with --keep-alive, inspect the task by id:
$ curl -s localhost:9000/task/7b3e1c90-2a4f-4d11-9c2a-1f6e8b0a5d33
{"status":"running","task_name":"serve","deployment_type":"online",
 "recent_output":[{"type":"output","content":"INFO: Uvicorn running on http://0.0.0.0:8080\n"}], ...}
```

## Remote install

[`../install-rixi.sh`](../install-rixi.sh) provisions the server onto a fresh Linux/macOS host
over SSH (installs Pixi, copies `server/`, registers a user-level auto-restart service). See the
top-level [README](../README.md#remote-install-one-command).
