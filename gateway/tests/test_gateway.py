# Tests for the multi-agent gateway: registration + per-node routing + auth.
# The test "connect" agent below speaks the same wire format as the OPEN public
# `rixi-tunnel connect`, so this also exercises interop with the unmodified primitive.
import asyncio
import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytest.importorskip("websockets")
import websockets  # noqa: E402
from gateway.protocol import dec, derive_keys, enc, proof  # noqa: E402
from gateway.server import Gateway  # noqa: E402

SECRET = "gw-secret"


async def _echo_server(tag: bytes):
    """Stand-in for a rixi server: prefixes a tag so we can tell agents apart."""
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


async def _connect_agent(ws_url: str, target_port: int, node_id: str, secret=SECRET):
    """Minimal open-tunnel `connect`: auth with node_id, bridge sessions to a local target."""
    key, _pk = derive_keys(secret)
    ws = await websockets.connect(ws_url)
    ch = dec(await ws.recv(), key)
    await ws.send(enc({"type": "auth", "proof": proof(_pk, ch["nonce"]), "node_id": node_id}, key))
    sessions: dict[str, asyncio.StreamWriter] = {}

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

    task = asyncio.create_task(loop())
    return ws, task


async def _wait(cond, timeout=8):
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if await cond():
            return True
        await asyncio.sleep(0.05)
    return False


def test_two_servers_register_and_route_independently():
    async def run():
        echo_a, pa = await _echo_server(b"A:")
        echo_b, pb = await _echo_server(b"B:")
        gw = Gateway(SECRET, "127.0.0.1", 0)
        ws_server = await websockets.serve(gw._on_agent, "127.0.0.1", 0, ping_interval=20)
        gw_port = ws_server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{gw_port}"
        agents = []
        try:
            agents.append(await _connect_agent(url, pa, "srv-A"))
            agents.append(await _connect_agent(url, pb, "srv-B"))
            # both register
            assert await _wait(lambda: asyncio.sleep(0, result=len(gw.list_nodes()) == 2))
            nodes = {n["node_id"]: n["port"] for n in gw.list_nodes()}
            assert set(nodes) == {"srv-A", "srv-B"}

            async def rt(port, msg):
                r, w = await asyncio.open_connection("127.0.0.1", port)
                w.write(msg)
                await w.drain()
                out = await asyncio.wait_for(r.read(len(msg) + 2), timeout=5)
                w.close()
                return out

            # each node's gateway port routes to ITS own server (tag proves it)
            assert await rt(nodes["srv-A"], b"ping") == b"A:ping"
            assert await rt(nodes["srv-B"], b"ping") == b"B:ping"
        finally:
            for ws, task in agents:
                task.cancel()
                await ws.close()
            ws_server.close()
            await ws_server.wait_closed()
            echo_a.close()
            echo_b.close()
            await echo_a.wait_closed()
            await echo_b.wait_closed()

    asyncio.run(run())


def test_wrong_secret_does_not_register():
    async def run():
        echo, p = await _echo_server(b"X:")
        gw = Gateway(SECRET, "127.0.0.1", 0)
        ws_server = await websockets.serve(gw._on_agent, "127.0.0.1", 0, ping_interval=20)
        gw_port = ws_server.sockets[0].getsockname()[1]
        try:
            try:
                ws, task = await _connect_agent(f"ws://127.0.0.1:{gw_port}", p, "bad", secret="WRONG")
            except Exception:
                ws = task = None
            await asyncio.sleep(0.5)
            assert gw.list_nodes() == []  # auth failed → never registered
            if task:
                task.cancel()
            if ws:
                await ws.close()
        finally:
            ws_server.close()
            await ws_server.wait_closed()
            echo.close()
            await echo.wait_closed()

    asyncio.run(run())


def test_deregister_on_disconnect():
    async def run():
        echo, p = await _echo_server(b"Y:")
        gw = Gateway(SECRET, "127.0.0.1", 0)
        ws_server = await websockets.serve(gw._on_agent, "127.0.0.1", 0, ping_interval=20)
        gw_port = ws_server.sockets[0].getsockname()[1]
        try:
            ws, task = await _connect_agent(f"ws://127.0.0.1:{gw_port}", p, "tmp")
            assert await _wait(lambda: asyncio.sleep(0, result=len(gw.list_nodes()) == 1))
            task.cancel()
            await ws.close()
            assert await _wait(lambda: asyncio.sleep(0, result=len(gw.list_nodes()) == 0))
        finally:
            ws_server.close()
            await ws_server.wait_closed()
            echo.close()
            await echo.wait_closed()

    asyncio.run(run())
