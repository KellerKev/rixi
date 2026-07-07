# inference-openai — serve a model behind an OpenAI-compatible API

> **The problem:** you've got a model on a GPU box, but every tool you use (LangChain,
> LlamaIndex, the `openai` SDK) expects an OpenAI endpoint. **Why it's hard normally:** wire up
> a bespoke serving stack and a translation layer for each client. **How RIXI does it:** run the
> model as a keep-alive task, drop the proxy in front, and you have a drop-in OpenAI base URL —
> point any OpenAI-compatible tool at it, unchanged.

Deploy a model on a RIXI GPU box and call it like any OpenAI endpoint. Three moving parts:

- **backend** — [`inference-server/`](../../inference-server/) runs as a keep-alive RIXI task
  (HuggingFace `transformers` or Ollama).
- **proxy** — [`proxy/`](../../proxy/) sits in front of that task and speaks the
  OpenAI/Anthropic/Ollama wire formats.
- **caller** — this directory: [`call.py`](call.py), using the stock `openai` SDK.

## End-to-end

```bash
# 1) deploy the model backend as a long-lived task → note the task id
cd inference-server
rixi-client --server https://gpu-box:9000 --task start --keep-alive
#   Task ID: 1ce0-inference   Status: running

# 2) put the proxy in front of that task
cd ../proxy
pixi run proxy -- --backend https://gpu-box:9000 --inference-task 1ce0-inference --port 8002

# 3) call it with the OpenAI SDK (this example)
cd ../examples/inference-openai
RIXI_PROXY_URL=http://localhost:8002/v1 pixi run call "haiku about the sea"
```

Or with plain `curl`:

```bash
curl http://localhost:8002/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-3.5-turbo","messages":[{"role":"user","content":"haiku about the sea"}]}'
```

Because the proxy is OpenAI-compatible, any tool that speaks OpenAI (LangChain, LlamaIndex,
the `openai` SDK, `llm`, …) can point its base URL at `http://localhost:8002/v1` and use your
self-hosted model unchanged.
