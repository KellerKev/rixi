# mcp_agent.py - COMPLETE FINAL VERSION with all fixes integrated
import asyncio
import json
import uuid
import time
import re
from typing import Dict, List, Any, Optional

from ai_agent_framework import Agent
from mcp_manager import MCPManager, MCPServerConfig

class ConfigurableMCPAgent(Agent):
    """
    Generic MCP-enhanced agent driven by configuration.
    No hardcoded content-type assumptions.
    FINAL VERSION with enhanced debugging and clean haiku processing.
    """

    def __init__(self, channel, config: Dict[str, Any] = None):
        super().__init__(channel)
        self.config = config or {}
        self.mcp_manager = MCPManager()
        self._mcp_started = False

    async def initialize_mcp(self, server_configs: List[MCPServerConfig] = None):
        """Initialize MCP servers"""
        if self._mcp_started:
            return

        await self.mcp_manager.start()

        if server_configs:
            for config in server_configs:
                print(f"🔧 Registering server: {config.name} (mode: {config.mode})")
                await self.mcp_manager.register_server(config)
                success = await self.mcp_manager.start_server(config.name)
                print(f"✅ Server {config.name} started: {success}")

        self._mcp_started = True
        print(f"✅ MCP initialized with {len(server_configs or [])} servers")
        
        # Debug: Show server status
        status = await self.mcp_manager.get_server_status()
        for server_name, server_info in status.items():
            print(f"🔍 Server {server_name}: {server_info['state']} ({server_info['mode']})")

    async def cleanup_mcp(self):
        """Clean up MCP resources"""
        if self._mcp_started:
            await self.mcp_manager.stop()
            self._mcp_started = False

    async def use_tool(self, tool_name: str, **params) -> Dict[str, Any]:
        """Use an MCP tool - pure interface"""
        if not self._mcp_started:
            raise RuntimeError("MCP not initialized")
        
        print(f"🔧 Calling tool: {tool_name} with params: {params}")
        result = await self.mcp_manager.call_tool(tool_name, params)
        print(f"📊 Tool result: {result.get('server_mode', 'unknown')} mode")
        return result

    async def has_tool(self, tool_name: str) -> bool:
        """Check if tool is available"""
        if not self._mcp_started:
            return False
        return tool_name in self.mcp_manager.tool_registry

    async def get_available_tools(self) -> Dict[str, List[str]]:
        """Get available tools"""
        if not self._mcp_started:
            return {}
        return await self.mcp_manager.get_available_tools()

    # Core configurable generation
    async def generate_with_config(self, context: Dict[str, Any],
                                 generation_config: Dict[str, Any]) -> str:
        """
        Generate content based on configuration.
        No hardcoded assumptions about content type.

        Args:
            context: Generation context (topic, research data, etc.)
            generation_config: Configuration for this generation
        """
        # Build prompt from configuration
        prompt = self._build_prompt_from_config(context, generation_config)

        # Generate using existing infrastructure
        request_id = str(uuid.uuid4())

        self.send_to_remote({
            "command": "generate",
            "prompt": prompt,
            "request_id": request_id
        })

        # Receive response
        response_data = None

        def response_callback(data):
            nonlocal response_data
            if "output" in data:
                try:
                    output_obj = json.loads(data["output"])
                    if "request_id" in output_obj and output_obj["request_id"] == request_id:
                        response_data = output_obj["response"]
                        return True
                except json.JSONDecodeError:
                    pass
            return False

        self.receive_from_remote(response_callback)

        if response_data:
            # Apply post-processing from configuration
            processed_response = self._process_response_from_config(response_data, generation_config)
            return processed_response
        else:
            raise Exception("Failed to receive response")

    async def execute_workflow(self, workflow_config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a workflow defined in configuration.
        ENHANCED with detailed debugging for context resolution.
        """
        results = {}
        context = workflow_config.get("context", {})
        
        print(f"🔄 Starting workflow with initial context: {context}")

        for step_config in workflow_config.get("steps", []):
            step_name = step_config["name"]
            step_type = step_config["type"]

            print(f"\n🔄 Executing step: {step_name} ({step_type})")
            print(f"📊 Current context keys: {list(context.keys())}")
            print(f"📊 Current results keys: {list(results.keys())}")

            if step_type == "tool_call":
                # Execute tool call
                tool_name = step_config["tool"]
                tool_params = step_config.get("params", {})

                # Support dynamic parameters from context
                resolved_params = self._resolve_params_from_context(tool_params, context, results)
                print(f"🔧 Resolved params for {tool_name}: {resolved_params}")

                result = await self.use_tool(tool_name, **resolved_params)
                results[step_name] = result
                print(f"🔧 Tool result: {result}")

                # FIXED: Update context if specified
                if "update_context" in step_config:
                    context_key = step_config["update_context"]
                    
                    # Extract the actual result content
                    if isinstance(result, dict):
                        if "result" in result:
                            context_value = result["result"]
                        else:
                            # For simulation, use the result directly
                            context_value = result
                    else:
                        context_value = str(result)
                    
                    context[context_key] = context_value
                    print(f"🔧 Updated context['{context_key}'] = {str(context_value)[:100]}...")

            elif step_type == "generate":
                # Generate content
                generation_config = step_config.get("generation", {})

                # FIXED: Build step context properly
                step_context = context.copy()
                
                # Add any additional context from the step config
                step_specific_context = step_config.get("context", {})
                print(f"🔧 Step-specific context to resolve: {step_specific_context}")
                
                # Resolve step-specific context variables
                for key, value in step_specific_context.items():
                    if isinstance(value, str) and value.startswith("${"):
                        var_name = value[2:-1]
                        if var_name in context:
                            step_context[key] = context[var_name]
                            print(f"✅ Resolved step context {key} = {var_name} -> {str(step_context[key])[:50]}...")
                        elif var_name in results:
                            result_value = results[var_name]
                            if isinstance(result_value, dict) and "result" in result_value:
                                step_context[key] = result_value["result"]
                            else:
                                step_context[key] = str(result_value)
                            print(f"✅ Resolved step context {key} from results -> {str(step_context[key])[:50]}...")
                        else:
                            print(f"⚠️  Could not resolve step context variable: {var_name}")
                            step_context[key] = f"[Missing: {var_name}]"
                    else:
                        step_context[key] = value
                
                print(f"📊 Final step context for generation: {list(step_context.keys())}")
                for key, value in step_context.items():
                    print(f"    {key}: {str(value)[:50]}...")

                result = await self.generate_with_config(step_context, generation_config)
                results[step_name] = {"result": result, "type": "generated_content"}
                print(f"📝 Generated content: {result[:100]}...")

                # Update context if specified
                if "context_key" in step_config:
                    context[step_config["context_key"]] = result

            elif step_type == "process":
                # Process previous results
                processor = step_config.get("processor", "identity")
                input_key = step_config.get("input", "")

                if input_key in results:
                    processed = self._apply_processor(results[input_key], processor, step_config)
                    results[step_name] = processed

            else:
                print(f"⚠️  Unknown step type: {step_type}")

            print(f"✅ Completed step: {step_name}")
            print(f"📊 Updated context keys: {list(context.keys())}")

        return {
            "workflow": workflow_config.get("name", "unnamed"),
            "results": results,
            "final_context": context,
            "success": True
        }

    def _build_prompt_from_config(self, context: Dict[str, Any],
                                generation_config: Dict[str, Any]) -> str:
        """Build prompt from configuration with enhanced debugging"""

        # Get base prompt template
        prompt_template = generation_config.get("prompt_template", "{topic}")

        # Get formatting instructions
        format_instructions = generation_config.get("format_instructions", "")

        # Get constraints
        constraints = generation_config.get("constraints", [])

        # Build full prompt
        prompt_parts = []

        # Add format instructions if specified
        if format_instructions:
            prompt_parts.append(format_instructions)

        # Add constraints
        if constraints:
            prompt_parts.append("Requirements:")
            for constraint in constraints:
                prompt_parts.append(f"- {constraint}")

        # ENHANCED: Debug context substitution
        print(f"🔧 Building prompt with context keys: {list(context.keys())}")
        print(f"🔧 Prompt template: {prompt_template}")
        
        # Add context and template
        try:
            formatted_template = prompt_template.format(**context)
            prompt_parts.append(formatted_template)
            print(f"✅ Successfully formatted template")
        except KeyError as e:
            print(f"⚠️  Missing context key for template: {e}")
            print(f"    Available keys: {list(context.keys())}")
            # Try to substitute what we can
            formatted_template = prompt_template
            for key, value in context.items():
                formatted_template = formatted_template.replace(f"{{{key}}}", str(value))
            
            # Check for remaining placeholders
            remaining = re.findall(r'\{(\w+)\}', formatted_template)
            if remaining:
                print(f"⚠️  Unresolved placeholders: {remaining}")
            
            prompt_parts.append(formatted_template)

        final_prompt = "\n\n".join(prompt_parts)
        print(f"📝 Final prompt (first 200 chars): {final_prompt[:200]}...")
        return final_prompt

    def _process_response_from_config(self, response: str,
                                    generation_config: Dict[str, Any]) -> str:
        """Process response based on configuration with ENHANCED haiku cleaning"""

        processors = generation_config.get("post_processors", [])

        processed_response = response

        for processor in processors:
            processor_type = processor.get("type", "identity")

            if processor_type == "clean_haiku":
                # ENHANCED: Extract clean haiku lines from verbose model output
                max_lines = processor.get("max_lines", 3)
                
                # Split into lines and clean each line
                lines = processed_response.split('\n')
                clean_lines = []
                
                for line in lines:
                    line = line.strip()
                    
                    # Skip empty lines
                    if not line:
                        continue
                    
                    # Skip lines that look like instructions or formatting
                    skip_patterns = [
                        'first line', 'second line', 'third line', 'line 1', 'line 2', 'line 3',
                        'syllable', 'haiku', 'requirement', 'output', 'format', 
                        '[', ']', '(', ')', 'write', 'return', 'exactly', 'only',
                        'inspiration:', 'topic:', 'research:', '===', '---', 
                        'created:', 'about', 'pattern'
                    ]
                    
                    should_skip = False
                    for pattern in skip_patterns:
                        if pattern.lower() in line.lower():
                            should_skip = True
                            break
                    
                    # Skip very short lines (likely not haiku content)
                    if len(line) < 3:
                        should_skip = True
                    
                    # Skip lines that are mostly punctuation or formatting
                    if len([c for c in line if c.isalpha()]) < 3:
                        should_skip = True
                    
                    if not should_skip:
                        # Clean up common artifacts
                        line = line.replace('[', '').replace(']', '')
                        line = line.replace('- ', '')
                        line = line.replace('* ', '')
                        
                        # Remove leading numbers (1., 2., 3., etc.)
                        line = re.sub(r'^\d+\.\s*', '', line)
                        
                        if line.strip():
                            clean_lines.append(line.strip())
                
                # Take only the requested number of lines
                processed_response = '\n'.join(clean_lines[:max_lines])
                print(f"🎨 Clean haiku extraction: {len(clean_lines)} lines found, using first {max_lines}")

            elif processor_type == "extract_lines":
                # Extract specific lines
                max_lines = processor.get("max_lines", 3)
                lines = processed_response.strip().split('\n')
                clean_lines = [line.strip() for line in lines if line.strip()]
                processed_response = '\n'.join(clean_lines[:max_lines])

            elif processor_type == "filter_patterns":
                # Filter out lines matching patterns
                patterns = processor.get("patterns", [])
                lines = processed_response.split('\n')
                filtered_lines = []

                for line in lines:
                    should_keep = True
                    for pattern in patterns:
                        if pattern.lower() in line.lower():
                            should_keep = False
                            break
                    if should_keep and line.strip():
                        filtered_lines.append(line.strip())

                processed_response = '\n'.join(filtered_lines)

            elif processor_type == "truncate":
                # Truncate to max length
                max_length = processor.get("max_length", 1000)
                if len(processed_response) > max_length:
                    processed_response = processed_response[:max_length].strip()

            elif processor_type == "clean_formatting":
                # Remove numbered lists, bullets, etc.
                lines = processed_response.split('\n')
                clean_lines = []

                for line in lines:
                    clean_line = line.strip()
                    # Skip numbered lists, bullets
                    if (clean_line.startswith(('1.', '2.', '3.', '4.', '5.', '-', '*')) or
                        len(clean_line) > processor.get("max_line_length", 100)):
                        continue
                    if clean_line:
                        clean_lines.append(clean_line)

                processed_response = '\n'.join(clean_lines)

        return processed_response.strip()

    def _resolve_params_from_context(self, params: Dict[str, Any],
                                   context: Dict[str, Any],
                                   results: Dict[str, Any]) -> Dict[str, Any]:
        """FIXED: Resolve dynamic parameters from context and results with enhanced debugging"""
        resolved = {}

        for key, value in params.items():
            if isinstance(value, str) and value.startswith("${"):
                # Dynamic parameter - resolve from context or results
                var_name = value[2:-1]  # Remove ${ and }
                
                print(f"🔍 Resolving variable: {var_name}")
                print(f"🔍 Available in context: {list(context.keys())}")
                print(f"🔍 Available in results: {list(results.keys())}")

                if var_name in context:
                    resolved[key] = context[var_name]
                    print(f"✅ Resolved {var_name} from context: {str(resolved[key])[:50]}...")
                elif var_name in results:
                    # Extract result value if it's a dict
                    result_value = results[var_name]
                    if isinstance(result_value, dict) and "result" in result_value:
                        resolved[key] = result_value["result"]
                        print(f"✅ Resolved {var_name} from results.result: {str(resolved[key])[:50]}...")
                    else:
                        resolved[key] = str(result_value)
                        print(f"✅ Resolved {var_name} from results: {str(resolved[key])[:50]}...")
                else:
                    # FIXED: Better resolution for workflow step results
                    found = False
                    for step_name, step_result in results.items():
                        if var_name == step_name:
                            if isinstance(step_result, dict):
                                if "result" in step_result:
                                    resolved[key] = step_result["result"]
                                    print(f"✅ Resolved {var_name} from {step_name}.result: {str(resolved[key])[:50]}...")
                                    found = True
                                    break
                                else:
                                    resolved[key] = str(step_result)
                                    print(f"✅ Resolved {var_name} from {step_name}: {str(resolved[key])[:50]}...")
                                    found = True
                                    break
                    
                    if not found:
                        print(f"⚠️  Could not resolve parameter: {var_name}")
                        print(f"    Available results: {list(results.keys())}")
                        for step_name, step_result in results.items():
                            if isinstance(step_result, dict):
                                print(f"    {step_name}: {list(step_result.keys())}")
                        
                        # Don't use the template variable, use descriptive placeholder
                        resolved[key] = f"[Could not resolve: {var_name}]"
            else:
                resolved[key] = value

        return resolved

    def _apply_processor(self, data: Any, processor_type: str, config: Dict[str, Any]) -> Any:
        """Apply data processor"""
        if processor_type == "identity":
            return data
        elif processor_type == "extract_field":
            field = config.get("field", "result")
            if isinstance(data, dict) and field in data:
                return data[field]
            return data
        elif processor_type == "format_as_text":
            if isinstance(data, dict):
                return str(data.get("result", data))
            return str(data)
        else:
            return data

# Factory function for configuration-driven agent creation
def create_agent_from_config(channel, agent_config: Dict[str, Any]) -> ConfigurableMCPAgent:
    """Create agent from configuration dictionary"""
    return ConfigurableMCPAgent(channel, agent_config)
