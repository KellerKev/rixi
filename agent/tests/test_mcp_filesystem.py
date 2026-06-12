#!/usr/bin/env python3
# test_mcp_filesystem.py - Test the MCP filesystem server directly

import asyncio
import json
import os
from mcp_filesystem import MCPFilesystemHandler

async def test_filesystem_server():
    """Test the MCP filesystem server directly"""
    print("🧪 Testing MCP Filesystem Server...")
    
    # Initialize handler
    handler = MCPFilesystemHandler(".")
    
    # Test 1: Write a file
    print("\n📝 Test 1: Writing a test file...")
    write_result = await handler.handle_tool_call("write_file", {
        "path": "mcp_test.txt",
        "content": "Hello from MCP filesystem server!\nThis is a test file."
    })
    print(f"Write result: {json.dumps(write_result, indent=2)}")
    
    # Test 2: Read the file back
    print("\n📖 Test 2: Reading the test file...")
    read_result = await handler.handle_tool_call("read_file", {
        "path": "mcp_test.txt"
    })
    print(f"Read result: {json.dumps(read_result, indent=2)}")
    
    # Test 3: List directory
    print("\n📂 Test 3: Listing current directory...")
    list_result = await handler.handle_tool_call("list_directory", {
        "path": "."
    })
    print(f"Directory listing (first 5 items):")
    for item in list_result.get("items", [])[:5]:
        print(f"  {item['type']}: {item['name']}")
    
    # Test 4: Create directory
    print("\n📁 Test 4: Creating test directory...")
    mkdir_result = await handler.handle_tool_call("create_directory", {
        "path": "test_mcp_dir"
    })
    print(f"Create directory result: {json.dumps(mkdir_result, indent=2)}")
    
    # Test 5: Write file in new directory
    print("\n📝 Test 5: Writing file in new directory...")
    nested_write_result = await handler.handle_tool_call("write_file", {
        "path": "test_mcp_dir/nested_file.txt",
        "content": "This is a nested file!"
    })
    print(f"Nested write result: {json.dumps(nested_write_result, indent=2)}")
    
    # Verify files exist on disk
    print("\n🔍 Verification: Checking files on disk...")
    if os.path.exists("mcp_test.txt"):
        print("✅ mcp_test.txt exists on disk")
        with open("mcp_test.txt", "r") as f:
            content = f.read()
            print(f"   Content: {repr(content[:50])}...")
    else:
        print("❌ mcp_test.txt does not exist on disk")
    
    if os.path.exists("test_mcp_dir/nested_file.txt"):
        print("✅ test_mcp_dir/nested_file.txt exists on disk")
    else:
        print("❌ test_mcp_dir/nested_file.txt does not exist on disk")
    
    print("\n🧹 Cleaning up test files...")
    try:
        if os.path.exists("mcp_test.txt"):
            os.remove("mcp_test.txt")
            print("✅ Cleaned up mcp_test.txt")
        
        if os.path.exists("test_mcp_dir/nested_file.txt"):
            os.remove("test_mcp_dir/nested_file.txt")
            print("✅ Cleaned up nested_file.txt")
        
        if os.path.exists("test_mcp_dir"):
            os.rmdir("test_mcp_dir")
            print("✅ Cleaned up test_mcp_dir")
    except Exception as e:
        print(f"⚠️  Cleanup warning: {e}")
    
    print("\n🎉 MCP Filesystem Server test completed!")

async def test_with_mcp_manager():
    """Test using the MCP manager"""
    print("\n🔧 Testing with MCP Manager...")
    
    from mcp_manager import MCPManager, MCPServerConfig
    
    manager = MCPManager(prefer_real_servers=True)
    await manager.start()
    
    # Create filesystem server config
    config = MCPServerConfig(
        name="test_workspace",
        command=["python", "-m", "mcp_filesystem", "."],
        tools=["read_file", "write_file", "list_directory", "create_directory"],
        description="Test filesystem server",
        mode="real"
    )
    
    # Register and start server
    await manager.register_server(config)
    success = await manager.start_server("test_workspace")
    print(f"🚀 Server started: {success}")
    
    # Get status
    status = await manager.get_server_status()
    print(f"📊 Server status: {json.dumps(status, indent=2)}")
    
    # Test tool call
    try:
        result = await manager.call_tool("write_file", {
            "path": "manager_test.txt",
            "content": "Hello from MCP Manager!"
        })
        print(f"📝 Write via manager: {json.dumps(result, indent=2)}")
        
        # Verify file exists
        if os.path.exists("manager_test.txt"):
            print("✅ File created via MCP Manager!")
            os.remove("manager_test.txt")
            print("✅ Cleaned up manager_test.txt")
        else:
            print("❌ File not created via MCP Manager")
            
    except Exception as e:
        print(f"❌ Error calling tool via manager: {e}")
    
    await manager.stop()
    print("🔧 MCP Manager test completed!")

if __name__ == "__main__":
    print("🚀 Starting MCP Filesystem Tests...")
    asyncio.run(test_filesystem_server())
    asyncio.run(test_with_mcp_manager())
