# RIXI Agent (multi-agent engine)

The agent engine: a configuration-driven agent that calls MCP tools and routes generation to a
remote inference backend over the RIXI channel. It runs as a task payload (or locally against a
server) and is independent of the [proxy](../proxy/) and [inference-server](../inference-server/).

## Architecture

A workflow step either calls a **local MCP tool** (filesystem, web search) to gather context, or
sends a generation request to a **remote model task** over the RIXI channel and reads the reply.

```
   ┌──────────────────────────┐
   │      start_agent.py      │   loads a workflow config, runs each step
   │  (ConfigurableMCPAgent)  │
   └──────────────────────────┘
   each step does one of:
     • call a local MCP tool (mcp_servers) to gather context, then
     • generate remotely:  POST /task/{id}/input   {"command":"generate",…}
                           GET  /task/{id}/stream  ◀ {"response":…,"request_id":…}
   …and the result feeds the next step.
```

## Modules

| File | Responsibility |
|------|----------------|
| `ai_agent_framework.py` | Core: AES-GCM helpers, the `RemoteChannel` (encrypted, length-prefixed streaming), the `Agent`/`HaikuAgent` base classes, and `read_pixi_config` / `create_auth_headers`. |
| `mcp_manager.py` | MCP server lifecycle, tool dispatch, config factories, and the YAML config loader. |
| `mcp_servers.py` | The MCP tool servers (filesystem + web search), launched as subprocesses. |
| `mcp_agent.py` | `ConfigurableMCPAgent` — the workflow engine (tool calls, generation, context resolution, post-processing). |
| `start_agent.py` | `GenericAgentRunner` — the CLI entry point that loads a config and runs a workflow. |

## Run

```bash
pixi install
pixi run agent            # = python start_agent.py (defaults to agent_config.example.yaml)
pixi run test             # pytest tests/
```

`start_agent.py` takes `--task-id`, `--aes-key`, `--config`, `--workflow`, `--topic`, etc. To run
against a model serving in a keep-alive task, pass that task's id:

```console
$ pixi run python start_agent.py \
    --server https://gpu-box:9000 --task-id 1ce0-inference \
    --config agent_config.example.yaml --workflow research_workflow --topic "fusion energy"
```

## MCP tool servers

The manager spawns each tool server as its own subprocess by module name:

```bash
python -m mcp_servers filesystem /workspace   # read/write/list/create within a root
python -m mcp_servers web_search              # DuckDuckGo/Searx/Google (honest failure if none)
```

These command arrays appear in the `mcp.servers[].command` entries of the config files — keep
them in sync with the module if you rename it.

## Configuration

`agent_config.example.yaml` is the in-place template (the default for `start_agent.py` and what
the tests load). Fully-populated demo configs live with the demos in
[`../examples/agent-demos/`](../examples/agent-demos/). Two alternative ways to drive this engine
— a lightweight `simple_agent.py` and a `crewai_integration.py` bridge — also live there.
