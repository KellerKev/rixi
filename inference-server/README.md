# RIXI Inference Server

A small, pluggable LLM inference backend. It reads JSON requests on stdin, generates a
completion (via HuggingFace `transformers` or a local Ollama), and writes a JSON response on
stdout — designed to run as a RIXI **task payload** behind the [proxy](../proxy/), which turns
OpenAI/Anthropic/Ollama HTTP calls into these requests.

## Run

```bash
pixi install
pixi run start
```

`transformers`/`torch` are imported lazily, so the Ollama path and the model-not-found path work
without the heavy ML stack loaded.

## Configuration (environment variables)

| Variable | Default | Meaning |
|----------|---------|---------|
| `MODEL_TYPE` | `huggingface` | `huggingface` or `ollama` |
| `MODEL_NAME` | `TinyLlama/TinyLlama-1.1B-Chat-v1.0` | Model to load/serve |
| `DEFAULT_TEMPERATURE` | `0.7` | Sampling temperature |
| `DEFAULT_MAX_LENGTH` | `200` | Max new tokens |
| `SHOW_AVAILABLE_MODELS` | `true` | Include the model list in error responses |
| `MODEL_NOT_FOUND_MESSAGE` | `Model not available` | Message when a requested model is missing |

## Request / response

Input is one JSON object per request on stdin (`prompt`, optional `model`, `temperature`,
`max_length`, `request_id`); the server emits a JSON object with the generated `response` and the
echoed `request_id`.
