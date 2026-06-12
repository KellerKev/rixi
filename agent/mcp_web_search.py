# mcp_web_search.py - Real web search MCP server implementation
import json
import os
import sys
import asyncio
import time
from typing import Dict, Any, List
import requests
from urllib.parse import urlencode, quote_plus

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
            # Note: You'd need to set up Google Custom Search API
            # This is a placeholder for the implementation
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

# MCP Protocol Handler
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

# Main entry point for MCP server
async def main():
    """Main MCP server entry point"""
    api_key = os.getenv("SEARCH_API_KEY")
    print(f"Starting Web Search MCP Server (API key: {'configured' if api_key else 'not configured'})")
    
    handler = MCPWebSearchHandler(api_key)
    
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
        print("MCP Web Search Server shutting down...")

if __name__ == "__main__":
    asyncio.run(main())
