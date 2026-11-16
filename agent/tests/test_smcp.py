# Tests for the SMCP (Secure MCP) protocol module and its mcp_manager integration.
import asyncio

import pytest

smcp = pytest.importorskip("smcp")
pytest.importorskip("websockets")
pytest.importorskip("jwt")
from smcp import (  # noqa: E402
    SMCPClient, SMCPConfig, SMCPToolServer, _derive_keys, _sign, _canonical, _verify_signature, _envelope,
)

SECRET = "my_secret_key_2024"
API_KEY = "demo_key_123"


def test_v3_key_derivation_round_trips():
    f, _mac = _derive_keys(SECRET)
    token = f.encrypt(b"hello")
    assert f.decrypt(token) == b"hello"


def test_v3_interop_vector_matches_rust():
    # Same secret+salt MUST yield the same signature as malgra-tunnel (crates/malgra-tunnel).
    _f, mac = _derive_keys("interop-secret-value-32-bytes-xx", "interop-salt")
    sig = _sign(mac, "fixedid", "auth", "1700000000.0", _canonical({"x": 1}))
    assert sig == "661c3041e7cc52690927113ca9bc39c08899613f94e7d770addecd349374f8ca"


def test_signature_roundtrip():
    f, mac = _derive_keys(SECRET)
    env = _envelope(mac, f, "handshake", {"a": 1}, False)
    assert _verify_signature(mac, env)
    env["signature"] = "deadbeef"
    assert not _verify_signature(mac, env)


def test_server_refuses_open_without_optin():
    # No api_key and no allow_open → must refuse to construct (no silent open server).
    with pytest.raises(RuntimeError):
        SMCPToolServer(_stub_registry(), secret_key=SECRET, node_id="rixi-test")


def test_jwt_secret_independent_when_absent():
    # No jwt_secret given → a random per-process secret, INDEPENDENT of the
    # channel secret (which every client holds and could otherwise forge with).
    from smcp import _derive_jwt_secret
    s1 = SMCPToolServer(_stub_registry(), secret_key=SECRET, api_key=API_KEY, node_id="rixi-test")
    s2 = SMCPToolServer(_stub_registry(), secret_key=SECRET, api_key=API_KEY, node_id="rixi-test")
    assert s1.jwt_secret and s2.jwt_secret
    assert s1.jwt_secret != _derive_jwt_secret(SECRET)  # not channel-derived
    assert s1.jwt_secret != s2.jwt_secret               # random per instance


def test_forged_token_from_channel_secret_rejected():
    # A client holding only the channel secret must not be able to forge a
    # session token by recomputing the old channel-derived signing key.
    import time
    import jwt as _jwt
    from smcp import _derive_jwt_secret
    s = SMCPToolServer(_stub_registry(), secret_key=SECRET, api_key=API_KEY, node_id="rixi-test")
    forged = _jwt.encode(
        {"client_id": "attacker", "permissions": ["tool_invoke", "discovery"],
         "exp": int(time.time()) + 300},
        _derive_jwt_secret(SECRET),  # what any channel holder could recompute
        algorithm="HS256",
    )
    assert s._valid_token(forged) is False


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
    # No api_key configured + allow_open=True → any client key is accepted.
    async def run():
        server = SMCPToolServer(_stub_registry(), secret_key=SECRET, node_id="rixi-test",
                                allow_open=True)
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


# ── a message is taken once, while it is fresh ───────────────────────────────

def _replay_server():
    import smcp as rixi_smcp
    calls = []
    tools = {"write_file": {"description": "", "parameters": {},
                            "handler": lambda p: calls.append(p) or "written"}}
    secret = "integration-secret-value-32-bytes-x"
    server = rixi_smcp.SMCPToolServer(tools, secret_key=secret, api_key="k")
    fernet, mac = rixi_smcp._derive_keys(secret, "")
    return rixi_smcp, server, fernet, mac, calls


def test_a_recorded_tool_call_cannot_be_sent_again():
    import copy
    import time
    rixi_smcp, server, fernet, mac, calls = _replay_server()
    token = server._issue_token("agent")
    env = rixi_smcp._envelope(mac, fernet, "tool_invoke", {
        "token": token, "tool_name": "write_file", "parameters": {"path": "a", "content": "b"}}, True)

    async def run():
        first = await server.handle_message(copy.deepcopy(env))
        assert first["type"] == "tool_response" and len(calls) == 1
        for _ in range(3):                                  # the same frame, on new connections
            again = await server.handle_message(copy.deepcopy(env))
            assert again["type"] == "error"
            assert "already received" in rixi_smcp._decrypt(fernet, again)["error"]
        assert len(calls) == 1
        # one stamped long ago (validly signed then) is refused however new its id
        old = rixi_smcp._envelope(mac, fernet, "tool_invoke", {
            "token": token, "tool_name": "write_file", "parameters": {}}, True)
        old["timestamp"] = float(int(time.time()) - 10 * 365 * 86400)
        old["signature"] = rixi_smcp._sign(mac, old["id"], old["type"],
                                           f"{int(old['timestamp'])}.0",
                                           rixi_smcp._canonical(old["payload"]))
        stale = await server.handle_message(old)
        assert stale["type"] == "error" and "not fresh" in rixi_smcp._decrypt(fernet, stale)["error"]
        assert len(calls) == 1
    asyncio.run(run())


def test_the_client_refuses_a_reply_played_back_or_meant_for_another_call():
    import json
    rixi_smcp, server, fernet, mac, _ = _replay_server()

    class FakeWs:
        def __init__(self, replies):
            self.replies = replies

        async def send(self, raw):
            pass

        async def recv(self):
            return json.dumps(self.replies.pop(0))
    cfg = rixi_smcp.SMCPConfig()
    client = rixi_smcp.SMCPClient(cfg)
    client._fernet, client._mac_key, client._token = fernet, mac, "t"
    reply = server._reply("tool_response", {"tool_name": "read_file", "result": "old content",
                                            "status": "success"})

    async def run():
        client._ws = FakeWs([reply, reply])
        assert await client.invoke_tool("read_file", path="x") == "old content"
        with pytest.raises(RuntimeError, match="already received"):
            await client.invoke_tool("read_file", path="x")           # the same reply again
        other = server._reply("tool_response", {"tool_name": "list_directory", "result": [],
                                                "status": "success"})
        client._ws = FakeWs([other])
        with pytest.raises(RuntimeError, match="not 'read_file'"):
            await client.invoke_tool("read_file", path="x")
        wrong_kind = server._reply("capability_discovery", {"capabilities": {}})
        client._ws = FakeWs([wrong_kind])
        with pytest.raises(RuntimeError, match="expected a tool_response"):
            await client.invoke_tool("read_file", path="x")
    asyncio.run(run())
