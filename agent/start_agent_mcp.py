# start_agent_mcp_fixed.py - Fixed version with clean MCP integration
import base64
import argparse
import asyncio
from ai_agent_framework import read_pixi_config, create_auth_headers, RemoteChannel
from agents.haiku_agent import HaikuAgent  # Your existing agent

# Import fixed MCP functionality
try:
    from mcp_agent import MCPHaikuAgent, MCPEnhancedAgent
    from mcp_manager import create_filesystem_server, create_web_search_server
    MCP_AVAILABLE = True
    print("✅ MCP features available (fixed version)")
except ImportError as e:
    MCP_AVAILABLE = False
    print(f"⚠️  MCP features not available: {e}")

def run_haiku_agent(server_url, task_id, aes_key_path=None, topic="wild reindeer", 
                   output_file="haiku.txt", mcp_mode="none"):
    """
    Fixed haiku agent runner with clean MCP integration.
    
    Args:
        mcp_mode: "none", "simple", "research"
    """
    print(f"🚀 Starting Haiku Agent (Fixed Version)")
    print(f"   Topic: {topic}")
    print(f"   MCP Mode: {mcp_mode}")
    
    # Load configuration (same as before)
    config = read_pixi_config()
    bearer_token = config.get("bearer_token") or config.get("bearer-token")
    snowflake_token = config.get("snowflake_token") or config.get("snowflake-token")
    auth_headers = create_auth_headers(bearer_token, snowflake_token)
    
    # Load AES key (same as before)
    aes_key = None
    if aes_key_path:
        with open(aes_key_path, "rb") as f:
            raw = f.read().strip()
            try:
                aes_key = base64.b64decode(raw)
                if len(aes_key) != 32:
                    print("Invalid AES key length")
                    return
                print("🔐 AES encryption enabled")
            except Exception as e:
                print(f"Error decoding AES key: {e}")
                return
    
    # Create remote channel (same as before)
    channel = RemoteChannel(server_url, task_id, aes_key, auth_headers)
    
    if mcp_mode == "none" or not MCP_AVAILABLE:
        # Use your original agent (100% backwards compatible)
        print("📝 Using original HaikuAgent...")
        agent = HaikuAgent(channel)
        result = agent.generate_haiku(topic, output_file)
        print("\nGenerated haiku:")
        print("-" * 40)
        print(result)
        print("-" * 40)
        
    elif mcp_mode == "simple":
        # Simple MCP enhancement
        print("🔧 Using MCP agent (simple mode)...")
        asyncio.run(_run_simple_mcp_agent(channel, topic, output_file))
        
    elif mcp_mode == "research":
        # Research-enhanced mode
        print("🔍 Using MCP agent (research mode)...")
        asyncio.run(_run_research_mcp_agent(channel, topic, output_file))

async def _run_simple_mcp_agent(channel, topic, output_file):
    """Run simple MCP-enhanced agent"""
    agent = MCPHaikuAgent(channel)
    
    try:
        # Minimal MCP setup - just filesystem
        servers = [create_filesystem_server("workspace", "/workspace")]
        await agent.initialize_mcp(servers)
        
        print("🛠️  MCP tools available: filesystem")
        
        # Use simple generation method
        result = await agent.generate_haiku_simple(topic, output_file)
        
        print("\n🎋 Generated haiku (simple MCP):")
        print("=" * 50)
        print(result)
        print("=" * 50)
        
    except Exception as e:
        print(f"❌ MCP simple mode failed: {e}")
        print("🔄 Falling back to standard agent...")
        
        # Fallback to original agent
        from agents.haiku_agent import HaikuAgent
        fallback_agent = HaikuAgent(channel)
        result = fallback_agent.generate_haiku(topic, output_file)
        print(f"✅ Fallback result: {result}")
        
    finally:
        await agent.cleanup_mcp()

