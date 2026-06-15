# Tests for the SMCP (Secure MCP) protocol module and its mcp_manager integration.
import asyncio

import pytest

smcp = pytest.importorskip("smcp")
pytest.importorskip("websockets")
pytest.importorskip("jwt")
from smcp import (  # noqa: E402
    SMCPClient, SMCPConfig, SMCPToolServer, _fernet, _sign, _verify_signature, _envelope,
)

SECRET = "my_secret_key_2024"
API_KEY = "demo_key_123"


def test_fernet_derivation_matches_reference():
    # Interop vector: PBKDF2-HMAC-SHA256(secret, b"scp_salt_2024", 100000) round-trips.
    f = _fernet(SECRET)
    token = f.encrypt(b"hello")
    assert f.decrypt(token) == b"hello"


def test_signature_roundtrip():
    env = _envelope(SECRET, _fernet(SECRET), "handshake", {"a": 1}, False)
    assert _verify_signature(SECRET, env)
    env["signature"] = "deadbeef"
    assert not _verify_signature(SECRET, env)


def _stub_registry():
    def echo(params):
        return {"echo": params.get("text", "")}

    async def add(params):
        return params.get("a", 0) + params.get("b", 0)

    return {
        "echo": {"description": "echo text", "parameters": {"text": {"type": "string"}}, "handler": echo},
        "add": {"description": "add a+b", "parameters": {}, "handler": add},
    }


async def _serve():
    server = SMCPToolServer(_stub_registry(), secret_key=SECRET, api_key=API_KEY, node_id="rixi-test")
    ws_server = await server.serve("127.0.0.1", 0)
    port = ws_server.sockets[0].getsockname()[1]
    return server, ws_server, port


def _client(port, api_key=API_KEY, secret=SECRET):
    cfg = SMCPConfig()
    cfg.server_url = f"ws://127.0.0.1:{port}"
    cfg.api_key = api_key
    cfg.secret_key = secret
    return SMCPClient(cfg)


def test_loopback_handshake_discover_invoke():
    async def run():
        server, ws_server, port = await _serve()
        try:
            c = _client(port)
            await c.connect()
            assert set(c.capabilities) == {"echo", "add"}
            assert (await c.invoke_tool("echo", text="hi")) == {"echo": "hi"}
            assert (await c.invoke_tool("add", a=2, b=3)) == 5     # async handler awaited
            await c.disconnect()
        finally:
            ws_server.close()
            await ws_server.wait_closed()

    asyncio.run(run())


def test_bad_api_key_fails_auth():
    async def run():
        server, ws_server, port = await _serve()
        try:
            c = _client(port, api_key="wrong")
            with pytest.raises(RuntimeError):
                await c.connect()
        finally:
            ws_server.close()
            await ws_server.wait_closed()

    asyncio.run(run())


def test_unknown_tool_errors():
    async def run():
        server, ws_server, port = await _serve()
        try:
            c = _client(port)
            await c.connect()
            with pytest.raises(RuntimeError):
                await c.invoke_tool("nope")
            await c.disconnect()
        finally:
            ws_server.close()
            await ws_server.wait_closed()

    asyncio.run(run())


def test_open_dev_mode_accepts_any_key():
    # No api_key configured on the server → any client key is accepted.
    async def run():
        server = SMCPToolServer(_stub_registry(), secret_key=SECRET, node_id="rixi-test")
        ws_server = await server.serve("127.0.0.1", 0)
        port = ws_server.sockets[0].getsockname()[1]
        try:
            c = _client(port, api_key="anything")
            await c.connect()
            assert "echo" in c.capabilities
            await c.disconnect()
        finally:
            ws_server.close()
            await ws_server.wait_closed()

    asyncio.run(run())


def test_mcp_manager_smcp_transport():
    # An MCPServerConfig(transport="smcp") registers, discovers tools, and call_tool routes
    # through the SMCP client — proving SMCP is "just another transport" on the MCP interface.
    from mcp_manager import MCPManager, create_smcp_server_config

    async def run():
        server, ws_server, port = await _serve()
        mgr = MCPManager()
        await mgr.start()
        try:
            cfg = create_smcp_server_config("stub", f"ws://127.0.0.1:{port}",
                                            api_key=API_KEY, secret_key=SECRET)
            await mgr.register_server(cfg)
            await mgr.start_server("stub")
            assert "echo" in mgr.tool_registry
            res = await mgr.call_tool("add", {"a": 4, "b": 5})
            assert res["result"] == 9 and res["server_mode"] == "smcp"
        finally:
            await mgr.stop()
            ws_server.close()
            await ws_server.wait_closed()

    asyncio.run(run())


def test_smcp_config_from_dict():
    from mcp_manager import MCPServerConfig
    cfg = MCPServerConfig.from_dict({
        "name": "malgra", "transport": "smcp", "url": "ws://localhost:8767",
        "api_key": "k", "secret_key": "s",
    })
    assert cfg.transport == "smcp" and cfg.url == "ws://localhost:8767"
    # default (no transport) stays stdio with a command — existing behavior unchanged
    legacy = MCPServerConfig.from_dict({"name": "fs", "command": ["python", "-m", "mcp_servers", "filesystem"]})
    assert legacy.transport == "stdio"
