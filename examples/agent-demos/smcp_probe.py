#!/usr/bin/env python3
"""SMCP connectivity probe for a running rixi SMCP server.

Start the server first:
    cd ../../agent
    RIXI_SMCP_SECRET=my_secret_key_2024 RIXI_SMCP_API_KEY=demo_key_123 \
        python smcp_server.py --bind 127.0.0.1:8770

Then run this probe (needs websockets + cryptography):
    SMCP_SECRET=my_secret_key_2024 SMCP_API_KEY=demo_key_123 python smcp_probe.py

It performs handshake -> auth -> capability_discovery -> tool_invoke, mirroring how
malgra/wolfgang clients talk to any SMCP server.
"""
import asyncio
import os
import sys

# Reuse rixi's own SMCP client from the agent engine.
_AGENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "agent")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

from smcp import SMCPClient, SMCPConfig  # noqa: E402


async def main():
    cfg = SMCPConfig()
    cfg.server_url = os.getenv("SMCP_URL", "ws://127.0.0.1:8770")
    cfg.secret_key = os.getenv("SMCP_SECRET", "my_secret_key_2024")
    cfg.api_key = os.getenv("SMCP_API_KEY", "demo_key_123")
    cfg.node_id = "probe"

    client = SMCPClient(cfg)
    await client.connect()
    print("1-3. handshake + auth + discover ok; tools:", list(client.capabilities))

    # Invoke a tool the rixi SMCP server exposes (filesystem list_directory on the root).
    if "list_directory" in client.capabilities:
        result = await client.invoke_tool("list_directory", path=".")
        names = [i["name"] for i in result.get("items", [])][:5] if isinstance(result, dict) else result
        print("4. list_directory('.') →", names, "…")
    await client.disconnect()
    print("\nSMCP INTEROP OK")


if __name__ == "__main__":
    asyncio.run(main())
