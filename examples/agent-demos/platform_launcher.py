#!/usr/bin/env python3
# platform_launcher.py - Universal launcher for any agent orchestration platform
"""
Universal Agent Platform Launcher

This script demonstrates the power of your MCP infrastructure by allowing
customers to switch between ANY orchestration platform while keeping the
same tools and infrastructure.

Usage:
    python platform_launcher.py --platform crewai --task "research quantum computing"
    python platform_launcher.py --platform autogen --task "research quantum computing"  
    python platform_launcher.py --platform aws --task "research quantum computing"
    python platform_launcher.py --platform google --task "research quantum computing"
    python platform_launcher.py --platform native --task "research quantum computing"
"""

import argparse
import asyncio
import json
import sys
import os
import base64
from pathlib import Path
from typing import Dict, Any, Optional

# This demo drives the agent framework, which lives in agent/ and uses bare imports.
_AGENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "agent")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

# Platform availability checks
PLATFORM_AVAILABLE = {
    "crewai": False,
    "autogen": False, 
    "aws": False,
    "google": False,
    "native": True  # Always available (your framework)
}

try:
    import crewai
    PLATFORM_AVAILABLE["crewai"] = True
except ImportError:
    pass

try:
    import autogen
    PLATFORM_AVAILABLE["autogen"] = True
except ImportError:
    pass

try:
    import boto3
    PLATFORM_AVAILABLE["aws"] = True
except ImportError:
    pass

try:
    import vertexai
    PLATFORM_AVAILABLE["google"] = True
except ImportError:
    pass

