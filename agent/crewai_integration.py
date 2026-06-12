# crewai_integration.py - FIXED CrewAI integration with your MCP infrastructure
from crewai import Agent, Task, Crew, Process
from crewai.tools import BaseTool
from typing import Dict, List, Any, Optional, Type
from pydantic import BaseModel, Field
import asyncio
from dataclasses import dataclass

from ai_agent_framework import RemoteChannel
from mcp_manager import MCPManager, MCPServerConfig

class MCPToolInput(BaseModel):
    """Input schema for MCP tools"""
    query: str = Field(..., description="Input parameters as JSON or text")

class MCPTool(BaseTool):
    """FIXED: Bridge MCP tools to CrewAI tool interface"""
    
    name: str = Field(..., description="Tool name")
    description: str = Field(..., description="Tool description")
    args_schema: Type[BaseModel] = MCPToolInput
    
    def __init__(self, tool_name: str, mcp_manager: MCPManager, description: str = ""):
        # Store the MCP-specific attributes
        self._tool_name = tool_name
        self._mcp_manager = mcp_manager
        
        # Initialize BaseTool with required fields
        super().__init__(
            name=tool_name,
            description=description or f"MCP tool: {tool_name}",
            args_schema=MCPToolInput
        )
    
    def _run(self, query: str) -> str:
        """Execute MCP tool synchronously for CrewAI"""
        # CrewAI expects sync tools, so we run async in event loop
        try:
            # Try to get existing loop, create new one if needed
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If loop is running, we need to use run_in_executor
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(self._run_async_tool, query)
                        result = future.result(timeout=30)
                else:
                    result = loop.run_until_complete(self._run_async_tool(query))
            except RuntimeError:
                # No event loop, create a new one
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    result = loop.run_until_complete(self._run_async_tool(query))
                finally:
                    loop.close()
            
            return result
            
        except Exception as e:
            return f"Error executing {self._tool_name}: {str(e)}"
    
    def _run_async_tool(self, query: str) -> str:
        """Run the actual async MCP tool call"""
        return asyncio.create_task(self._execute_mcp_tool(query))
    
    async def _execute_mcp_tool(self, query: str) -> str:
        """Execute the MCP tool"""
        try:
            # Parse query as parameters
            params = self._parse_query_params(query)
            
            result = await self._mcp_manager.call_tool(self._tool_name, params)
            
            # Extract result content for CrewAI
            if isinstance(result, dict):
                if "result" in result:
                    return str(result["result"])
                elif "content" in result:
                    return str(result["content"])
                elif "success" in result and result["success"]:
                    return str(result)
                else:
                    return str(result)
            return str(result)
            
        except Exception as e:
            return f"MCP tool error: {str(e)}"
    
    def _parse_query_params(self, query: str) -> Dict[str, Any]:
        """Parse query string into tool parameters"""
        # Simple parameter extraction based on tool type
        params = {}
        
        if self._tool_name == "web_search":
            params["query"] = query
            params["num_results"] = 5
            
        elif self._tool_name == "write_file":
            # For write_file, expect format like "filename: content"
            if ":" in query:
                parts = query.split(":", 1)
                params["path"] = parts[0].strip()
                params["content"] = parts[1].strip()
            else:
                params["path"] = "output.txt"
                params["content"] = query
                
        elif self._tool_name == "read_file":
            params["path"] = query.strip()
            
        elif self._tool_name == "list_directory":
            params["path"] = query.strip() if query.strip() else "."
            
        else:
            # Generic parameters
            params["input"] = query
            
        return params

