# mcp_filesystem.py - Real filesystem MCP server implementation
import json
import os
import pathlib
import sys
from typing import Dict, Any, List
import asyncio

class FilesystemMCPServer:
    """Real filesystem MCP server implementation"""
    
    def __init__(self, root_path: str = "."):
        self.root_path = pathlib.Path(root_path).resolve()
        self.ensure_safe_path(self.root_path)
        
    def ensure_safe_path(self, path: pathlib.Path) -> pathlib.Path:
        """Ensure path is within root directory for security"""
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root_path)
            return resolved
        except ValueError:
            raise PermissionError(f"Access denied: {path} is outside root directory")
    
    async def read_file(self, path: str, encoding: str = "utf-8") -> Dict[str, Any]:
        """Read file contents"""
        try:
            file_path = self.root_path / path
            safe_path = self.ensure_safe_path(file_path)
            
            if not safe_path.exists():
                return {"error": f"File not found: {path}", "success": False}
            
            if not safe_path.is_file():
                return {"error": f"Not a file: {path}", "success": False}
            
            content = safe_path.read_text(encoding=encoding)
            return {
                "success": True,
                "content": content,
                "path": str(safe_path.relative_to(self.root_path)),
                "size": len(content)
            }
            
        except Exception as e:
            return {"error": f"Read error: {str(e)}", "success": False}
    
    async def write_file(self, path: str, content: str, encoding: str = "utf-8") -> Dict[str, Any]:
        """Write file contents"""
        try:
            file_path = self.root_path / path
            safe_path = self.ensure_safe_path(file_path)
            
            # Create parent directories if they don't exist
            safe_path.parent.mkdir(parents=True, exist_ok=True)
            
            safe_path.write_text(content, encoding=encoding)
            
            return {
                "success": True,
                "path": str(safe_path.relative_to(self.root_path)),
                "bytes_written": len(content.encode(encoding)),
                "message": f"Successfully wrote {len(content)} characters to {path}"
            }
            
        except Exception as e:
            return {"error": f"Write error: {str(e)}", "success": False}
    
    async def list_directory(self, path: str = ".") -> Dict[str, Any]:
        """List directory contents"""
        try:
            dir_path = self.root_path / path
            safe_path = self.ensure_safe_path(dir_path)
            
            if not safe_path.exists():
                return {"error": f"Directory not found: {path}", "success": False}
            
            if not safe_path.is_dir():
                return {"error": f"Not a directory: {path}", "success": False}
            
            items = []
            for item in safe_path.iterdir():
                rel_path = item.relative_to(self.root_path)
                items.append({
                    "name": item.name,
                    "path": str(rel_path),
                    "type": "directory" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else None
                })
            
            return {
                "success": True,
                "items": sorted(items, key=lambda x: (x["type"], x["name"])),
                "count": len(items),
                "path": str(safe_path.relative_to(self.root_path))
            }
            
        except Exception as e:
            return {"error": f"List error: {str(e)}", "success": False}
    
    async def create_directory(self, path: str) -> Dict[str, Any]:
        """Create directory"""
        try:
            dir_path = self.root_path / path
            safe_path = self.ensure_safe_path(dir_path)
            
            safe_path.mkdir(parents=True, exist_ok=True)
            
            return {
                "success": True,
                "path": str(safe_path.relative_to(self.root_path)),
                "message": f"Directory created: {path}"
            }
            
        except Exception as e:
            return {"error": f"Create directory error: {str(e)}", "success": False}

# MCP Protocol Handler
class MCPFilesystemHandler:
    """Handle MCP protocol for filesystem server"""
    
    def __init__(self, root_path: str = "."):
        self.server = FilesystemMCPServer(root_path)
        self.tools = {
            "read_file": self.server.read_file,
            "write_file": self.server.write_file,
            "list_directory": self.server.list_directory,
            "create_directory": self.server.create_directory
        }
    
    async def handle_tool_call(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tool call via MCP protocol"""
        if tool_name not in self.tools:
            return {
                "success": False,
                "error": f"Unknown tool: {tool_name}",
                "available_tools": list(self.tools.keys())
            }
        
        try:
            result = await self.tools[tool_name](**params)
            result["tool_name"] = tool_name
            result["timestamp"] = __import__("time").time()
            return result
        except Exception as e:
            return {
                "success": False,
                "error": f"Tool execution error: {str(e)}",
                "tool_name": tool_name
            }
    
    def get_available_tools(self) -> List[str]:
        """Get list of available tools"""
        return list(self.tools.keys())

# Main entry point for MCP server
async def main():
    """Main MCP server entry point"""
    root_path = sys.argv[1] if len(sys.argv) > 1 else "."
    print(f"Starting Filesystem MCP Server with root: {root_path}")
    
    handler = MCPFilesystemHandler(root_path)
    
    # Simple JSONL protocol over stdin/stdout
    print("Available tools:", handler.get_available_tools())
    print("Ready for MCP requests...")
    
    try:
        while True:
            # Read from stdin
            line = input()
            if not line.strip():
                continue
                
            try:
                request = json.loads(line)
                tool_name = request.get("tool")
                params = request.get("params", {})
                request_id = request.get("id", "unknown")
                
                result = await handler.handle_tool_call(tool_name, params)
                result["request_id"] = request_id
                
                # Write to stdout
                print(json.dumps(result))
                
            except json.JSONDecodeError:
                print(json.dumps({"error": "Invalid JSON", "success": False}))
            except Exception as e:
                print(json.dumps({"error": str(e), "success": False}))
                
    except (EOFError, KeyboardInterrupt):
        print("MCP Filesystem Server shutting down...")

if __name__ == "__main__":
    asyncio.run(main())