class UniversalAgentLauncher:
    """Universal launcher that works with any orchestration platform"""
    
    def __init__(self, server_url: str, task_id: str, aes_key: bytes = None):
        self.server_url = server_url
        self.task_id = task_id
        self.aes_key = aes_key
        
    async def launch_platform(self, platform: str, task: str, config_overrides: Dict[str, Any] = None) -> Dict[str, Any]:
        """Launch task on specified platform"""
        
        if not PLATFORM_AVAILABLE[platform]:
            return {
                "success": False,
                "error": f"Platform '{platform}' not available. Install required dependencies.",
                "install_hint": self._get_install_hint(platform)
            }
        
        # Generate appropriate config for platform
        config = self._generate_config(platform, task, config_overrides)
        
        # Save temporary config file
        config_file = f"temp_{platform}_config.yaml"
        self._save_config(config, config_file)
        
        try:
            # Launch on appropriate platform
            if platform == "crewai":
                result = await self._launch_crewai(config_file, task)
            elif platform == "autogen":
                result = await self._launch_autogen(config_file, task)
            elif platform == "aws":
                result = await self._launch_aws(config_file, task)
            elif platform == "google":
                result = await self._launch_google(config_file, task)
            elif platform == "native":
                result = await self._launch_native(config_file, task)
            else:
                raise ValueError(f"Unknown platform: {platform}")
            
            # Cleanup
            if os.path.exists(config_file):
                os.remove(config_file)
            
            return {
                "success": True,
                "platform": platform,
                "result": result,
                "message": f"Successfully executed on {platform}"
            }
            
        except Exception as e:
            return {
                "success": False,
                "platform": platform,
                "error": str(e),
                "message": f"Failed to execute on {platform}"
            }
    
    def _generate_config(self, platform: str, task: str, overrides: Dict[str, Any] = None) -> Dict[str, Any]:
        """Generate platform-specific configuration"""
        
        # Base MCP configuration (same for all platforms)
        base_mcp = {
            "prefer_real_servers": True,
            "servers": [
                {
                    "name": "workspace",
                    "command": ["python", "-m", "mcp_filesystem", "."],
                    "mode": "real",
                    "tools": ["read_file", "write_file", "list_directory", "create_directory"]
                },
                {
                    "name": "research",
                    "command": ["python", "-m", "mcp_web_search"],
                    "mode": "simulation",  # Change to "real" when web search is implemented
                    "tools": ["web_search", "search_documents"]
                }
            ]
        }
        
        # Platform-specific configurations
        configs = {
            "crewai": {
                "orchestrator": "crewai",
                "mcp": base_mcp,
                "agents": [
                    {
                        "role": "Research Specialist",
                        "goal": f"Complete the task: {task}",
                        "backstory": "Expert researcher with access to web search and file operations",
                        "verbose": True
                    }
                ],
                "tasks": [
                    {
                        "description": task,
                        "agent_index": 0,
                        "expected_output": "Complete task results"
                    }
                ],
                "process": "sequential"
            },
            
            "autogen": {
                "orchestrator": "autogen", 
                "mcp": base_mcp,
                "agents": [
                    {
                        "name": "assistant",
                        "system_message": f"You are an AI assistant. Complete this task: {task}. Use available MCP tools as needed.",
                        "llm_config": {
                            "config_list": [{"model": "gpt-4", "api_key": os.getenv("OPENAI_API_KEY", "placeholder")}]
                        }
                    }
                ],
                "initial_message": task,
                "max_round": 10
            },
            
            "aws": {
                "orchestrator": "aws",
                "aws": {
                    "bedrock_agent_id": os.getenv("BEDROCK_AGENT_ID", "placeholder")
                },
                "mcp": base_mcp,
                "test_input": task
            },
            
            "google": {
                "orchestrator": "google",
                "google": {
                    "project_id": os.getenv("GOOGLE_CLOUD_PROJECT", "placeholder"),
                    "location": "us-central1"
                },
                "mcp": base_mcp,
                "agents": [
                    {
                        "name": "assistant",
                        "model_name": "gemini-1.5-pro",
                        "system_instruction": f"Complete this task: {task}. Use available MCP tools."
                    }
                ],
                "test_input": task
            },
            
            "native": {
                "agent": {
                    "name": "NativeAgent",
                    "description": "Native MCP agent"
                },
                "execution_mode": "workflow",
                "mcp": base_mcp,
                "workflows": {
                    "dynamic": {
                        "name": "Dynamic Task Execution",
                        "steps": [
                            {
                                "name": "research",
                                "type": "tool_call",
                                "tool": "web_search",
                                "params": {"query": task},
                                "update_context": "research_data"
                            },
                            {
                                "name": "save_result",
                                "type": "tool_call", 
                                "tool": "write_file",
                                "params": {
                                    "path": "task_result.txt",
                                    "content": "Task: " + task + "\nResearch: ${research_data}"
                                }
                            }
                        ]
                    }
                },
                "default_workflow": "dynamic"
            }
        }
        
        config = configs[platform]
        
        # Apply overrides
        if overrides:
            config.update(overrides)
        
        return config
    
    def _save_config(self, config: Dict[str, Any], filename: str):
        """Save configuration to YAML file"""
        import yaml
        with open(filename, 'w') as f:
            yaml.dump(config, f, default_flow_style=False)
    
    async def _launch_crewai(self, config_file: str, task: str) -> Dict[str, Any]:
        """Launch task on CrewAI using remote inference"""
        try:
            from crewai_remote_bridge import run_crewai_with_remote_inference
            result = await run_crewai_with_remote_inference(config_file, self.server_url, self.task_id, self.aes_key)
            return {"platform_result": result}
        except ImportError:
            raise Exception("CrewAI remote bridge not found. Save crewai_remote_bridge.py first.")
    
    async def _launch_autogen(self, config_file: str, task: str) -> Dict[str, Any]:
        """Launch task on AutoGen"""
        try:
            from autogen_integration import run_autogen_from_config
            result = await run_autogen_from_config(config_file, self.server_url, self.task_id, self.aes_key)
            return {"platform_result": result}
        except ImportError:
            raise Exception("AutoGen integration module not found. Save autogen_integration.py first.")
    
    async def _launch_aws(self, config_file: str, task: str) -> Dict[str, Any]:
        """Launch task on AWS"""
        try:
            from aws_integration import run_aws_from_config
            result = await run_aws_from_config(config_file, self.server_url, self.task_id, self.aes_key)
            return {"platform_result": result}
        except ImportError:
            raise Exception("AWS integration module not found. Save aws_integration.py first.")
    
    async def _launch_google(self, config_file: str, task: str) -> Dict[str, Any]:
        """Launch task on Google AI"""
        try:
            from google_integration import run_google_from_config
            result = await run_google_from_config(config_file, self.server_url, self.task_id, self.aes_key)
            return {"platform_result": result}
        except ImportError:
            raise Exception("Google AI integration module not found. Save google_integration.py first.")
    
    async def _launch_native(self, config_file: str, task: str) -> Dict[str, Any]:
        """Launch task on native framework"""
        try:
            from start_agent import main as run_native
            import sys
            
            # Temporarily override sys.argv for native agent
            original_argv = sys.argv.copy()
            sys.argv = [
                "start_agent.py",
                "--task-id", self.task_id,
                "--config", config_file,
                "--workflow", "dynamic",
                "--topic", task,
                "--output-file", "native_result.txt"
            ]
            
            if self.aes_key:
                sys.argv.extend(["--aes-key", "aes.key"])
            
            try:
                result = await run_native()
                return {"platform_result": result}
            finally:
                sys.argv = original_argv
                
        except ImportError:
            raise Exception("Native agent not found. Ensure start_agent.py is available.")
    
    def _get_install_hint(self, platform: str) -> str:
        """Get installation hints for platforms"""
        hints = {
            "crewai": "pip install crewai",
            "autogen": "pip install pyautogen",
            "aws": "pip install boto3",
            "google": "pip install google-cloud-aiplatform"
        }
        return hints.get(platform, "Check platform documentation")

