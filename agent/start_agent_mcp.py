# start_agent_mcp.py - Thin entry point for the configurable MCP agent
import argparse
import asyncio
import base64
import yaml
from pathlib import Path

from ai_agent_framework import read_pixi_config, create_auth_headers, RemoteChannel
from mcp_agent import ConfigurableMCPAgent
from mcp_manager import load_server_configs_from_dict

def load_aes_key(key_path: str):
    """Load and validate base64-encoded AES key from file"""
    if not key_path:
        return None
    raw = Path(key_path).read_bytes().strip()
    aes_key = base64.b64decode(raw)
    if len(aes_key) != 32:
        raise ValueError("Invalid AES key length")
    return aes_key

def load_agent_config(config_path: str) -> dict:
    """Load the first document from a (possibly multi-document) YAML config"""
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_file, "r") as f:
        configs = list(yaml.safe_load_all(f))
    if not configs or configs[0] is None:
        raise ValueError(f"Empty config file: {config_path}")
    return configs[0]

async def run(args) -> int:
    config = load_agent_config(args.config)

    pixi_config = read_pixi_config()
    bearer_token = pixi_config.get("bearer_token") or pixi_config.get("bearer-token")
    snowflake_token = pixi_config.get("snowflake_token") or pixi_config.get("snowflake-token")
    auth_headers = create_auth_headers(bearer_token, snowflake_token)

    aes_key = load_aes_key(args.aes_key)
    channel = RemoteChannel(args.server, args.task_id, aes_key, auth_headers)

    agent = ConfigurableMCPAgent(channel, config)

    try:
        mcp_config = config.get("mcp", {})
        if mcp_config:
            server_configs = load_server_configs_from_dict(mcp_config)
            await agent.initialize_mcp(server_configs)

        workflows = config.get("workflows", {})
        workflow_name = args.workflow or config.get("default_workflow")
        if not workflow_name and workflows:
            workflow_name = next(iter(workflows))
        if not workflow_name or workflow_name not in workflows:
            raise ValueError(f"Workflow '{workflow_name}' not found in {args.config}")

        workflow_config = dict(workflows[workflow_name])
        context = dict(workflow_config.get("context", {}))
        if args.topic:
            context["topic"] = args.topic
            for key, value in list(context.items()):
                if isinstance(value, str):
                    context[key] = value.replace("${TOPIC}", args.topic)
        workflow_config["context"] = context

        print(f"🔄 Executing workflow: {workflow_name}")
        result = await agent.execute_workflow(workflow_config)
        print(f"✅ Workflow '{workflow_name}' completed: success={result.get('success')}")
        return 0

    finally:
        await agent.cleanup_mcp()

def main() -> int:
    parser = argparse.ArgumentParser(description="Configurable MCP Agent Runner")
    parser.add_argument("--server", default="http://localhost:9000", help="Pixi Runner server URL")
    parser.add_argument("--task-id", required=True, help="Remote task ID to connect to")
    parser.add_argument("--aes-key", help="Path to AES key file (if encryption enabled)")
    parser.add_argument("--config", default="agent_config.yaml", help="Agent configuration YAML")
    parser.add_argument("--workflow", help="Workflow name from the config (defaults to first)")
    parser.add_argument("--topic", help="Topic context for the workflow")
    args = parser.parse_args()

    try:
        return asyncio.run(run(args))
    except Exception as e:
        print(f"❌ Error: {e}")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
