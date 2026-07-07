# mcp_servers.py - MCP tool servers (filesystem + web search)
#
# Each server is launched as its own subprocess by the MCP manager:
#   python -m mcp_servers filesystem [root_path]
#   python -m mcp_servers web_search
# and speaks a simple JSONL protocol over stdin/stdout.
import json
import os
import pathlib
import sys
import asyncio
import time
from typing import Dict, Any, List
import requests
from urllib.parse import urlencode

# ───────────────────────── Filesystem server ──────────────────────────────
class FilesystemMCPServer:
    """Real filesystem MCP server implementation"""

    MAX_READ_BYTES = 10 * 1024 * 1024  # 10 MB

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

            file_size = safe_path.stat().st_size
            if file_size > self.MAX_READ_BYTES:
                return {
                    "error": f"File too large: {path} is {file_size} bytes (limit {self.MAX_READ_BYTES} bytes)",
                    "success": False
                }

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
            result["timestamp"] = time.time()
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

# ───────────────────────── Web search server ──────────────────────────────
class WebSearchMCPServer:
    """Real web search MCP server implementation"""

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv("SEARCH_API_KEY")

    async def web_search(self, query: str, num_results: int = 5) -> Dict[str, Any]:
        """Perform web search using multiple search engines"""
        try:
            # Try multiple search approaches for robustness
            results = []

            # Method 1: DuckDuckGo (no API key required)
            ddg_results = await self._duckduckgo_search(query, num_results)
            if ddg_results:
                results.extend(ddg_results)

            # Method 2: Searx (if available)
            if len(results) < num_results:
                searx_results = await self._searx_search(query, num_results - len(results))
                if searx_results:
                    results.extend(searx_results)

            # Method 3: Google Custom Search (if API key available)
            if self.api_key and len(results) < num_results:
                google_results = await self._google_search(query, num_results - len(results))
                if google_results:
                    results.extend(google_results)

            # Method 4: Bing Search (fallback)
            if len(results) < num_results:
                bing_results = await self._bing_search(query, num_results - len(results))
                if bing_results:
                    results.extend(bing_results)

            if results:
                summary = self._create_search_summary(results, query)
                return {
                    "success": True,
                    "query": query,
                    "results": results[:num_results],
                    "summary": summary,
                    "total_found": len(results),
                    "search_method": "real_web_search"
                }
            else:
                # No backend produced results - report an honest failure
                return await self._fallback_simulated_search(query)

        except Exception as e:
            print(f"Web search error: {e}")
            return await self._fallback_simulated_search(query)

    async def _duckduckgo_search(self, query: str, num_results: int) -> List[Dict[str, Any]]:
        """Search using DuckDuckGo (no API key required)"""
        try:
            # Simple approach using DuckDuckGo instant answers
            params = urlencode({"q": query, "format": "json", "no_html": 1, "skip_disambig": 1})
            url = f"https://api.duckduckgo.com/?{params}"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()
                results = []

                # Extract abstract
                if data.get("Abstract"):
                    results.append({
                        "title": data.get("Heading", "DuckDuckGo Summary"),
                        "snippet": data["Abstract"],
                        "url": data.get("AbstractURL", ""),
                        "source": "DuckDuckGo"
                    })

                # Extract related topics
                for topic in data.get("RelatedTopics", [])[:num_results-1]:
                    if isinstance(topic, dict) and topic.get("Text"):
                        results.append({
                            "title": topic.get("FirstURL", "").split("/")[-1].replace("_", " "),
                            "snippet": topic["Text"],
                            "url": topic.get("FirstURL", ""),
                            "source": "DuckDuckGo"
                        })

                return results
        except Exception as e:
            print(f"DuckDuckGo search failed: {e}")
            return []

    async def _searx_search(self, query: str, num_results: int) -> List[Dict[str, Any]]:
        """Search using public Searx instance"""
        try:
            # Use public Searx instance
            searx_url = "https://searx.org/search"
            params = {
                "q": query,
                "format": "json",
                "engines": "google,bing,duckduckgo"
            }

            response = requests.get(searx_url, params=params, timeout=10)

            if response.status_code == 200:
                data = response.json()
                results = []

                for item in data.get("results", [])[:num_results]:
                    results.append({
                        "title": item.get("title", ""),
                        "snippet": item.get("content", ""),
                        "url": item.get("url", ""),
                        "source": "Searx"
                    })

                return results
        except Exception as e:
            print(f"Searx search failed: {e}")
            return []

    async def _google_search(self, query: str, num_results: int) -> List[Dict[str, Any]]:
        """Search using Google Custom Search API (requires API key)"""
        if not self.api_key:
            return []

        try:
            url = "https://www.googleapis.com/customsearch/v1"
            params = {
                "key": self.api_key,
                "cx": os.getenv("GOOGLE_CSE_ID"),  # Custom Search Engine ID
                "q": query,
                "num": num_results
            }

            response = requests.get(url, params=params, timeout=10)

            if response.status_code == 200:
                data = response.json()
                results = []

                for item in data.get("items", []):
                    results.append({
                        "title": item.get("title", ""),
                        "snippet": item.get("snippet", ""),
                        "url": item.get("link", ""),
                        "source": "Google"
                    })

                return results
        except Exception as e:
            print(f"Google search failed: {e}")
            return []

    async def _bing_search(self, query: str, num_results: int) -> List[Dict[str, Any]]:
        """Search using Bing (requires a real API key; no scraping fallback)"""
        # Without a Bing API key there is no way to return real results,
        # so report none rather than fabricating them.
        return []

    def _create_search_summary(self, results: List[Dict[str, Any]], query: str) -> str:
        """Create a summary of search results"""
        if not results:
            return f"No results found for '{query}'"

        summaries = []
        for result in results[:3]:  # Use top 3 results for summary
            snippet = result.get("snippet", "")
            if snippet:
                summaries.append(snippet[:100] + "..." if len(snippet) > 100 else snippet)

        return f"Search for '{query}' found {len(results)} results. " + " ".join(summaries)

    async def _fallback_simulated_search(self, query: str) -> Dict[str, Any]:
        """Honest failure when no real search backend is available"""
        return {
            "success": False,
            "query": query,
            "results": [],
            "error": "no search backend configured/available",
            "search_method": "none"
        }

    async def search_documents(self, query: str, doc_type: str = "pdf") -> Dict[str, Any]:
        """Search for specific document types"""
        enhanced_query = f"{query} filetype:{doc_type}"
        return await self.web_search(enhanced_query, 3)

