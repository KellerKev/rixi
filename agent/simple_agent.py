#!/usr/bin/env python3
"""
Simple Agent - Enhanced with Hybrid Architecture
=================================================

ALL ORIGINAL FUNCTIONALITY PRESERVED:
✅ python simple_agent.py --task-id ID --prompt "Hello"
✅ python simple_agent.py --task-id ID --config workflow.yaml  
✅ python simple_agent.py --task-id ID --interactive
✅ All existing features work exactly the same

NEW HYBRID FEATURES ADDED:
✅ python simple_agent.py --hybrid --workflow hybrid_workflow.yaml
✅ Local MCP servers + Remote compute services
✅ Mix local tools with remote API calls in same workflow

The hybrid mode is OPTIONAL - all your existing usage patterns continue to work!
"""

import asyncio
import argparse
import json
import uuid
import time
import yaml
import os
from pathlib import Path
from typing import Dict, Any, Optional, List, Union
import requests

from ai_agent_framework import RemoteChannel, read_pixi_config, create_auth_headers

# Import API tools if available
try:
    from api_client_tools import add_api_tools_to_agent
    API_TOOLS_AVAILABLE = True
except ImportError:
    API_TOOLS_AVAILABLE = False

# Import MCP components if available (for hybrid mode)
try:
    from mcp_manager import MCPManager, create_filesystem_server_config, create_web_search_server_config
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