async def demo_all_platforms(launcher: UniversalAgentLauncher, task: str):
    """Demonstrate the same task running on all available platforms"""
    print(f"🚀 Running task '{task}' on all available platforms...\n")
    
    results = {}
    
    for platform in ["native", "crewai", "autogen", "aws", "google"]:
        print(f"{'='*60}")
        print(f"🔧 Testing {platform.upper()} Platform")
        print(f"{'='*60}")
        
        if not PLATFORM_AVAILABLE[platform]:
            print(f"❌ {platform} not available - {launcher._get_install_hint(platform)}")
            continue
        
        try:
            result = await launcher.launch_platform(platform, task)
            results[platform] = result
            
            if result["success"]:
                print(f"✅ {platform}: SUCCESS")
                print(f"   Result: {str(result['result'])[:100]}...")
            else:
                print(f"❌ {platform}: FAILED")
                print(f"   Error: {result['error']}")
                
        except Exception as e:
            print(f"💥 {platform}: EXCEPTION - {e}")
            results[platform] = {"success": False, "error": str(e)}
        
        print()
    
    # Summary
    print(f"{'='*60}")
    print("📊 PLATFORM COMPARISON SUMMARY")
    print(f"{'='*60}")
    
    for platform, result in results.items():
        status = "✅ SUCCESS" if result.get("success") else "❌ FAILED" 
        print(f"{platform:10} | {status}")
    
    print(f"\n🎯 All platforms use the SAME MCP infrastructure!")
    print(f"🔧 Customer can switch platforms without changing tools!")

async def main():
    parser = argparse.ArgumentParser(description="Universal Agent Platform Launcher")
    parser.add_argument("--platform", 
                       choices=["crewai", "autogen", "aws", "google", "native", "all"],
                       default="native",
                       help="Agent orchestration platform to use")
    parser.add_argument("--task", 
                       default="research quantum computing and create a summary",
                       help="Task to execute")
    parser.add_argument("--server", 
                       default="http://localhost:9000",
                       help="Pixi Runner server URL")
    parser.add_argument("--task-id", 
                       required=True,
                       help="Remote task ID")
    parser.add_argument("--aes-key",
                       help="AES key file path")
    parser.add_argument("--demo",
                       action="store_true", 
                       help="Run demo on all available platforms")
    
    args = parser.parse_args()
    
    # Load AES key if provided
    aes_key = None
    if args.aes_key:
        try:
            with open(args.aes_key, 'rb') as f:
                aes_key = base64.b64decode(f.read().strip())
        except Exception as e:
            print(f"❌ Error loading AES key: {e}")
            return 1
    
    # Create launcher
    launcher = UniversalAgentLauncher(args.server, args.task_id, aes_key)
    
    # Run demo or single platform
    if args.demo or args.platform == "all":
        await demo_all_platforms(launcher, args.task)
    else:
        print(f"🚀 Launching task on {args.platform}...")
        result = await launcher.launch_platform(args.platform, args.task)
        
        if result["success"]:
            print(f"✅ Success on {args.platform}!")
            print(f"Result: {json.dumps(result, indent=2)}")
        else:
            print(f"❌ Failed on {args.platform}")
            print(f"Error: {result['error']}")
            return 1
    
    return 0

if __name__ == "__main__":
    exit(asyncio.run(main()))