class MCPWebSearchHandler:
    """Handle MCP protocol for web search server"""

    def __init__(self, api_key: str = None):
        self.server = WebSearchMCPServer(api_key)
        self.tools = {
            "web_search": self.server.web_search,
            "search_documents": self.server.search_documents
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
            result["timestamp"] = time.time()
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

# ───────────────────────── JSONL serve loop + dispatch ─────────────────────
async def _serve(handler, banner: str):
    """Simple JSONL protocol over stdin/stdout, shared by both servers."""
    print(banner)
    print("Available tools:", handler.get_available_tools())
    print("Ready for MCP requests...")

    try:
        while True:
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

                print(json.dumps(result))

            except json.JSONDecodeError:
                print(json.dumps({"error": "Invalid JSON", "success": False}))
            except Exception as e:
                print(json.dumps({"error": str(e), "success": False}))

    except (EOFError, KeyboardInterrupt):
        print("MCP server shutting down...")

async def main():
    """Dispatch to the requested server: `python -m mcp_servers <filesystem|web_search> [args]`."""
    which = sys.argv[1] if len(sys.argv) > 1 else ""

    if which == "filesystem":
        root_path = sys.argv[2] if len(sys.argv) > 2 else "."
        handler = MCPFilesystemHandler(root_path)
        await _serve(handler, f"Starting Filesystem MCP Server with root: {root_path}")
    elif which == "web_search":
        api_key = os.getenv("SEARCH_API_KEY")
        handler = MCPWebSearchHandler(api_key)
        await _serve(handler, f"Starting Web Search MCP Server (API key: {'configured' if api_key else 'not configured'})")
    else:
        print("Usage: python -m mcp_servers <filesystem [root_path] | web_search>", file=sys.stderr)
        sys.exit(2)

if __name__ == "__main__":
    asyncio.run(main())
