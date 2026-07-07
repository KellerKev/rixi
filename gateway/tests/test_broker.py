# Tests for brokered routing (client agent ↔ gateway ↔ server), control ops, and claims.
import asyncio
import base64
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytest.importorskip("websockets")
import websockets  # noqa: E402
from gateway.claims import Claims  # noqa: E402
from gateway.client import GatewayClient  # noqa: E402
from gateway.protocol import dec, derive_keys, enc, proof  # noqa: E402
from gateway.server import Gateway  # noqa: E402

SECRET = "gw-secret"


async def _echo(tag: bytes):
    async def handle(reader, writer):
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(tag + data)
            await writer.drain()
        writer.close()
    s = await asyncio.start_server(handle, "127.0.0.1", 0)
    return s, s.sockets[0].getsockname()[1]


async def _server_agent(ws_url, target_port, node_id):
    """A stand-in for the open `rixi-tunnel connect` (auth with node_id, role=server)."""
    key, _pk = derive_keys(SECRET)
    ws = await websockets.connect(ws_url)
    ch = dec(await ws.recv(), key)
    await ws.send(enc({"type": "auth", "proof": proof(_pk, ch["nonce"]), "node_id": node_id}, key))
    sessions = {}

    async def pump(sid, reader):
        try:
            while True:
                d = await reader.read(65536)
                if not d:
                    break
                await ws.send(enc({"type": "session_data", "sid": sid,
                                   "data": base64.b64encode(d).decode()}, key))
        finally:
            sessions.pop(sid, None)

    async def loop():
        async for raw in ws:
            o = dec(raw, key)
            if o["type"] == "session_open":
                r, w = await asyncio.open_connection("127.0.0.1", target_port)
                sessions[o["sid"]] = w
                asyncio.create_task(pump(o["sid"], r))
            elif o["type"] == "session_data":
                w = sessions.get(o["sid"])
                if w:
                    w.write(base64.b64decode(o["data"]))
                    await w.drain()
            elif o["type"] == "session_close":
                w = sessions.pop(o["sid"], None)
                if w:
                    w.close()
    return ws, asyncio.create_task(loop())


async def _start_gateway():
    gw = Gateway(SECRET, "127.0.0.1", 0)
    ws_server = await websockets.serve(gw._on_agent, "127.0.0.1", 0, ping_interval=20)
    return gw, ws_server, f"ws://127.0.0.1:{ws_server.sockets[0].getsockname()[1]}"


async def _wait(fn, timeout=8):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if fn():
            return True
        await asyncio.sleep(0.05)
    return False


def test_brokered_round_trip_through_client_agent():
    async def run():
        echo, ep = await _echo(b"S:")
        gw, ws_server, url = await _start_gateway()
        sa = ca = None
        try:
            sa = await _server_agent(url, ep, "srv-1")
            assert await _wait(lambda: gw.registry.get("srv-1") is not None)

            ca = GatewayClient(url, SECRET, "client-1")
            await ca.connect()
            assert await _wait(lambda: gw.registry.get("client-1") is not None)

            local, lport = await ca.serve_local("srv-1", "127.0.0.1", 0)
            try:
                r, w = await asyncio.open_connection("127.0.0.1", lport)
                w.write(b"hello")
                await w.drain()
                assert await asyncio.wait_for(r.read(7), timeout=5) == b"S:hello"
                w.close()
            finally:
                local.close()
                await local.wait_closed()
        finally:
            for a in (sa, ca):
                pass
            if ca and ca._loop_task:
                ca._loop_task.cancel()
            if sa:
                sa[1].cancel()
                await sa[0].close()
            ws_server.close()
            await ws_server.wait_closed()
            echo.close()
            await echo.wait_closed()

    asyncio.run(run())


def test_claim_redemption_signals_waiting_client():
    async def run():
        echo, ep = await _echo(b"P:")
        gw, ws_server, url = await _start_gateway()
        sa = None
        ca = GatewayClient(url, SECRET, "C")
        try:
            await ca.connect()
            assert await _wait(lambda: gw.registry.get("C") is not None)
            # a claim is pending for client C (as request_compute would create)
            claim = gw.claims.create("C", "dummy", {})
            # the provisioned server dials in using the token as its node_id
            sa = await _server_agent(url, ep, claim.token)
            assert await _wait(lambda: ca.ready.is_set())
            assert ca.ready_node == claim.token
            assert gw.claims.get(claim.token).status == "active"
        finally:
            if ca._loop_task:
                ca._loop_task.cancel()
            if sa:
                sa[1].cancel()
                await sa[0].close()
            ws_server.close()
            await ws_server.wait_closed()
            echo.close()
            await echo.wait_closed()

    asyncio.run(run())


def test_control_list_and_route():
    async def run():
        echo, ep = await _echo(b"Z:")
        gw, ws_server, url = await _start_gateway()
        sa = None
        ca = GatewayClient(url, SECRET, "ctl")
        try:
            sa = await _server_agent(url, ep, "node-Z")
            await ca.connect()
            assert await _wait(lambda: gw.registry.get("node-Z") is not None)
            nodes = {n["node_id"] for n in await ca.list_nodes()}
            assert "node-Z" in nodes and "ctl" in nodes
            assert (await ca._control("route", node_id="node-Z"))["exists"] is True
            assert (await ca._control("route", node_id="nope"))["exists"] is False
        finally:
            if ca._loop_task:
                ca._loop_task.cancel()
            if sa:
                sa[1].cancel()
                await sa[0].close()
            ws_server.close()
            await ws_server.wait_closed()
            echo.close()
            await echo.wait_closed()

    asyncio.run(run())


def test_claims_token_lifecycle():
    c = Claims()
    claim = c.create("client-x", "dummy", {"gpu": 1}, ttl=0.2)
    assert claim.token.startswith("rxtok_")
    # single redemption only
    assert c.redeem(claim.token, claim.token) is not None
    assert c.redeem(claim.token, claim.token) is None       # already used
    # unknown token
    assert c.redeem("rxtok_nope", "x") is None
    # expiry
    expiring = c.create("client-y", "dummy", {}, ttl=0.05)
    time.sleep(0.1)
    assert expiring.expired
    assert c.redeem(expiring.token, expiring.token) is None
