# start_agent.py - Pure generic configuration-driven agent runner
import base64
import argparse
import asyncio
import yaml
import json
from pathlib import Path
from typing import Dict, Any, Optional

from ai_agent_framework import read_pixi_config, create_auth_headers, RemoteChannel

# Import clean MCP components
try:
    from mcp_agent import ConfigurableMCPAgent, create_agent_from_config
    from mcp_manager import load_server_configs_from_dict
    MCP_AVAILABLE = True
    print("✅ MCP features available")
except ImportError as e:
    MCP_AVAILABLE = False
    print(f"⚠️  MCP features not available: {e}")

class GenericAgentRunner:
    """
    Pure generic agent runner - no hardcoded content types or behaviors.
    Everything comes from configuration files.
    """
    
    def __init__(self, server_url: str, task_id: str, aes_key: bytes = None):
        self.server_url = server_url
        self.task_id = task_id
        self.aes_key = aes_key
        self.channel = None
        
    def setup_channel(self):
        """Setup encrypted channel to remote infrastructure"""
        config = read_pixi_config()
        bearer_token = config.get("bearer_token") or config.get("bearer-token")
        snowflake_token = config.get("snowflake_token") or config.get("snowflake-token")
        auth_headers = create_auth_headers(bearer_token, snowflake_token)
        
        self.channel = RemoteChannel(self.server_url, self.task_id, self.aes_key, auth_headers)
        print(f"✅ Connected to: {self.server_url}")
    
    def load_config(self, config_path: str) -> Dict[str, Any]:
        """Load configuration from YAML file"""
        try:
            config_file = Path(config_path)
            if not config_file.exists():
                raise FileNotFoundError(f"Config file not found: {config_path}")
            
            with open(config_file, 'r') as f:
                # Handle multiple YAML documents
                configs = list(yaml.safe_load_all(f))
                if len(configs) == 1:
                    return configs[0]
                else:
                    # For multi-document YAML, return the one that matches context
                    return {"configs": configs}
                    
        except Exception as e:
            print(f"❌ Error loading config: {e}")
            raise
    
    def resolve_context_variables(self, config: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve ${VARIABLE} references in configuration"""
        config_str = json.dumps(config)
        
        # Replace context variables
        for key, value in context.items():
            placeholder = f"${{{key}}}"
            config_str = config_str.replace(placeholder, str(value))
            
            # Also support ${key} format
            placeholder = f"${key}"
            config_str = config_str.replace(placeholder, str(value))
        
        return json.loads(config_str)
    
    async def run_with_config(self, config_path: str, context: Dict[str, Any] = None) -> Any:
        """
        Run agent using configuration file - completely generic.
        """
        if not MCP_AVAILABLE:
            return await self._run_fallback_mode(config_path, context)
        
        print(f"🔧 Loading configuration: {config_path}")
        raw_config = self.load_config(config_path)
        
        # Handle multi-document YAML
        if "configs" in raw_config:
            # Use first config as main, others as available
            main_config = raw_config["configs"][0]
            
            # If context specifies which config to use
            if context and "config_index" in context:
                config_index = context["config_index"]
                if config_index < len(raw_config["configs"]):
                    main_config = raw_config["configs"][config_index]
        else:
            main_config = raw_config
        
        # Resolve context variables in config
        if context:
            main_config = self.resolve_context_variables(main_config, context)
        
        # Setup channel
        self.setup_channel()
        
        # Create agent from configuration
        agent = create_agent_from_config(self.channel, main_config)
        
        try:
            # Initialize MCP if configured
            mcp_config = main_config.get("mcp", {})
            if mcp_config:
                server_configs = load_server_configs_from_dict(mcp_config)
                await agent.initialize_mcp(server_configs)
                print(f"🛠️  Initialized {len(server_configs)} MCP servers")
                
                # Show available tools
                tools = await agent.get_available_tools()
                if tools:
                    print(f"🔧 Available tools: {list(tools.keys())}")
            
            # Determine execution mode from config
            execution_mode = main_config.get("execution_mode", "workflow")
            
            if execution_mode == "workflow":
                result = await self._execute_workflow_mode(agent, main_config, context)
            elif execution_mode == "generation":
                result = await self._execute_generation_mode(agent, main_config, context)
            elif execution_mode == "interactive":
                result = await self._execute_interactive_mode(agent, main_config, context)
            else:
                raise ValueError(f"Unknown execution mode: {execution_mode}")
            
            return result
            
        finally:
            await agent.cleanup_mcp()
    
    async def _execute_workflow_mode(self, agent, config: Dict[str, Any], context: Dict[str, Any]) -> Any:
        """Execute in workflow mode"""
        workflows = config.get("workflows", {})
        
        # Determine which workflow to run
        if context and "workflow" in context:
            workflow_name = context["workflow"]
        else:
            workflow_name = config.get("default_workflow")
            if not workflow_name:
                workflow_name = list(workflows.keys())[0] if workflows else None
        
        if not workflow_name or workflow_name not in workflows:
            raise ValueError(f"Workflow '{workflow_name}' not found")
        
        print(f"🔄 Executing workflow: {workflow_name}")
        
        workflow_config = workflows[workflow_name].copy()
        workflow_config["context"] = context or {}
        
        result = await agent.execute_workflow(workflow_config)
        
        # Display result based on config
        self._display_result(result, config.get("output_format", {}))
        
        return result
    
    async def _execute_generation_mode(self, agent, config: Dict[str, Any], context: Dict[str, Any]) -> Any:
        """Execute in direct generation mode"""
        generation_config = config.get("generation", {})
        
        # Determine which generation config to use
        if context and "generation_type" in context:
            gen_type = context["generation_type"]
            if gen_type in generation_config:
                gen_config = generation_config[gen_type]
            else:
                gen_config = generation_config.get("default", {})
        else:
            gen_config = generation_config.get("default", {})
        
        print(f"📝 Direct generation mode")
        
        result = await agent.generate_with_config(context or {}, gen_config)
        
        # Save if configured
        if context and "output_file" in context:
            await self._save_result(agent, result, context["output_file"])
        
        # Display result
        self._display_result({"result": result}, config.get("output_format", {}))
        
        return result
    
    async def _execute_interactive_mode(self, agent, config: Dict[str, Any], context: Dict[str, Any]) -> Any:
        """Execute in interactive mode"""
        print("🎮 Interactive mode - not implemented yet")
        return {"mode": "interactive", "status": "not_implemented"}
    
    async def _run_fallback_mode(self, config_path: str, context: Dict[str, Any]) -> Any:
        """Fallback mode when MCP is not available"""
        print("⚠️  MCP not available, checking for fallback configuration...")
        
        config = self.load_config(config_path)
        fallback_config = config.get("fallback", {})
        
        if not fallback_config:
            raise RuntimeError("MCP required but not available, and no fallback configured")
        
        # Execute fallback behavior
        fallback_type = fallback_config.get("type", "original_agent")
        
        if fallback_type == "original_agent":
            return await self._execute_original_agent_fallback(fallback_config, context)
        else:
            raise ValueError(f"Unknown fallback type: {fallback_type}")
    
    async def _execute_original_agent_fallback(self, fallback_config: Dict[str, Any], context: Dict[str, Any]) -> Any:
        """Execute using original agents as fallback"""
        self.setup_channel()
        
        agent_type = fallback_config.get("agent_type")
        
        if agent_type == "haiku":
            # Import only when needed for fallback
            from ai_agent_framework import HaikuAgent
            
            topic = context.get("topic", "nature")
            output_file = context.get("output_file", "output.txt")
            
            agent = HaikuAgent(self.channel)
            result = agent.generate_haiku(topic, output_file)
            
            print(f"\n📄 Generated content:")
            print("-" * 40)
            print(result)
            print("-" * 40)
            
            return result
        else:
            raise ValueError(f"Unknown fallback agent type: {agent_type}")
    
    def _display_result(self, result: Any, output_format: Dict[str, Any]):
        """Display result based on output format configuration"""
        display_type = output_format.get("type", "simple")
        
        if display_type == "simple":
            if isinstance(result, dict) and "result" in result:
                content = result["result"]
            elif isinstance(result, dict) and "results" in result:
                # Extract from workflow results
                workflow_results = result["results"]
                # Get the last step's result
                last_step = list(workflow_results.keys())[-1] if workflow_results else None
                if last_step:
                    step_result = workflow_results[last_step]
                    content = step_result.get("result", str(step_result))
                else:
                    content = str(result)
            else:
                content = str(result)
            
            print(f"\n📄 Generated content:")
            print("=" * 50)
            print(content)
            print("=" * 50)
            
            if isinstance(content, str):
                print(f"📏 Length: {len(content)} characters")
                print(f"📐 Lines: {len(content.split(chr(10)))}")
        
        elif display_type == "detailed":
            print(f"\n📊 Detailed Results:")
            print("=" * 60)
            print(json.dumps(result, indent=2))
            print("=" * 60)
        
        elif display_type == "summary":
            print(f"\n📋 Summary:")
            if isinstance(result, dict):
                if "workflow" in result:
                    print(f"   Workflow: {result.get('workflow', 'unknown')}")
                if "results" in result:
                    print(f"   Steps completed: {len(result['results'])}")
                if "success" in result:
                    print(f"   Success: {result['success']}")
            print("   ✅ Execution completed")
    
    async def _save_result(self, agent, result: Any, output_file: str):
        """Save result using available tools"""
        content = str(result) if not isinstance(result, str) else result
        
        if await agent.has_tool("write_file"):
            try:
                await agent.use_tool("write_file", path=output_file, content=content)
                print(f"💾 Saved to {output_file} using MCP")
            except Exception as e:
                print(f"⚠️  MCP save failed: {e}, using local save")
                self._save_local(content, output_file)
        else:
            self._save_local(content, output_file)
    
    def _save_local(self, content: str, output_file: str):
        """Save content locally"""
        try:
            with open(output_file, 'w') as f:
                f.write(content)
            print(f"💾 Saved to {output_file} locally")
        except Exception as e:
            print(f"⚠️  Local save failed: {e}")

def load_aes_key(key_path: str) -> bytes:
    """Load AES key from file"""
    if not key_path:
        return None
        
    with open(key_path, "rb") as f:
        raw = f.read().strip()
        try:
            aes_key = base64.b64decode(raw)
            if len(aes_key) != 32:
                raise ValueError("Invalid AES key length")
            return aes_key
        except Exception as e:
            raise ValueError(f"Error decoding AES key: {e}")

async def main():
    parser = argparse.ArgumentParser(description="Generic Configuration-Driven Agent Runner")
    parser.add_argument("--server", default="http://localhost:9000", help="Server URL")
    parser.add_argument("--task-id", required=True, help="Task ID")
    parser.add_argument("--aes-key", help="AES key file path")
    
    # Core parameters - completely generic
    parser.add_argument("--config", required=True, help="Configuration file path")
    parser.add_argument("--workflow", help="Specific workflow to execute")
    parser.add_argument("--generation-type", help="Generation type from config")
    parser.add_argument("--context", help="Additional context as JSON string")
    
    # Common context parameters
    parser.add_argument("--topic", help="Content topic")
    parser.add_argument("--output-file", help="Output file path")
    
    args = parser.parse_args()
    
    # Load AES key
    aes_key = load_aes_key(args.aes_key) if args.aes_key else None
    
    # Build context from arguments
    context = {}
    if args.topic:
        context["topic"] = args.topic
    if args.output_file:
        context["output_file"] = args.output_file
    if args.workflow:
        context["workflow"] = args.workflow
    if args.generation_type:
        context["generation_type"] = args.generation_type
    
    # Add additional context from JSON
    if args.context:
        try:
            additional_context = json.loads(args.context)
            context.update(additional_context)
        except json.JSONDecodeError as e:
            print(f"❌ Invalid JSON in --context: {e}")
            return 1
    
    # Create runner and execute
    runner = GenericAgentRunner(args.server, args.task_id, aes_key)
    
    try:
        result = await runner.run_with_config(args.config, context)
        print(f"✅ Execution completed successfully")
        return 0
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return 1

if __name__ == "__main__":
    exit(asyncio.run(main()))
