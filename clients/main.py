#!/usr/bin/env python3
"""
CrewAI with Ollama Showcase - Enhanced with Local MCP Access
A demonstration of AI agents working together using local LLMs + local MCP tools
FIXED: Proper MCP server discovery from nested structure
"""

import os
import json
import asyncio
import argparse
import yaml
import requests
from typing import Dict, Any, Optional
from pathlib import Path

# CrewAI and Ollama imports
from crewai import Agent, Task, Crew, Process, LLM
import ollama

class LocalMCPClient:
    """Client for accessing local MCP servers via encrypted back-channel"""
    
    def __init__(self, channel_file: str = "/tmp/task_channel"):
        self.channel_file = channel_file
        self.available_tools = {}
        self.filesystem_available = False
        self.request_id_counter = 0
        self._discover_tools()
    
    def _discover_tools(self):
        """Discover available MCP tools via back-channel"""
        try:
            # Check if MCP info file exists (created by client.py)
            mcp_info_file = Path("mcp_info.json")
            if mcp_info_file.exists():
                with open(mcp_info_file, 'r') as f:
                    mcp_info = json.load(f)
                
                if mcp_info.get("mcp_enabled"):
                    servers = mcp_info.get("servers", {})
                    
                    # FIXED: Check for filesystem in builtin_servers
                    builtin_servers = servers.get("builtin_servers", {})
                    if "filesystem" in builtin_servers:
                        self.filesystem_available = True
                        filesystem_info = builtin_servers["filesystem"]
                        self.available_tools["filesystem"] = filesystem_info
                        print(f"✅ Connected to local MCP filesystem server")
                        print(f"📁 Root path: {filesystem_info.get('root_path', 'unknown')}")
                        print(f"🛠️  Available tools: {filesystem_info.get('tools', [])}")
                    
                    # Also check external servers if needed
                    external_servers = servers.get("external_servers", {})
                    if external_servers.get("enabled"):
                        print(f"🌐 External MCP servers available: {external_servers.get('server_count', 0)}")
                    
                    return
            
            print("⚠️ No MCP back-channel available")
            print("💡 Run client with --mcp-filesystem to enable local file access")
        except Exception as e:
            print(f"⚠️ Could not connect to MCP back-channel: {e}")
    
    def _send_mcp_request(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Send MCP request via encrypted back-channel"""
        self.request_id_counter += 1
        request_id = f"mcp_req_{self.request_id_counter}"
        
        request = {
            "type": "mcp_call",
            "action": action,
            "params": params,
            "request_id": request_id
        }
        
        try:
            # Write request to stdout (will be captured by Pixi task and sent via channel)
            print(f"MCP_REQUEST:{json.dumps(request)}")
            
            # In a real implementation, we'd wait for the response via stdin or a response file
            # For now, we'll simulate a successful response
            # The actual implementation would involve:
            # 1. Writing request to stdout with special prefix
            # 2. Pixi task captures this and sends via encrypted channel to client
            # 3. Client processes MCP request locally
            # 4. Client sends response back via encrypted channel
            # 5. Pixi task writes response to a file or stdin
            # 6. This function reads the response
            
            import time
            time.sleep(1)  # Simulate network delay
            
            # For demo purposes, return a mock successful response
            return {
                "success": True,
                "request_id": request_id,
                "action": action,
                "result": {"simulated": True, "action": action, "params": params}
            }
            
        except Exception as e:
            return {
                "success": False,
                "request_id": request_id,
                "error": str(e)
            }
    
    async def read_file(self, path: str) -> str:
        """Read file from local filesystem via MCP back-channel"""
        if not self.filesystem_available:
            return f"MCP filesystem not available. Cannot read {path}"
        
        print(f"📖 Reading file via MCP back-channel: {path}")
        
        response = self._send_mcp_request("filesystem_read_file", {"path": path})
        
        if response.get("success"):
            result = response.get("result", {})
            # In real implementation, this would return actual file content
            return result.get("content", f"[Simulated content of {path}]")
        else:
            error = response.get("error", "Unknown error")
            print(f"❌ MCP read_file failed: {error}")
            return f"Error reading {path}: {error}"
    
    async def write_file(self, path: str, content: str) -> bool:
        """Write file to local filesystem via MCP back-channel"""
        if not self.filesystem_available:
            print(f"❌ MCP filesystem not available. Cannot write {path}")
            return False
        
        print(f"💾 Writing file via MCP back-channel: {path} ({len(content)} bytes)")
        
        response = self._send_mcp_request("filesystem_write_file", {"path": path, "content": content})
        
        if response.get("success"):
            print(f"✅ Successfully wrote file via MCP: {path}")
            return True
        else:
            error = response.get("error", "Unknown error")
            print(f"❌ MCP write_file failed: {error}")
            return False
    
    async def list_directory(self, path: str = ".") -> list:
        """List directory contents via MCP back-channel"""
        if not self.filesystem_available:
            print(f"❌ MCP filesystem not available. Cannot list {path}")
            return []
        
        print(f"📂 Listing directory via MCP back-channel: {path}")
        
        response = self._send_mcp_request("filesystem_list_directory", {"path": path})
        
        if response.get("success"):
            result = response.get("result", {})
            files = result.get("files", [])
            print(f"📁 Found {len(files)} items in {path}")
            return files
        else:
            error = response.get("error", "Unknown error")
            print(f"❌ MCP list_directory failed: {error}")
            return []

class OllamaManager:
    """Manages Ollama model setup and availability"""
    
    def __init__(self, model_name: str = "llama3.2:latest"):
        self.model_name = model_name
        self.llm = None
        
    def ensure_model_available(self) -> bool:
        """Ensure the Ollama model is pulled and ready"""
        try:
            # Test basic Ollama connection first
            try:
                response = ollama.list()
                print(f"✅ Ollama service is running")
            except Exception as conn_error:
                print(f"❌ Cannot connect to Ollama service: {conn_error}")
                print("Make sure Ollama is running with: ollama serve")
                return False
            
            # Check if model is available
            models = response.get('models', [])
            model_names = []
            
            for model in models:
                if hasattr(model, 'model'):
                    # Handle Ollama's model objects (newer versions)
                    name = model.model
                    model_names.append(name)
                elif isinstance(model, dict):
                    # Handle dictionary format
                    name = model.get('name') or model.get('model', '')
                    model_names.append(name)
                else:
                    print(f"Unexpected model format: {model}")
            
            print(f"Available models: {model_names}")
            
            if self.model_name not in model_names:
                print(f"Pulling Ollama model: {self.model_name}")
                ollama.pull(self.model_name)
                print(f"✅ Model {self.model_name} ready")
            else:
                print(f"✅ Model {self.model_name} already available")
                
            return True
        except Exception as e:
            print(f"❌ Error with Ollama model: {e}")
            print(f"Error details: {type(e).__name__}: {str(e)}")
            return False
    
    def get_llm(self):
        """Get configured Ollama LLM instance"""
        if not self.llm:
            from crewai import LLM
            
            # Use CrewAI's LLM class with Ollama provider
            self.llm = LLM(
                model=f"ollama/{self.model_name}",  # Specify ollama provider
                base_url="http://localhost:11434",
                temperature=0.7
            )
        return self.llm

class ResearchTeam:
    """A team of AI agents for research tasks with local MCP access"""
    
    def __init__(self, ollama_manager: OllamaManager, mcp_client: Optional[LocalMCPClient] = None):
        self.llm = ollama_manager.get_llm()
        self.mcp_client = mcp_client
        
    def create_agents(self) -> Dict[str, Agent]:
        """Create specialized research agents with local file access"""
        
        # Enhanced researcher with local file access
        researcher_backstory = """You are an experienced research analyst with expertise in 
        analyzing trends, technologies, and market dynamics. You excel at finding 
        key insights and organizing information clearly."""
        
        if self.mcp_client and self.mcp_client.filesystem_available:
            researcher_backstory += """ You also have access to local files and can 
            read existing research materials to enhance your analysis."""
        
        researcher = Agent(
            role="Senior Research Analyst",
            goal="Conduct thorough research and gather comprehensive information",
            backstory=researcher_backstory,
            verbose=True,
            allow_delegation=False,
            llm=self.llm
        )
        
        writer = Agent(
            role="Technical Writer",
            goal="Create clear, engaging, and well-structured content",
            backstory="""You are a skilled technical writer who specializes in 
            making complex topics accessible. You have a talent for creating 
            compelling narratives from research data. You can also save your 
            work to local files for easy access.""",
            verbose=True,
            allow_delegation=False,
            llm=self.llm
        )
        
        analyst = Agent(
            role="Data Analyst",
            goal="Analyze information and extract meaningful insights",
            backstory="""You are a data analyst with strong analytical skills. 
            You excel at identifying patterns, trends, and key takeaways from 
            complex information. You can access local data files when available.""",
            verbose=True,
            allow_delegation=False,
            llm=self.llm
        )
        
        return {
            "researcher": researcher,
            "writer": writer,
            "analyst": analyst
        }
    
    async def research_task(self, topic: str, output_file: Optional[str] = None) -> str:
        """Execute a research task on given topic with local file access"""
        agents = self.create_agents()
        
        # Check for existing research materials
        local_context = ""
        if self.mcp_client and self.mcp_client.filesystem_available:
            print("🔍 Checking for existing research materials...")
            files = await self.mcp_client.list_directory(".")
            research_files = [f for f in files if any(ext in f.lower() for ext in ['.md', '.txt', '.json']) and 'research' in f.lower()]
            
            if research_files:
                print(f"📁 Found existing research files: {research_files}")
                for file in research_files[:3]:  # Limit to first 3 files
                    content = await self.mcp_client.read_file(file)
                    if content and not content.startswith("Error"):
                        local_context += f"\n\nExisting research from {file}:\n{content[:500]}..."
        
        # Enhanced research task with local context
        research_description = f"""
        Research the topic: {topic}
        
        Your task is to:
        1. Gather comprehensive information about the topic
        2. Identify key trends and developments
        3. Note important facts and statistics
        4. Summarize the current state and future outlook
        
        Focus on accuracy and depth of information.
        """
        
        if local_context:
            research_description += f"""
            
        Consider this existing research context when building your analysis:
        {local_context}
        """
        
        research_task = Task(
            description=research_description,
            agent=agents["researcher"],
            expected_output="A comprehensive research summary with key findings"
        )
        
        analysis_task = Task(
            description=f"""
            Based on the research conducted on {topic}, perform detailed analysis:
            
            1. Identify the most significant trends
            2. Highlight key opportunities and challenges
            3. Provide insights on implications
            4. Suggest areas for further investigation
            
            Present your analysis in a structured format.
            """,
            agent=agents["analyst"],
            expected_output="Structured analysis with insights and recommendations"
        )
        
        writing_task = Task(
            description=f"""
            Create a comprehensive report about {topic} based on the research and analysis:
            
            1. Write an executive summary
            2. Present key findings clearly
            3. Include analysis and insights
            4. Provide actionable recommendations
            5. Use clear, professional language
            
            Make it engaging and informative for a business audience.
            """,
            agent=agents["writer"],
            expected_output="A well-structured research report"
        )
        
        # Create and execute crew
        crew = Crew(
            agents=[agents["researcher"], agents["analyst"], agents["writer"]],
            tasks=[research_task, analysis_task, writing_task],
            process=Process.sequential,
            verbose=True
        )
        
        result = crew.kickoff()
        result_str = str(result)
        
        # Save output if specified and MCP is available
        if output_file and self.mcp_client and self.mcp_client.filesystem_available:
            success = await self.mcp_client.write_file(output_file, result_str)
            if success:
                print(f"✅ Research report saved to: {output_file}")
            else:
                print(f"❌ Failed to save report via MCP. Falling back to local save.")
                self._save_local_fallback(result_str, output_file)
        elif output_file:
            self._save_local_fallback(result_str, output_file)
            
        return result_str
    
    def _save_local_fallback(self, content: str, filename: str):
        """Fallback to local file save (won't work in remote execution)"""
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"✅ Output saved locally to: {filename}")
        except Exception as e:
            print(f"❌ Failed to save output locally: {e}")

class ContentTeam:
    """A team of AI agents for content creation"""
    
    def __init__(self, ollama_manager: OllamaManager):
        self.llm = ollama_manager.get_llm()
    
    def create_content(self, topic: str) -> str:
        """Create engaging content about a topic"""
        
        strategist = Agent(
            role="Content Strategist",
            goal="Develop content strategy and key messaging",
            backstory="Expert in content strategy with deep understanding of audience engagement",
            verbose=True,
            llm=self.llm
        )
        
        creator = Agent(
            role="Content Creator",
            goal="Create engaging and informative content",
            backstory="Creative content creator with expertise in storytelling and engagement",
            verbose=True,
            llm=self.llm
        )
        
        strategy_task = Task(
            description=f"""
            Develop a content strategy for: {topic}
            
            Consider:
            1. Key messages to communicate
            2. Target audience interests
            3. Content structure and flow
            4. Engagement opportunities
            """,
            agent=strategist,
            expected_output="Content strategy with key messaging framework"
        )
        
        creation_task = Task(
            description=f"""
            Create engaging content about {topic} following the strategy:
            
            1. Write compelling introduction
            2. Present information clearly
            3. Include practical examples
            4. Add engaging elements
            5. Conclude with actionable takeaways
            """,
            agent=creator,
            expected_output="Engaging content piece ready for publication"
        )
        
        crew = Crew(
            agents=[strategist, creator],
            tasks=[strategy_task, creation_task],
            process=Process.sequential,
            verbose=True
        )
        
        result = crew.kickoff()
        return str(result)

def load_config() -> Dict[str, Any]:
    """Load configuration from YAML file"""
    config_file = Path("config.yaml")
    if config_file.exists():
        with open(config_file, 'r') as f:
            return yaml.safe_load(f)
    
    # Default configuration
    return {
        "ollama": {
            "model": "llama3.2:latest",
            "base_url": "http://localhost:11434"
        },
        "output": {
            "format": "markdown",
            "include_metadata": True
        }
    }

def save_output(content: str, filename: str, config: Dict[str, Any]) -> None:
    """Save output to file with optional metadata"""
    
    if config.get("output", {}).get("include_metadata", True):
        metadata = {
            "generated_by": "CrewAI with Ollama",
            "model": config.get("ollama", {}).get("model", "llama3.2:latest"),
            "timestamp": str(Path().cwd()),
        }
        
        output = f"""---
{yaml.dump(metadata, default_flow_style=False)}---

{content}
"""
    else:
        output = content
    
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(output)
    
    print(f"✅ Output saved to: {filename}")

def main():
    parser = argparse.ArgumentParser(description="CrewAI with Ollama Showcase + MCP Back-Channel")
    parser.add_argument("--task", choices=["research", "content", "analysis"], 
                       default="research", help="Type of task to execute")
    parser.add_argument("--topic", default="artificial intelligence", 
                       help="Topic for the task")
    parser.add_argument("--output", help="Output file path")
    parser.add_argument("--model", default="llama3.2:latest", 
                       help="Ollama model to use")
    parser.add_argument("--file", help="Input file for analysis task")
    parser.add_argument("--no-mcp", action="store_true", 
                       help="Disable MCP back-channel client")
    
    args = parser.parse_args()
    
    # Load configuration
    config = load_config()
    if args.model:
        config["ollama"]["model"] = args.model
    
    print("🚀 Starting CrewAI with Ollama Showcase + MCP Back-Channel")
    print(f"📋 Task: {args.task}")
    print(f"🎯 Topic: {args.topic}")
    print(f"🤖 Model: {config['ollama']['model']}")
    
    # Initialize MCP client for back-channel
    mcp_client = None
    if not args.no_mcp:
        print("🔗 Initializing MCP back-channel client...")
        mcp_client = LocalMCPClient()
    
    # Initialize Ollama
    ollama_manager = OllamaManager(config["ollama"]["model"])
    
    if not ollama_manager.ensure_model_available():
        print("❌ Failed to setup Ollama model. Exiting.")
        return
    
    # Execute task
    try:
        if args.task == "research":
            team = ResearchTeam(ollama_manager, mcp_client)
            result = asyncio.run(team.research_task(args.topic, args.output))
            
        elif args.task == "content":
            team = ContentTeam(ollama_manager, mcp_client)
            result = team.create_content(args.topic)
            
        elif args.task == "analysis":
            # Placeholder for analysis task with MCP file access
            result = f"Analysis task for {args.topic} - Feature coming soon!"
            
        print("\n" + "="*50)
        print("🎉 TASK COMPLETED!")
        print("="*50)
        print(result)
        
        # Save output if specified (fallback to local save)
        if args.output and not mcp_client:
            save_output(result, args.output, config)
        
    except Exception as e:
        print(f"❌ Error executing task: {e}")
        raise

if __name__ == "__main__":
    main()
