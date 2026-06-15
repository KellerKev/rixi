#!/usr/bin/env python3
"""Expose rixi's MCP tools over SMCP (Secure MCP).

Wraps the existing tool handlers in mcp_servers.py (filesystem + web search) as an
SMCP capability set and serves them over a Fernet/HMAC WebSocket, so any SMCP client
(wolfgang, malgra's probe, or rixi's own agent in client mode) can securely discover
and invoke them.

Run:
    python smcp_server.py --bind 127.0.0.1:8770 --secret-key … --api-key …
    pixi run smcp-serve

Secrets may also come from the environment: RIXI_SMCP_SECRET, RIXI_SMCP_API_KEY,
RIXI_SMCP_JWT_SECRET.
"""
from __future__ import annotations

import argparse
import asyncio
import os

from mcp_servers import MCPFilesystemHandler, MCPWebSearchHandler
from smcp import SMCPToolServer

# Curated parameter schemas (JSON-Schema "properties") + descriptions for the tools.
_TOOL_META = {
    "read_file": ("Read a file within the server root",
                  {"path": {"type": "string"}, "encoding": {"type": "string"}}),
    "write_file": ("Write a file within the server root",
                   {"path": {"type": "string"}, "content": {"type": "string"}}),
    "list_directory": ("List a directory within the server root",
                       {"path": {"type": "string"}}),
    "create_directory": ("Create a directory within the server root",
                         {"path": {"type": "string"}}),
    "web_search": ("Web search across configured backends",
                   {"query": {"type": "string"}, "num_results": {"type": "integer"}}),
    "search_documents": ("Search for documents of a given type",
                         {"query": {"type": "string"}, "doc_type": {"type": "string"}}),
}


def build_tool_registry(root: str = ".", search_api_key: str = ""):
    """Build the SMCP tool registry from rixi's MCP handlers."""
    fs = MCPFilesystemHandler(root)
    ws = MCPWebSearchHandler(search_api_key or os.getenv("SEARCH_API_KEY"))
    registry = {}
    for handler in (fs, ws):
        for name in handler.get_available_tools():
            desc, params = _TOOL_META.get(name, (f"rixi tool {name}", {}))
            registry[name] = {
                "description": desc,
                "parameters": params,
                # handle_tool_call is async → the SMCP server awaits the coroutine.
                "handler": (lambda p, h=handler, n=name: h.handle_tool_call(n, p)),
            }
    return registry


def _parse_bind(bind: str):
    host, _, port = bind.partition(":")
    return host or "127.0.0.1", int(port or "8770")


async def _run(server: SMCPToolServer, host: str, port: int):
    await server.serve(host, port)
    print(f"🔐 rixi SMCP server on ws://{host}:{port} — tools: {list(server.tools)}")
    await asyncio.Future()  # run forever


def main():
    ap = argparse.ArgumentParser(description="rixi SMCP tool server")
    ap.add_argument("--bind", default="127.0.0.1:8770", help="host:port to listen on (default loopback)")
    ap.add_argument("--root", default=".", help="filesystem root exposed to read/write tools")
    ap.add_argument("--secret-key", default=os.getenv("RIXI_SMCP_SECRET", ""),
                    help="shared Fernet/HMAC secret (or RIXI_SMCP_SECRET)")
    ap.add_argument("--api-key", default=os.getenv("RIXI_SMCP_API_KEY"),
                    help="required client api_key (or RIXI_SMCP_API_KEY); omit to accept any (dev)")
    ap.add_argument("--jwt-secret",
                    default=os.getenv("RIXI_SMCP_JWT_SECRET", "rixi-smcp-default-jwt-secret-change-me"),
                    help="HS256 secret for session tokens (or RIXI_SMCP_JWT_SECRET)")
    ap.add_argument("--search-api-key", default="", help="API key for the web_search tool")
    args = ap.parse_args()

    if not args.secret_key:
        ap.error("a --secret-key (or RIXI_SMCP_SECRET) is required")

    host, port = _parse_bind(args.bind)
    registry = build_tool_registry(args.root, args.search_api_key)
    server = SMCPToolServer(
        registry,
        secret_key=args.secret_key,
        jwt_secret=args.jwt_secret,
        api_key=args.api_key,
        node_id="rixi",
    )
    try:
        asyncio.run(_run(server, host, port))
    except KeyboardInterrupt:
        print("\nSMCP server shutting down…")


if __name__ == "__main__":
    main()
