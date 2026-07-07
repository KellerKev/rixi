# RIXI Proxy

An API-compatibility layer that exposes OpenAI / Anthropic / Ollama-shaped HTTP endpoints and
routes them to a RIXI inference backend (a task running on a RIXI server). Adds optional API-key
auth, response caching, metrics, model-name mapping, and a generic HTTP passthrough.

## Architecture

The proxy translates standard API calls into the RIXI channel protocol: it POSTs the prompt to
the backend task's stdin and polls the task for the matching response, then formats the reply
back into the caller's API shape.

```
   OpenAI / Anthropic / Ollama client
        │  POST /v1/chat/completions  (or /api/generate, /v1/messages…)
        ▼
   ┌────────────────┐
   │     proxy      │
   │   (FastAPI)    │
   └───────┬────────┘
           │  POST /task/{id}/input   then poll /task/{id} for the request_id
           ▼
   ┌────────────────┐
   │ inference task │
   │ on rixi_server │
   └────────────────┘
```

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
| `--port` / `--host` | Listen address (default `127.0.0.1:8002`; pass `--host 0.0.0.0` to expose it) |
| `--api-key` | Require this key on `/v1/*`, `/api/*`, passthrough routes (`/health` stays open) |
| `--aes-key PATH` / `--no-decrypt` | Decrypt encrypted backend responses |
| `--mode {openai,anthropic,generic}` | Default API surface hint |

## Endpoints

OpenAI (`/v1/chat/completions`, `/v1/responses`, `/v1/models`), Anthropic (`/v1/messages`),
Ollama (`/api/generate`, `/api/chat`, `/api/tags`, `/api/show`), generic (`/generate`,
`/stream`), health (`/health`), metrics/admin (`/metrics`, `/admin/*`), and an HTTP passthrough.

## Example

Point the proxy at a running inference task (see [inference-server](../inference-server/)) and
call it like any OpenAI endpoint:

```console
$ pixi run proxy -- --backend https://gpu-box:9000 --inference-task 1ce0-inference --port 8002
📡 Server: 127.0.0.1:8002

$ curl http://localhost:8002/v1/chat/completions \
    -H 'content-type: application/json' \
    -d '{"model":"gpt-3.5-turbo","messages":[{"role":"user","content":"haiku about the sea"}]}'
{"choices":[{"message":{"role":"assistant","content":"Salt wind on the waves…"}}], ...}
```