class MCPCrewAgent:
    """Enhanced CrewAI agent with MCP infrastructure access"""
    
    def __init__(self, channel: RemoteChannel, agent_config: Dict[str, Any]):
        self.channel = channel
        self.config = agent_config
        self.mcp_manager = MCPManager()
        self.tools = []
        
    async def initialize(self, server_configs: List[MCPServerConfig]):
        """Initialize MCP infrastructure"""
        await self.mcp_manager.start()
        
        # Start all MCP servers
        for config in server_configs:
            await self.mcp_manager.register_server(config)
            await self.mcp_manager.start_server(config.name)
        
        # Create CrewAI tools from MCP tools
        available_tools = await self.mcp_manager.get_available_tools()
        for server_name, tool_list in available_tools.items():
            for tool_name in tool_list:
                mcp_tool = MCPTool(
                    tool_name=tool_name,
                    mcp_manager=self.mcp_manager,
                    description=f"Tool from {server_name}: {tool_name}"
                )
                self.tools.append(mcp_tool)
        
        print(f"✅ Initialized {len(self.tools)} MCP tools for CrewAI")
    
    def create_crewai_agent(self) -> Agent:
        """Create CrewAI agent with MCP tools"""
        return Agent(
            role=self.config.get("role", "Research Assistant"),
            goal=self.config.get("goal", "Complete assigned tasks using available tools"),
            backstory=self.config.get("backstory", "I am an AI agent with access to secure MCP tools"),
            tools=self.tools,
            verbose=self.config.get("verbose", True),
            allow_delegation=self.config.get("allow_delegation", False)
        )

class MCPCrew:
    """CrewAI crew with MCP infrastructure integration"""
    
    def __init__(self, crew_config: Dict[str, Any], channel: RemoteChannel):
        self.config = crew_config
        self.channel = channel
        self.agents = []
        self.tasks = []
        
    async def setup_from_config(self):
        """Setup crew from configuration"""
        # Initialize MCP servers
        mcp_config = self.config.get("mcp", {})
        server_configs = []
        
        for server_def in mcp_config.get("servers", []):
            server_configs.append(MCPServerConfig.from_dict(server_def))
        
        # Create MCP-enhanced agents
        for agent_config in self.config.get("agents", []):
            mcp_agent = MCPCrewAgent(self.channel, agent_config)
            await mcp_agent.initialize(server_configs)
            
            crewai_agent = mcp_agent.create_crewai_agent()
            self.agents.append(crewai_agent)
        
        # Create tasks
        for task_config in self.config.get("tasks", []):
            task = Task(
                description=task_config["description"],
                agent=self.agents[task_config.get("agent_index", 0)],
                expected_output=task_config.get("expected_output", "Completed task")
            )
            self.tasks.append(task)
        
        print(f"✅ Setup crew with {len(self.agents)} agents and {len(self.tasks)} tasks")
    
    def create_crew(self) -> Crew:
        """Create CrewAI crew"""
        # FIXED: Handle CrewAI Process enum correctly
        process_type = self.config.get("process", "sequential").lower()
        
        if process_type == "sequential":
            process = Process.sequential
        elif process_type == "hierarchical":
            process = Process.hierarchical
        else:
            process = Process.sequential  # Default fallback
        
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=process,
            verbose=self.config.get("verbose", True)
        )

# Configuration-driven CrewAI execution
async def run_crewai_from_config(config_path: str, server_url: str, task_id: str, aes_key: bytes = None):
    """Run CrewAI crew from YAML configuration"""
    import yaml
    
    # Load configuration
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    # Setup channel
    from ai_agent_framework import read_pixi_config, create_auth_headers
    pixi_config = read_pixi_config()
    auth_headers = create_auth_headers(
        pixi_config.get("bearer_token"),
        pixi_config.get("snowflake_token")
    )
    
    channel = RemoteChannel(server_url, task_id, aes_key, auth_headers)
    
    # Setup and run crew
    mcp_crew = MCPCrew(config, channel)
    await mcp_crew.setup_from_config()
    
    crew = mcp_crew.create_crew()
    
    # Execute crew
    print("🚀 Starting CrewAI execution with MCP infrastructure...")
    result = crew.kickoff()
    
    print("✅ CrewAI execution completed")
    return result

# Example usage
if __name__ == "__main__":
    import sys
    import base64
    
    if len(sys.argv) < 4:
        print("Usage: python crewai_integration.py <config.yaml> <task_id> <aes_key_file>")
        sys.exit(1)
    
    config_path = sys.argv[1]
    task_id = sys.argv[2]
    aes_key_file = sys.argv[3]
    
    # Load AES key
    with open(aes_key_file, 'rb') as f:
        aes_key = base64.b64decode(f.read().strip())
    
    # Run CrewAI with MCP
    result = asyncio.run(run_crewai_from_config(
        config_path, 
        "http://localhost:9000", 
        task_id, 
        aes_key
    ))
    
    print("Final result:", result)
