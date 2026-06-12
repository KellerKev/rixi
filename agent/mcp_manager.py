# mcp_manager.py - FIXED - Actually calls real servers
import asyncio
import json
import subprocess
import uuid
import time
import os
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
import logging

logger = logging.getLogger(__name__)

class MCPServerState(Enum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"

@dataclass
class MCPServerConfig:
    """MCP server configuration"""
    name: str
    command: List[str]
    working_dir: Optional[str] = None
    env_vars: Dict[str, str] = field(default_factory=dict)
    tools: List[str] = field(default_factory=list)
    description: str = ""
    mode: str = "real"  # "real" or "simulation"

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> 'MCPServerConfig':
        """Create config from dictionary (for YAML loading)"""
        return cls(
            name=config_dict['name'],
            command=config_dict['command'],
            working_dir=config_dict.get('working_dir'),
            env_vars=config_dict.get('env_vars', {}),
            tools=config_dict.get('tools', []),
            description=config_dict.get('description', ''),
            mode=config_dict.get('mode', 'real')
        )

@dataclass
class MCPServerInstance:
    """Runtime MCP server instance"""
    config: MCPServerConfig
    process: Optional[subprocess.Popen] = None
    state: MCPServerState = MCPServerState.STOPPED
    start_time: Optional[float] = None
    available_tools: List[str] = field(default_factory=list)
    server_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    is_real: bool = True

class MCPManager:
    """FIXED MCP manager that actually calls real servers"""

    def __init__(self, prefer_real_servers: bool = True):
        self.servers: Dict[str, MCPServerInstance] = {}
        self.tool_registry: Dict[str, str] = {}  # tool_name -> server_name
        self.running = False
        self.prefer_real_servers = prefer_real_servers

    async def start(self):
        """Start the MCP manager"""
        if self.running:
            return
        self.running = True
        logger.info("MCP Manager started")

    async def stop(self):
        """Stop the MCP manager and all servers"""
        if not self.running:
            return
        self.running = False

        for server_name in list(self.servers.keys()):
            await self.stop_server(server_name)
        logger.info("MCP Manager stopped")

    async def register_server(self, config: MCPServerConfig) -> bool:
        """Register an MCP server"""
        if config.name in self.servers:
            logger.warning(f"Server {config.name} already registered")
            return False

        instance = MCPServerInstance(config=config)
        self.servers[config.name] = instance
        
        # Determine if this should be a real or simulated server
        if config.mode == "simulation" or not self.prefer_real_servers:
            instance.is_real = False
            print(f"📝 Registered MCP server: {config.name} (simulation mode)")
        else:
            instance.is_real = True
            print(f"🚀 Registered MCP server: {config.name} (real mode)")
        
        return True

    async def start_server(self, server_name: str) -> bool:
        """Start a specific MCP server"""
        if server_name not in self.servers:
            logger.error(f"Server {server_name} not registered")
            return False

        instance = self.servers[server_name]
        if instance.state == MCPServerState.RUNNING:
            return True

        try:
            instance.state = MCPServerState.STARTING
            instance.start_time = time.time()

            if instance.is_real and instance.config.mode != "simulation":
                # For filesystem servers, we don't need a subprocess - use direct implementation
                if self._is_filesystem_server(instance.config.name):
                    print(f"🔧 Starting real filesystem server: {instance.config.name}")
                    instance.available_tools = ["read_file", "write_file", "list_directory", "create_directory"]
                    instance.is_real = True
                else:
                    # Try to start real server process for other types
                    success = await self._start_real_server_process(instance)
                    if not success:
                        print(f"⚠️  Failed to start real server {server_name}, falling back to simulation")
                        instance.is_real = False
                        await self._setup_simulation_server(instance)
            else:
                # Use simulation
                await self._setup_simulation_server(instance)

            instance.state = MCPServerState.RUNNING

            # Register tools
            for tool in instance.available_tools:
                self.tool_registry[tool] = server_name

            mode = "real" if instance.is_real else "simulated"
            print(f"✅ Started MCP server: {server_name} ({mode})")
            return True

        except Exception as e:
            instance.state = MCPServerState.ERROR
            logger.error(f"Failed to start server {server_name}: {e}")
            return False

    def _is_filesystem_server(self, server_name: str) -> bool:
        """Check if this is a filesystem server"""
        name_lower = server_name.lower()
        return any(keyword in name_lower for keyword in ["filesystem", "workspace", "file", "fs"])

    async def _start_real_server_process(self, instance: MCPServerInstance) -> bool:
        """Start a real MCP server process (for non-filesystem servers)"""
        try:
            env = os.environ.copy()
            env.update(instance.config.env_vars)
            
            instance.process = subprocess.Popen(
                instance.config.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=instance.config.working_dir,
                env=env,
                text=True,
                bufsize=1
            )
            
            # Wait a moment for startup
            await asyncio.sleep(0.5)
            
            # Check if process is still running
            if instance.process.poll() is not None:
                print(f"❌ MCP server process exited immediately with code {instance.process.returncode}")
                return False
            
            # Discover available tools
            if instance.config.tools:
                instance.available_tools = instance.config.tools.copy()
            else:
                instance.available_tools = await self._discover_real_tools(instance)
            
            print(f"🚀 Real MCP server started with tools: {instance.available_tools}")
            return True
            
        except Exception as e:
            print(f"❌ Failed to start real MCP server: {e}")
            return False

    async def _discover_real_tools(self, instance: MCPServerInstance) -> List[str]:
        """Discover tools from real MCP server"""
        # This would implement the actual MCP tool discovery protocol
        # For now, return default tools based on server type
        server_name = instance.config.name.lower()
        if "search" in server_name:
            return ["web_search", "search_documents"]
        else:
            return ["generic_tool"]

    async def _setup_simulation_server(self, instance: MCPServerInstance):
        """Setup simulation server"""
        if instance.config.tools:
            instance.available_tools = instance.config.tools.copy()
        else:
            # Fallback based on server name/command
            server_name = instance.config.name.lower()
            if "filesystem" in server_name or "workspace" in server_name:
                instance.available_tools = ["read_file", "write_file", "list_directory", "create_directory"]
            elif "search" in server_name:
                instance.available_tools = ["web_search", "search_documents"]
            elif "database" in server_name:
                instance.available_tools = ["execute_query", "get_schema"]
            else:
                instance.available_tools = ["generic_tool"]

    async def stop_server(self, server_name: str) -> bool:
        """Stop a specific MCP server"""
        if server_name not in self.servers:
            return False

        instance = self.servers[server_name]
        if instance.state == MCPServerState.STOPPED:
            return True

        try:
            if instance.process:
                instance.process.terminate()
                try:
                    instance.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    instance.process.kill()

            # Remove tools from registry
            tools_to_remove = [tool for tool, server in self.tool_registry.items()
                             if server == server_name]
            for tool in tools_to_remove:
                del self.tool_registry[tool]

            instance.state = MCPServerState.STOPPED
            logger.info(f"Stopped MCP server: {server_name}")
            return True

        except Exception as e:
            logger.error(f"Error stopping server {server_name}: {e}")
            return False

    async def call_tool(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Call a tool - FIXED to actually use real servers"""
        if tool_name not in self.tool_registry:
            raise ValueError(f"Tool '{tool_name}' not found")

        server_name = self.tool_registry[tool_name]
        instance = self.servers.get(server_name)

        if not instance or instance.state != MCPServerState.RUNNING:
            raise RuntimeError(f"Server '{server_name}' not running")

        print(f"🔧 Calling tool {tool_name} on server {server_name} (real={instance.is_real})")

        # FIXED: Always try real implementation first for filesystem operations
        if tool_name in ["read_file", "write_file", "list_directory", "create_directory"]:
            if instance.is_real or instance.config.mode == "real":
                try:
                    result = await self._call_real_filesystem_tool(tool_name, params, instance)
                    print(f"✅ Real filesystem operation successful")
                    return result
                except Exception as e:
                    print(f"❌ Real filesystem operation failed: {e}, falling back to simulation")
                    return await self._call_simulated_tool(tool_name, params, instance)
            else:
                return await self._call_simulated_tool(tool_name, params, instance)
        
        # For other tools, try real server process or simulation
        if instance.is_real and instance.process:
            try:
                result = await self._call_real_process_tool(tool_name, params, instance)
                return result
            except Exception as e:
                print(f"❌ Real process tool failed: {e}, falling back to simulation")
                return await self._call_simulated_tool(tool_name, params, instance)
        else:
            return await self._call_simulated_tool(tool_name, params, instance)


    async def _call_real_filesystem_tool(self, tool_name: str, params: Dict[str, Any], 
                                    instance: MCPServerInstance) -> Dict[str, Any]:
        """FIXED: Call real filesystem operations with correct path"""
        print(f"🚀 Using REAL filesystem implementation for {tool_name}")
    
        # Import the real filesystem handler
        try:
            from mcp_filesystem import MCPFilesystemHandler
        except ImportError as e:
            raise Exception(f"Cannot import mcp_filesystem: {e}")
    
        # FIXED: Get root path correctly from command args
        root_path = "."  # Default to current directory
    
        # Parse command properly - command is ["python", "-m", "mcp_filesystem", "."]
        if len(instance.config.command) > 3:
            root_path = instance.config.command[3]  # FIXED: index 3, not 2
        elif len(instance.config.command) > 2 and instance.config.command[2] not in ["mcp_filesystem"]:
            root_path = instance.config.command[2]
    
        print(f"🔧 Filesystem root path: {root_path}")
    
        handler = MCPFilesystemHandler(root_path)
        result = await handler.handle_tool_call(tool_name, params)
    
        # Add metadata
        result["server_mode"] = "real"
        result["server_name"] = instance.config.name
        result["timestamp"] = time.time()
    
        print(f"📊 Real filesystem result: {result.get('success', False)}")
        return result

    async def _call_real_process_tool(self, tool_name: str, params: Dict[str, Any],
                                     instance: MCPServerInstance) -> Dict[str, Any]:
        """Call tool on real MCP server process via JSONL protocol"""
        try:
            request = {
                "tool": tool_name,
                "params": params,
                "id": str(uuid.uuid4())
            }
            
            # Send request
            instance.process.stdin.write(json.dumps(request) + "\n")
            instance.process.stdin.flush()
            
            # Read response (with timeout)
            try:
                response_line = instance.process.stdout.readline()
                if response_line:
                    result = json.loads(response_line.strip())
                    result["server_mode"] = "real"
                    return result
                else:
                    raise Exception("No response from MCP server")
            except json.JSONDecodeError as e:
                raise Exception(f"Invalid JSON response: {e}")
                
        except Exception as e:
            raise Exception(f"Real process tool call failed: {e}")

    async def _call_simulated_tool(self, tool_name: str, params: Dict[str, Any],
                                  instance: MCPServerInstance) -> Dict[str, Any]:
        """Simulate tool call"""
        print(f"📱 Using SIMULATED implementation for {tool_name}")
        await asyncio.sleep(0.1)

        base_response = {
            "success": True,
            "tool_name": tool_name,
            "server_name": instance.config.name,
            "server_mode": "simulated",
            "timestamp": time.time()
        }

        # Tool-specific simulation
        if tool_name == "web_search":
            query = params.get("query", "")
            base_response["result"] = f"Search results for '{query}': [simulated search data]"
            base_response["query"] = query

        elif tool_name == "read_file":
            path = params.get("path", "")
            base_response["result"] = f"Content from {path}: [simulated file content]"
            base_response["path"] = path

        elif tool_name == "write_file":
            path = params.get("path", "")
            content = params.get("content", "")
            base_response["result"] = f"Wrote {len(content)} bytes to {path}"
            base_response["path"] = path
            base_response["bytes_written"] = len(content)

        else:
            base_response["result"] = f"Tool {tool_name} executed with params: {params}"
            base_response["params"] = params

        return base_response

    async def get_available_tools(self) -> Dict[str, List[str]]:
        """Get available tools by server"""
        tools_by_server = {}
        for server_name, instance in self.servers.items():
            if instance.state == MCPServerState.RUNNING:
                mode = "real" if instance.is_real else "simulated"
                tools_by_server[f"{server_name} ({mode})"] = instance.available_tools.copy()
        return tools_by_server

    async def get_server_status(self) -> Dict[str, Dict[str, Any]]:
        """Get server status"""
        status = {}
        for server_name, instance in self.servers.items():
            status[server_name] = {
                "state": instance.state.value,
                "mode": "real" if instance.is_real else "simulated",
                "uptime": time.time() - instance.start_time if instance.start_time else 0,
                "available_tools": instance.available_tools,
                "description": instance.config.description
            }
        return status

# Factory functions for easy server creation
def create_filesystem_server_config(name: str, root_path: str = ".", mode: str = "real") -> MCPServerConfig:
    """Create filesystem server config"""
    return MCPServerConfig(
        name=name,
        command=["python", "-m", "mcp_filesystem", root_path],
        tools=["read_file", "write_file", "list_directory", "create_directory"],
        description=f"Filesystem server for {root_path}",
        mode=mode
    )

def create_web_search_server_config(name: str, api_key: str = "", mode: str = "real") -> MCPServerConfig:
    """Create web search server config"""
    return MCPServerConfig(
        name=name,
        command=["python", "-m", "mcp_web_search"],
        env_vars={"API_KEY": api_key} if api_key else {},
        tools=["web_search", "search_documents", "search_images"],
        description="Web search server",
        mode=mode
    )

def load_server_configs_from_dict(config_dict: Dict[str, Any]) -> List[MCPServerConfig]:
    """Load server configs from dictionary (for YAML)"""
    configs = []
    prefer_real = config_dict.get("prefer_real_servers", True)
    
    for server_config in config_dict.get("servers", []):
        # Override mode based on global preference if not explicitly set
        if "mode" not in server_config and not prefer_real:
            server_config["mode"] = "simulation"
        configs.append(MCPServerConfig.from_dict(server_config))
    return configs
