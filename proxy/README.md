# RIXI Proxy

An API-compatibility layer that exposes OpenAI / Anthropic / Ollama-shaped HTTP endpoints and
routes them to a RIXI inference backend (a task running on a RIXI server). Adds optional API-key
auth, response caching, metrics, model-name mapping, and a generic HTTP passthrough.

## Run

```bash
pixi install

# A) From a config file (backends, model mapping, auth, CORS, …)
pixi run proxy -- --config proxy_config.example.yaml

# B) Single-backend shorthand (no config file)
pixi run proxy -- --backend http://localhost:9000 --inference-task <TASK_ID> --port 8002
```

Copy [`proxy_config.example.yaml`](proxy_config.example.yaml) and edit it for a real deployment.

## Key flags

| Flag | Purpose |
|------|---------|
| `--config PATH` | YAML/JSON config (backends, model map, auth, CORS) |
| `--backend URL` / `--inference-task ID` / `--server URL` | Single-backend shorthand (used when `--config` is absent) |
| `--port` / `--host` | Listen address (default `0.0.0.0:8002`) |
| `--api-key` | Require this key on `/v1/*`, `/api/*`, passthrough routes (`/health` stays open) |
| `--aes-key PATH` / `--no-decrypt` | Decrypt encrypted backend responses |
| `--mode {openai,anthropic,generic}` | Default API surface hint |

## Endpoints

OpenAI (`/v1/chat/completions`, `/v1/responses`, `/v1/models`), Anthropic (`/v1/messages`),
Ollama (`/api/generate`, `/api/chat`, `/api/tags`, `/api/show`), generic (`/generate`,
`/stream`), health (`/health`), metrics/admin (`/metrics`, `/admin/*`), and an HTTP passthrough.