class SimpleAgent:
    """Enhanced SimpleAgent with optional hybrid capabilities"""
    
    def __init__(self, channel: RemoteChannel = None, hybrid_mode: bool = False):
        self.channel = channel
        self.tools = {}
        self.hybrid_mode = hybrid_mode
        
        # Hybrid mode components (only if enabled)
        self.mcp_manager = None
        self.remote_services = {}
        
        if hybrid_mode and MCP_AVAILABLE:
            print("🔄 Initializing hybrid mode...")
            self.mcp_manager = MCPManager()

    async def setup(self):
        """Complete async setup (hybrid mode MCP servers)"""
        if self.hybrid_mode and self.mcp_manager:
            await self._setup_hybrid_mode()

    async def _setup_hybrid_mode(self):
        """Setup hybrid mode with local MCP servers"""
        if not self.mcp_manager:
            return
        
        await self.mcp_manager.start()
        
        # Add local filesystem server
        fs_config = create_filesystem_server_config("local_filesystem", ".")
        await self.mcp_manager.register_server(fs_config)
        await self.mcp_manager.start_server("local_filesystem")
        
        # Add local web search server  
        search_config = create_web_search_server_config("local_search")
        await self.mcp_manager.register_server(search_config)
        await self.mcp_manager.start_server("local_search")
        
        print("✅ Hybrid mode: Local MCP servers ready")
    
    def add_tool(self, name: str, func):
        """Add a simple tool function"""
        self.tools[name] = func
    
    def register_remote_service(self, name: str, base_url: str, auth_headers: Dict[str, str] = None):
        """Register remote compute service (hybrid mode only)"""
        if not self.hybrid_mode:
            print("⚠️  Remote services only available in hybrid mode")
            return
        
        self.remote_services[name] = {
            'base_url': base_url.rstrip('/'),
            'auth_headers': auth_headers or {},
            'session': requests.Session()
        }
        
        if auth_headers:
            self.remote_services[name]['session'].headers.update(auth_headers)
        
        print(f"🔗 Registered remote service: {name}")
    
    # ORIGINAL SIMPLE_AGENT FUNCTIONALITY (UNCHANGED)
    
    async def prompt(self, prompt_template: str, context: Dict[str, Any] = None, 
                    save_to: str = None, clean_output: bool = True) -> str:
        """Execute a simple prompt (ORIGINAL FUNCTIONALITY)"""
        
        if not self.channel:
            raise Exception("No channel available for prompt execution")
        
        # Substitute context variables
        if context:
            try:
                prompt = prompt_template.format(**context)
            except KeyError as e:
                print(f"⚠️  Missing context variable: {e}")
                prompt = prompt_template
        else:
            prompt = prompt_template
        
        print(f"📝 Executing prompt...")
        
        # Generate unique request ID
        request_id = str(uuid.uuid4())
        
        # Send to remote service
        self.channel.send({
            "command": "generate", 
            "prompt": prompt,
            "request_id": request_id
        })
        
        # Wait for response
        response = await self._wait_for_response(request_id)
        
        if not response:
            raise Exception("No response received")
        
        # Clean response if requested
        if clean_output:
            cleaned_response = self._auto_clean_response(response)
        else:
            cleaned_response = response
        
        # Save to file if requested
        if save_to:
            await self._save_result(cleaned_response, save_to)
            print(f"💾 Saved to: {save_to}")
        
        return cleaned_response
    
    async def chat(self, message: str) -> str:
        """Simple chat interface (ORIGINAL FUNCTIONALITY)"""
        return await self.prompt(message)
    
    async def use_tool(self, tool_name: str, **params) -> Any:
        """Use a tool (ENHANCED for hybrid mode)"""
        
        # Try local tools first (original behavior)
        if tool_name in self.tools:
            return await self.tools[tool_name](**params)
        
        # Try MCP tools in hybrid mode
        if self.hybrid_mode and self.mcp_manager and tool_name in self.mcp_manager.tool_registry:
            return await self.mcp_manager.call_tool(tool_name, params)
        
        # Try remote command (original behavior for non-hybrid)
        if self.channel:
            command = {"command": tool_name, "params": params}
            return await self.send_command(command)
        
        raise ValueError(f"Tool '{tool_name}' not available")
    
    async def send_command(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """Send structured command to remote service (ORIGINAL FUNCTIONALITY)"""
        if not self.channel:
            raise Exception("No channel available for command")
        
        request_id = str(uuid.uuid4())
        command["request_id"] = request_id
        
        print(f"📨 Sending command: {command.get('command', 'unknown')}")
        
        self.channel.send(command)
        response = await self._wait_for_response(request_id)
        
        try:
            return json.loads(response) if isinstance(response, str) else response
        except json.JSONDecodeError:
            return {"success": False, "error": "Invalid JSON response", "raw": response}
    
    # ENHANCED WORKFLOW FUNCTIONALITY
    
    async def workflow(self, steps: List[Dict[str, Any]], context: Dict[str, Any] = None, 
                      clean_output: bool = True) -> Dict[str, Any]:
        """Execute workflow (ENHANCED for hybrid mode)"""
        
        results = {}
        working_context = context.copy() if context else {}
        
        for i, step in enumerate(steps):
            step_name = step.get('name', f'step_{i}')
            step_type = step.get('type', 'prompt')
            
            print(f"🔄 Executing step: {step_name} ({step_type})")
            
            # ORIGINAL STEP TYPES (unchanged)
            if step_type == 'prompt':
                prompt_template = step['prompt']
                result = await self.prompt(prompt_template, working_context, clean_output=clean_output)
                results[step_name] = result
                
            elif step_type == 'tool':
                tool_name = step['tool']
                params = step.get('params', {})
                resolved_params = self._resolve_context(params, working_context)
                result = await self.use_tool(tool_name, **resolved_params)
                results[step_name] = result
                
            elif step_type == 'save':
                content = working_context.get(step['content'], step.get('content', ''))
                filename = step['filename']
                resolved_filename = filename.format(**working_context) if '{' in filename else filename
                await self._save_result(content, resolved_filename)
                results[step_name] = f"Saved to {resolved_filename}"
            
            # NEW HYBRID STEP TYPES (only in hybrid mode)
            elif step_type == 'mcp_tool' and self.hybrid_mode:
                if not self.mcp_manager:
                    raise Exception("MCP not available")
                
                tool_name = step['tool']
                params = self._resolve_context(step.get('params', {}), working_context)
                result = await self.mcp_manager.call_tool(tool_name, params)
                results[step_name] = result
                print(f"🔧 MCP tool {tool_name}: {result.get('success', False)}")
                
            elif step_type == 'remote_call' and self.hybrid_mode:
                service_name = step['service']
                endpoint = step['endpoint']
                method = step.get('method', 'POST')
                data = self._resolve_context(step.get('data', {}), working_context)
                
                if service_name not in self.remote_services:
                    raise Exception(f"Remote service {service_name} not registered")
                
                result = await self._call_remote_service(service_name, endpoint, method, data)
                results[step_name] = result
                
            elif step_type == 'conditional':
                condition = step.get('condition', 'true')
                if self._evaluate_condition(condition, working_context, results):
                    action = step.get('action', {})
                    action_result = await self._execute_single_step(action, working_context, results)
                    results[step_name] = action_result
                else:
                    results[step_name] = {"skipped": True, "reason": "condition not met"}
            
            else:
                if self.hybrid_mode:
                    raise Exception(f"Unknown step type: {step_type}")
                else:
                    print(f"⚠️  Step type '{step_type}' only available in hybrid mode")
                    results[step_name] = {"skipped": True, "reason": "hybrid mode required"}
            
            # Update context with result
            if 'save_as' in step:
                working_context[step['save_as']] = results[step_name]
        
        return {
            "success": True,
            "results": results,
            "final_context": working_context,
            "mode": "hybrid" if self.hybrid_mode else "simple"
        }
    
    # HYBRID MODE HELPERS
    
    async def _call_remote_service(self, service_name: str, endpoint: str, 
                                 method: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Call remote compute service"""
        service = self.remote_services[service_name]
        url = f"{service['base_url']}{endpoint}"
        
        try:
            print(f"🌐 Calling {service_name}{endpoint}")
            
            if method.upper() == "GET":
                response = service['session'].get(url, params=data)
            elif method.upper() == "POST":
                response = service['session'].post(url, json=data)
            elif method.upper() == "PUT":
                response = service['session'].put(url, json=data)
            else:
                raise ValueError(f"Unsupported method: {method}")
            
            response.raise_for_status()
            
            return {
                "success": True,
                "data": response.json() if response.headers.get('content-type', '').startswith('application/json') else {"text": response.text},
                "status_code": response.status_code,
                "service": service_name
            }
        
        except requests.RequestException as e:
            return {"success": False, "error": str(e), "service": service_name}
    
    async def _execute_single_step(self, step: Dict[str, Any], context: Dict[str, Any], results: Dict[str, Any]) -> Any:
        """Execute single step for conditional/parallel execution"""
        step_type = step.get("type", "unknown")
        
        if step_type == "remote_call" and self.hybrid_mode:
            service_name = step["service"]
            endpoint = step["endpoint"]
            method = step.get("method", "POST")
            data = self._resolve_context(step.get("data", {}), context)
            return await self._call_remote_service(service_name, endpoint, method, data)
        
        elif step_type == "mcp_tool" and self.hybrid_mode:
            tool_name = step["tool"]
            params = self._resolve_context(step.get("params", {}), context)
            return await self.mcp_manager.call_tool(tool_name, params)
        
        elif step_type == "tool":
            tool_name = step["tool"]
            params = self._resolve_context(step.get("params", {}), context)
            return await self.use_tool(tool_name, **params)
        
        else:
            return {"error": f"Unsupported step type: {step_type}"}
    
    # ORIGINAL HELPER METHODS (unchanged)
    
    async def _wait_for_response(self, request_id: str, timeout: int = 30) -> Optional[str]:
        """Wait for response with matching request ID (ORIGINAL)"""
        response_data = None
        
        def callback(data):
            nonlocal response_data
            if "output" in data:
                try:
                    output_obj = json.loads(data["output"])
                    if output_obj.get("request_id") == request_id:
                        response_data = output_obj.get("response")
                        return True
                except json.JSONDecodeError:
                    pass
            return False
        
        try:
            self.channel.receive(callback, timeout=timeout)
        except:
            pass
        
        if not response_data:
            start_time = time.time()
            while not response_data and (time.time() - start_time) < timeout:
                try:
                    status = self.channel.query_task_status()
                    for output in status.get("recent_output", []):
                        if output["type"] == "output":
                            try:
                                output_obj = json.loads(output["content"])
                                if output_obj.get("request_id") == request_id:
                                    response_data = output_obj.get("response")
                                    break
                            except (json.JSONDecodeError, KeyError):
                                pass
                    if response_data:
                        break
                    await asyncio.sleep(1)
                except:
                    break
        
        return response_data
    
    def _resolve_context(self, params: Dict[str, Any], context: Dict[str, Any], results: Dict[str, Any] = None) -> Dict[str, Any]:
        """Resolve context variables (ENHANCED)"""
        if isinstance(params, dict):
            resolved = {}
            combined_context = {**context, **(results or {})}
            
            for key, value in params.items():
                if isinstance(value, str) and '{' in value:
                    try:
                        resolved[key] = value.format(**combined_context)
                    except KeyError as e:
                        print(f"⚠️  Missing context variable in {key}: {e}")
                        resolved[key] = value
                elif isinstance(value, dict):
                    resolved[key] = self._resolve_context(value, context, results)
                elif isinstance(value, list):
                    resolved[key] = [
                        item.format(**combined_context) if isinstance(item, str) and '{' in item else item
                        for item in value
                    ]
                else:
                    resolved[key] = value
            return resolved
        else:
            return params
    
    def _evaluate_condition(self, condition: str, context: Dict[str, Any], results: Dict[str, Any]) -> bool:
        """Evaluate simple conditions (ORIGINAL)"""
        combined_context = {**context, **results}
        for key, value in combined_context.items():
            condition = condition.replace(f'{{{key}}}', str(value))
        
        if condition == 'true':
            return True
        elif condition == 'false':
            return False
        elif ' == ' in condition:
            left, right = condition.split(' == ')
            return left.strip().strip('"\'') == right.strip().strip('"\'')
        elif ' != ' in condition:
            left, right = condition.split(' != ')
            return left.strip().strip('"\'') != right.strip().strip('"\'')
        else:
            return True
    
    def _auto_clean_response(self, response: str) -> str:
        """Clean verbose responses (ORIGINAL)"""
        if not response or not isinstance(response, str):
            return str(response) if response else ""
        
        # Try to extract JSON content first
        try:
            json_response = json.loads(response)
            if isinstance(json_response, dict):
                for field in ['response', 'result', 'content', 'text', 'output']:
                    if field in json_response:
                        response = str(json_response[field])
                        break
        except json.JSONDecodeError:
            pass
        
        # Basic text cleaning
        lines = response.strip().split('\n')
        cleaned_lines = []
        
        skip_patterns = [
            'example:', 'here is', 'here\'s', 'output:', 'result:',
            'response:', 'answer:', 'explanation:', 'note:'
        ]
        
        for line in lines:
            line = line.strip()
            if not line or line in ['---', '===', '***']:
                continue
            
            should_skip = any(line.lower().startswith(pattern.lower()) for pattern in skip_patterns)
            if should_skip:
                continue
            
            line = line.strip('*-_=').strip()
            if len(line) >= 3:
                cleaned_lines.append(line)
        
        return '\n'.join(cleaned_lines) if cleaned_lines else response.strip()
    
    async def _save_result(self, content: str, filename: str):
        """Save result to file (ENHANCED for hybrid)"""
        try:
            # Try MCP write_file in hybrid mode
            if self.hybrid_mode and self.mcp_manager:
                try:
                    await self.mcp_manager.call_tool('write_file', {'path': filename, 'content': content})
                    return
                except Exception as e:
                    print(f"⚠️  MCP save failed: {e}, using local fallback")
            
            # Try local tools
            if 'write_file' in self.tools:
                await self.tools['write_file'](path=filename, content=content)
            else:
                Path(filename).write_text(content)
                
        except Exception as e:
            print(f"⚠️  Save failed, using local fallback: {e}")
            Path(filename).write_text(content)

# Built-in tools (ORIGINAL - unchanged)
async def simple_write_file(path: str, content: str) -> Dict[str, Any]:
    """Simple file writing tool"""
    try:
        Path(path).write_text(content)
        return {"success": True, "message": f"Wrote to {path}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

async def simple_read_file(path: str) -> Dict[str, Any]:
    """Simple file reading tool"""
    try:
        content = Path(path).read_text()
        return {"success": True, "content": content}
    except Exception as e:
        return {"success": False, "error": str(e)}

def create_simple_agent(server_url: str, task_id: str, aes_key: bytes = None, 
                       enable_api_tools: bool = False, hybrid_mode: bool = False) -> SimpleAgent:
    """Create agent (ENHANCED with hybrid option)"""
    
    channel = None
    if server_url and task_id:
        config = read_pixi_config()
        bearer_token = config.get("bearer_token") or config.get("bearer-token")
        snowflake_token = config.get("snowflake_token") or config.get("snowflake-token")
        auth_headers = create_auth_headers(bearer_token, snowflake_token)
        
        channel = RemoteChannel(server_url, task_id, aes_key, auth_headers)
    
    agent = SimpleAgent(channel, hybrid_mode)
    
    # Add built-in tools
    agent.add_tool('write_file', simple_write_file)
    agent.add_tool('read_file', simple_read_file)
    
    # Add API tools if requested
    if enable_api_tools and API_TOOLS_AVAILABLE:
        add_api_tools_to_agent(agent)
    
    return agent

def load_config(config_path: str) -> Dict[str, Any]:
    """Load configuration file (ORIGINAL)"""
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

async def interactive_mode(agent: SimpleAgent):
    """Interactive mode (ENHANCED for hybrid)"""
    mode_info = "hybrid mode" if agent.hybrid_mode else "simple mode"
    print(f"🎮 Interactive mode started ({mode_info}). Type 'quit' to exit.")
    
    if agent.hybrid_mode:
        print("💡 Hybrid commands: /mcp <tool> <params>, /remote <service> <endpoint>")
    
    while True:
        try:
            user_input = input("\n> ").strip()
            
            if user_input.lower() in ['quit', 'exit', 'q']:
                break
            
            elif user_input.startswith('/save '):
                filename = user_input[6:].strip()
                print(f"Next response will be saved to: {filename}")
                user_input = input("Prompt> ")
                response = await agent.chat(user_input)
                print(f"\n🤖 {response}")
                await agent._save_result(response, filename)
                print(f"💾 Saved to: {filename}")
            
            elif user_input.startswith('/mcp ') and agent.hybrid_mode:
                # MCP tool usage: /mcp write_file path=test.txt content="hello"
                parts = user_input[5:].split()
                if len(parts) >= 1:
                    tool_name = parts[0]
                    params = {}
                    for part in parts[1:]:
                        if '=' in part:
                            key, value = part.split('=', 1)
                            params[key] = value.strip('"\'')
                    
                    result = await agent.mcp_manager.call_tool(tool_name, params)
                    print(f"🔧 MCP result: {result}")
                else:
                    print("Usage: /mcp <tool> key=value...")
            
            elif user_input.startswith('/remote ') and agent.hybrid_mode:
                # Remote service call: /remote training /status
                parts = user_input[8:].split()
                if len(parts) >= 2:
                    service_name, endpoint = parts[0], parts[1]
                    result = await agent._call_remote_service(service_name, endpoint, "GET", {})
                    print(f"🌐 Remote result: {result}")
                else:
                    print("Usage: /remote <service> <endpoint>")
            
            else:
                response = await agent.chat(user_input)
                print(f"\n🤖 {response}")
                
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"❌ Error: {e}")
    
    print("\n👋 Goodbye!")

async def main():
    """Main CLI interface (ENHANCED but backward compatible)"""
    parser = argparse.ArgumentParser(description="Simple AI Agent - Enhanced with Hybrid Architecture")
    
    # Connection parameters
    parser.add_argument("--server", default="http://localhost:9000")
    parser.add_argument("--task-id", help="Remote task ID")
    parser.add_argument("--aes-key", help="AES key file path")
    
    # NEW: Hybrid mode
    parser.add_argument("--hybrid", action="store_true", help="Enable hybrid mode (local MCP + remote compute)")
    parser.add_argument("--register-remote", action="append", help="Register remote service: name,url,auth_header")
    
    # ORIGINAL: Simple execution modes (unchanged)
    parser.add_argument("--prompt", help="Simple prompt to execute")
    parser.add_argument("--config", help="YAML configuration file")
    parser.add_argument("--interactive", action="store_true", help="Interactive mode")
    
    # ORIGINAL: Context and options (unchanged)
    parser.add_argument("--set", action="append", help="Set context variables as key=value")
    parser.add_argument("--save-to", help="File to save result")
    parser.add_argument("--no-clean", action="store_true", help="Disable response cleaning")
    parser.add_argument("--enable-api-tools", action="store_true", help="Enable API tools")
    
    args = parser.parse_args()
    
    # Hybrid mode validation
    if args.hybrid and not MCP_AVAILABLE:
        print("❌ Hybrid mode requires MCP components. Install with missing dependencies.")
        return 1
    
    # Load AES key if specified
    aes_key = None
    if args.aes_key:
        import base64
        raw = Path(args.aes_key).read_bytes().strip()
        try:
            aes_key = base64.b64decode(raw)
            if len(aes_key) != 32:
                raise ValueError("Invalid key length")
            print("🔒 AES encryption enabled")
        except:
            print("❌ Invalid AES key")
            return 1
    
    # Create agent
    agent = create_simple_agent(
        args.server, 
        args.task_id, 
        aes_key, 
        args.enable_api_tools,
        args.hybrid
    )
    
    # Register remote services in hybrid mode
    if args.hybrid and args.register_remote:
        for remote_spec in args.register_remote:
            parts = remote_spec.split(',')
            if len(parts) >= 2:
                name, url = parts[0], parts[1]
                auth_header = parts[2] if len(parts) > 2 else None
                headers = {"Authorization": auth_header} if auth_header else {}
                agent.register_remote_service(name, url, headers)
    
    # Complete hybrid setup
    if args.hybrid:
        await agent.setup()

    try:
        # Build context from arguments (ORIGINAL)
        context = {}
        if args.set:
            for item in args.set:
                if '=' in item:
                    key, value = item.split('=', 1)
                    context[key] = value
        
        clean_output = not args.no_clean
        
        # Execute based on mode (ORIGINAL behavior preserved)
        if args.interactive:
            await interactive_mode(agent)
        
        elif args.prompt:
            if not args.task_id and not args.hybrid:
                print("❌ Must specify --task-id for prompt execution (or use --hybrid for local-only)")
                return 1
            
            result = await agent.prompt(args.prompt, context, args.save_to, clean_output)
            print(f"\n📄 Result:")
            print("=" * 50)
            print(result)
            print("=" * 50)
        
        elif args.config:
            config = load_config(args.config)
            
            if 'workflow' in config:
                result = await agent.workflow(config['workflow'], context, clean_output)
                print(f"\n📊 Workflow Results ({result.get('mode', 'simple')}):")
                print(f"   Success: {result['success']}")
                for step, step_result in result['results'].items():
                    print(f"   {step}: {str(step_result)[:100]}...")
            
            elif 'prompt' in config:
                if not args.task_id and not args.hybrid:
                    print("❌ Must specify --task-id for prompt execution (or use --hybrid for local-only)")
                    return 1
                
                save_to = config.get('save_to')
                if save_to and context:
                    try:
                        save_to = save_to.format(**context)
                    except KeyError as e:
                        print(f"⚠️  Missing context variable in save_to: {e}")
                
                result = await agent.prompt(config['prompt'], context, save_to, clean_output)
                print(f"\n📄 Result:\n{result}")
            
            else:
                print("❌ Invalid config format - need 'workflow' or 'prompt'")
                return 1
        
        else:
            print("❌ Must specify --prompt, --config, or --interactive")
            return 1
        
        return 0
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return 1
    
    finally:
        # Cleanup hybrid mode
        if agent.hybrid_mode and agent.mcp_manager:
            await agent.mcp_manager.stop()

if __name__ == "__main__":
    exit(asyncio.run(main()))
