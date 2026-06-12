# test_mcp.py - Simple test script
import asyncio
import sys
import os

# Add current directory to path so imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp_manager import SimpleMCPManager, create_filesystem_server, create_web_search_server

async def test_mcp_basic():
    """Test basic MCP functionality"""
    print("🧪 Testing MCP Basic Functionality")
    
    # Create manager
    manager = SimpleMCPManager()
    await manager.start()
    
    try:
        # Register servers
        fs_server = create_filesystem_server("test_fs", "/tmp")
        search_server = create_web_search_server("test_search")
        
        await manager.register_server(fs_server)
        await manager.register_server(search_server)
        
        # Start servers
        fs_started = await manager.start_server("test_fs")
        search_started = await manager.start_server("test_search")
        
        print(f"   Filesystem server: {'✅' if fs_started else '❌'}")
        print(f"   Search server: {'✅' if search_started else '❌'}")
        
        # Test tools
        if fs_started:
            result = await manager.call_tool("write_file", {"path": "/tmp/test.txt", "content": "Hello MCP!"})
            print(f"   Write file: {'✅' if result['success'] else '❌'}")
            
            result = await manager.call_tool("read_file", {"path": "/tmp/test.txt"})
            print(f"   Read file: {'✅' if result['success'] else '❌'}")
        
        if search_started:
            result = await manager.call_tool("web_search", {"query": "test search"})
            print(f"   Web search: {'✅' if result['success'] else '❌'}")
        
        # Show available tools
        tools = await manager.get_available_tools()
        print(f"   Available tools: {tools}")
        
        print("✅ MCP basic test completed successfully!")
        
    finally:
        await manager.stop()

if __name__ == "__main__":
    asyncio.run(test_mcp_basic())