async def _run_research_mcp_agent(channel, topic, output_file):
    """Run research-enhanced MCP agent"""
    agent = MCPHaikuAgent(channel)
    
    try:
        # Full MCP setup with research tools
        servers = [
            create_filesystem_server("workspace", "/workspace"),
            create_web_search_server("search")
        ]
        await agent.initialize_mcp(servers)
        
        tools = await agent.get_available_tools()
        print(f"🛠️  MCP tools available: {list(tools.keys())}")
        
        # Use research-enhanced generation
        result = await agent.generate_haiku_with_research(topic, output_file)
        
        print("\n🎋 Generated haiku (with research):")
        print("=" * 50)
        print(result)
        print("=" * 50)
        
    except Exception as e:
        print(f"❌ MCP research mode failed: {e}")
        print("🔄 Falling back to simple mode...")
        
        # Fallback to simple mode
        try:
            result = await agent.generate_haiku_simple(topic, output_file)
            print(f"✅ Simple fallback result: {result}")
        except Exception as e2:
            print(f"❌ Simple fallback also failed: {e2}")
            print("🔄 Using original agent...")
            
            # Final fallback to original
            from agents.haiku_agent import HaikuAgent
            fallback_agent = HaikuAgent(channel)
            result = fallback_agent.generate_haiku(topic, output_file)
            print(f"✅ Original agent result: {result}")
        
    finally:
        await agent.cleanup_mcp()

async def test_mcp_connection(channel):
    """Test MCP functionality with the remote connection"""
    if not MCP_AVAILABLE:
        print("❌ MCP not available for testing")
        return False
    
    print("🧪 Testing MCP with remote connection...")
    
    agent = MCPEnhancedAgent(channel)
    
    try:
        # Simple test setup
        servers = [create_filesystem_server("test", "/tmp")]
        await agent.initialize_mcp(servers)
        
        # Test basic functionality
        has_write = await agent.has_tool("write_file")
        print(f"   Write file tool: {'✅' if has_write else '❌'}")
        
        if has_write:
            test_result = await agent.use_tool("write_file", path="/tmp/mcp_test.txt", content="MCP test successful!")
            print(f"   Test write: {'✅' if test_result.get('success') else '❌'}")
        
        # Test simple generation
        test_haiku = await agent.generate_simple("Write a haiku about testing")
        print(f"   Simple generation: {'✅' if len(test_haiku) > 10 else '❌'}")
        print(f"   Generated: {test_haiku[:50]}...")
        
        print("✅ MCP connection test completed")
        return True
        
    except Exception as e:
        print(f"❌ MCP connection test failed: {e}")
        return False
        
    finally:
        await agent.cleanup_mcp()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fixed Haiku Agent with MCP")
    parser.add_argument("--server", default="http://localhost:9000", help="Pixi Runner server URL")
    parser.add_argument("--task-id", required=True, help="Remote task ID to connect to")
    parser.add_argument("--aes-key", help="Path to AES key file")
    parser.add_argument("--topic", default="wild reindeer", help="Topic for haiku generation")
    parser.add_argument("--output", default="haiku.txt", help="Output file")
    
    # Simplified MCP modes
    parser.add_argument("--mcp-mode", choices=["none", "simple", "research"], default="none",
                       help="MCP mode: none (original), simple (basic MCP), research (with web search)")
    
    # Test mode
    parser.add_argument("--test-mcp", action="store_true", help="Test MCP connection and functionality")
    
    args = parser.parse_args()
    
    if args.test_mcp:
        # Test MCP functionality
        config = read_pixi_config()
        bearer_token = config.get("bearer_token") or config.get("bearer-token")
        snowflake_token = config.get("snowflake_token") or config.get("snowflake-token")
        auth_headers = create_auth_headers(bearer_token, snowflake_token)
        
        aes_key = None
        if args.aes_key:
            with open(args.aes_key, "rb") as f:
                raw = f.read().strip()
                aes_key = base64.b64decode(raw)
        
        channel = RemoteChannel(args.server, args.task_id, aes_key, auth_headers)
        asyncio.run(test_mcp_connection(channel))
    else:
        # Run haiku generation
        run_haiku_agent(
            args.server,
            args.task_id,
            args.aes_key,
            args.topic,
            args.output,
            args.mcp_mode
        )
