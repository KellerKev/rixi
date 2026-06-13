# RIXI Server

The FastAPI task-execution server: it accepts an uploaded Pixi project (tar + LZ4), unpacks it
into an isolated temp dir, runs `pixi run <task>` in a subprocess, and streams the output back —
with optional JWT auth and AES-256-GCM encryption.

## Run

```bash
pixi install
pixi run python rixi_server.py --port 9000
curl http://localhost:9000/health
```

## Secure by default

- Binds **`127.0.0.1`** by default. To listen on a public interface you must either enable JWT
  auth (`--public-key` / `--jwks-url`) or pass `--insecure` explicitly.
- Uploaded archives are extracted with path-traversal/symlink filtering; task names are validated
  before reaching a shell; uploads are size-capped (`--max-upload-mb`, default 2048).

## Key flags

| Flag | Purpose |
|------|---------|
| `--host` / `--port` | Listen address (default `127.0.0.1:9000`) |
| `--insecure` | Allow a non-loopback bind without auth |
| `--public-key PATH` / `--jwks-url URL` | JWT verification (pinned alg allow-list) |
| `--aes-key PATH` / `--gen-aes` | AES-256-GCM encryption (use/generate a key) |
| `--key-secret` / `--key-secret-uses` | Handshake secret for key rotation |
| `--max-upload-mb` | Hard cap on transferred + decompressed bytes |
| `--log-level` / `--log-dir` | Structured JSON logging |

Secrets can be supplied out-of-band via the `RIXI_KEY_SECRET` and `RIXI_AES_KEY` environment
variables instead of CLI flags, so they don't appear in `ps` or shell history.

## Remote install

[`../install-rixi.sh`](../install-rixi.sh) provisions the server onto a fresh Linux/macOS host
over SSH (installs Pixi, copies `server/`, registers a user-level auto-restart service). See the
top-level [README](../README.md#remote-install-one-command).
