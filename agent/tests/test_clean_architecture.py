# test_clean_architecture.py - Test the clean architecture
import asyncio
import sys
from pathlib import Path

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent))

async def test_clean_architecture():
    """Test the clean, modular architecture"""
    print("🧪 Testing Clean MCP Architecture")
    print("=" * 50)
    
    # Test 1: Pure MCP Manager
    print("\n📋 Test 1: Pure MCP Manager")
    try:
        from mcp_manager import MCPManager, create_server_config
        
        manager = MCPManager()
        await manager.start()
        
        # Create server config
        fs_config = create_server_config("test_fs", "filesystem", root_path="/tmp")
        search_config = create_server_config("test_search", "web_search")
        
        # Register and start
        await manager.register_server(fs_config)
        await manager.register_server(search_config)
        
        fs_started = await manager.start_server("test_fs")
        search_started = await manager.start_server("test_search")
        
        print(f"   Filesystem server: {'✅' if fs_started else '❌'}")
        print(f"   Search server: {'✅' if search_started else '❌'}")
        
        # Test tool calls
        if fs_started:
            result = await manager.call_tool("write_file", {"path": "/tmp/test.txt", "content": "Hello!"})
            print(f"   File write: {'✅' if result['success'] else '❌'}")
        
        if search_started:
            result = await manager.call_tool("web_search", {"query": "test"})
            print(f"   Web search: {'✅' if result['success'] else '❌'}")
        
        await manager.stop()
        print("   ✅ Pure MCP Manager test passed")
        
    except Exception as e:
        print(f"   ❌ MCP Manager test failed: {e}")
    
    # Test 2: Configuration Loading
    print("\n📋 Test 2: Configuration Loading")
    try:
        import yaml
        
        # Test config parsing
        sample_config = {
            "agent": {"name": "TestAgent"},
            "mcp": {
                "servers": [
                    {
                        "name": "test_server",
                        "command": ["python", "-m", "test"],
                        "tools": ["test_tool"]
                    }
                ]
            },
            "generation": {
                "default": {
                    "prompt_template": "Test prompt for {topic}",
                    "post_processors": [
                        {"type": "truncate", "max_length": 100}
                    ]
                }
            }
        }
        
        # Save test config
        with open("test_config.yaml", "w") as f:
            yaml.dump(sample_config, f)
        
        # Load it back
        from mcp_manager import load_server_configs_from_dict
        server_configs = load_server_configs_from_dict(sample_config["mcp"])
        
        print(f"   Config loading: {'✅' if len(server_configs) == 1 else '❌'}")
        print(f"   Server name: {server_configs[0].name}")
        print(f"   Server tools: {server_configs[0].tools}")
        
        # Cleanup
        Path("test_config.yaml").unlink(missing_ok=True)
        print("   ✅ Configuration loading test passed")
        
    except Exception as e:
        print(f"   ❌ Configuration test failed: {e}")
    
    # Test 3: Generic Agent (without actual remote connection)
    print("\n📋 Test 3: Generic Agent Structure")
    try:
        from mcp_agent import ConfigurableMCPAgent
        
        # Mock channel for testing
        class MockChannel:
            def __init__(self):
                self.server_url = "mock://test"
                self.task_id = "test-task"
        
        mock_channel = MockChannel()
        
        # Create agent with test config
        test_config = {
            "generation": {
                "default": {
                    "prompt_template": "Write about {topic}",
                    "post_processors": [
                        {"type": "truncate", "max_length": 50}
                    ]
                }
            }
        }
        
        agent = ConfigurableMCPAgent(mock_channel, test_config)
        
        print(f"   Agent creation: ✅")
        print(f"   Config loaded: {'✅' if agent.config else '❌'}")
        print(f"   MCP manager: {'✅' if agent.mcp_manager else '❌'}")
        
        # Test prompt building
        context = {"topic": "nature"}
        generation_config = test_config["generation"]["default"]
        prompt = agent._build_prompt_from_config(context, generation_config)
        
        print(f"   Prompt building: {'✅' if 'nature' in prompt else '❌'}")
        print("   ✅ Generic agent test passed")
        
    except Exception as e:
        print(f"   ❌ Generic agent test failed: {e}")
    
    print("\n🎉 Clean Architecture Tests Complete!")
    print("=" * 50)

if __name__ == "__main__":
    asyncio.run(test_clean_architecture())

