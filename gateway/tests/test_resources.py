# Behaviour tests for named resources (config.py + server.py) driven by the dummy provider
# (real `tofu apply`, offline). Each test does ~1 apply; skips cleanly without tofu / the open rixi.
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytest.importorskip("websockets")
import websockets  # noqa: E402
from gateway.config import ResourceDef  # noqa: E402
from gateway.client import GatewayClient  # noqa: E402
from gateway.provisioning.tofu import find_tofu  # noqa: E402
from gateway.server import Gateway  # noqa: E402

SECRET = "gw-res-secret"
RIXI_DIR = os.getenv("RIXI_DIR", "/Users/kevin/DevelopmentStudio/rixi")
_DUMMY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "provisioning", "terraform", "providers", "dummy")

needs_tofu = pytest.mark.skipif(find_tofu() is None, reason="needs `tofu`/`terraform` on PATH")


def _have_rixi():
    return os.path.exists(os.path.join(RIXI_DIR, "tunnel", "rixi_tunnel.py"))


async def _wait(fn, timeout=15):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if fn():
            return True
        await asyncio.sleep(0.05)
    return False


async def _start(catalog, reap_interval=999.0):
    gw = Gateway(SECRET, "127.0.0.1", 0, reap_interval=reap_interval)
    gw.catalog = catalog
    ws_server = await websockets.serve(gw._on_agent, "127.0.0.1", 0, ping_interval=20)
    url = f"ws://127.0.0.1:{ws_server.sockets[0].getsockname()[1]}"
    gw.public_ws_url = url  # the dummy box dials this back
    return gw, ws_server, url


async def _roundtrip(ca, node):
    """Reach the dummy echo box through the brokered path; assert the DUMMY: prefix."""
    local, lport = await ca.serve_local(node, "127.0.0.1", 0)
    try:
        rd, wr = await asyncio.open_connection("127.0.0.1", lport)
        wr.write(b"hi")
        await wr.drain()
        out = await asyncio.wait_for(rd.read(len(b"DUMMY:hi")), timeout=10)
        assert out == b"DUMMY:hi"
        wr.close()
    finally:
        local.close()
        await local.wait_closed()


@needs_tofu
def test_reuse_resource_provisions_then_reuses(tmp_path, monkeypatch):
    if not _have_rixi():
        pytest.skip("open rixi checkout not found (set RIXI_DIR)")
    monkeypatch.setenv("RIXI_STATE_DIR", str(tmp_path))

    async def run():
        cat = {"gpu": ResourceDef(name="gpu", provider="dummy", reuse=True, teardown="manual")}
        gw, ws_server, url = await _start(cat)
        ca = GatewayClient(url, SECRET, "client-R")
        ca2 = GatewayClient(url, SECRET, "client-R2")
        try:
            await ca.connect()
            r1 = await ca.request_resource("gpu")
            assert r1["reused"] is False and r1["node_id"] == "res-gpu"
            node = await ca.wait_ready(timeout=180)
            assert node == "res-gpu"
            assert gw.registry.get("res-gpu") is not None
            await _roundtrip(ca, node)

            # second client, while it's up → reused, no new provision (registry short-circuit)
            await ca2.connect()
            r2 = await ca2.request_resource("gpu")
            assert r2["reused"] is True
            assert await ca2.wait_ready(timeout=10) == "res-gpu"

            # explicit teardown → tofu destroy → the box disconnects
            await ca.teardown("gpu")
            assert await _wait(lambda: gw.registry.get("res-gpu") is None, timeout=15)
        finally:
            await _cleanup(gw, "gpu")
            for c in (ca, ca2):
                if c._loop_task:
                    c._loop_task.cancel()
            ws_server.close()
            await ws_server.wait_closed()

    asyncio.run(run())


@needs_tofu
def test_fresh_resource_uses_one_time_token(tmp_path):
    if not _have_rixi():
        pytest.skip("open rixi checkout not found (set RIXI_DIR)")

    async def run():
        cat = {"scratch": ResourceDef(name="scratch", provider="dummy", reuse=False,
                                      teardown="on_release")}
        gw, ws_server, url = await _start(cat)
        ca = GatewayClient(url, SECRET, "client-F")
        token = None
        try:
            await ca.connect()
            r = await ca.request_resource("scratch")
            assert r["reused"] is False
            token = r["token"]
            node = await ca.wait_ready(timeout=180)
            assert node == token                 # fresh box dials in under the one-time token
            assert gw.registry.get(token) is not None
            await _roundtrip(ca, node)
        finally:
            if token:
                try:
                    await ca.release(token)      # → tofu destroy
                    await asyncio.sleep(1)
                except Exception:
                    pass
            if ca._loop_task:
                ca._loop_task.cancel()
            ws_server.close()
            await ws_server.wait_closed()

    asyncio.run(run())


@needs_tofu
def test_custom_module_path_is_used(tmp_path, monkeypatch):
    if not _have_rixi():
        pytest.skip("open rixi checkout not found (set RIXI_DIR)")
    monkeypatch.setenv("RIXI_STATE_DIR", str(tmp_path))

    async def run():
        cat = {"byo": ResourceDef(name="byo", provider="dummy", module=_DUMMY, reuse=True)}
        gw, ws_server, url = await _start(cat)
        ca = GatewayClient(url, SECRET, "client-B")
        try:
            await ca.connect()
            await ca.request_resource("byo")
            assert await ca.wait_ready(timeout=180) == "res-byo"
            assert gw.registry.get("res-byo") is not None
        finally:
            await _cleanup(gw, "byo")
            if ca._loop_task:
                ca._loop_task.cancel()
            ws_server.close()
            await ws_server.wait_closed()

    asyncio.run(run())


@needs_tofu
def test_idle_reaper_tears_down(tmp_path, monkeypatch):
    if not _have_rixi():
        pytest.skip("open rixi checkout not found (set RIXI_DIR)")
    monkeypatch.setenv("RIXI_STATE_DIR", str(tmp_path))

    async def run():
        cat = {"eph": ResourceDef(name="eph", provider="dummy", reuse=True,
                                  teardown="idle", idle_timeout=0.1)}
        gw, ws_server, url = await _start(cat)
        ca = GatewayClient(url, SECRET, "client-I")
        try:
            await ca.connect()
            await ca.request_resource("eph")
            assert await ca.wait_ready(timeout=180) == "res-eph"
            assert gw.registry.get("res-eph") is not None
            # idle (no routes) past the timeout → a reaper pass destroys it
            await asyncio.sleep(0.3)
            await gw._reap_once()
            assert await _wait(lambda: gw.registry.get("res-eph") is None, timeout=15)
        finally:
            await _cleanup(gw, "eph")
            if ca._loop_task:
                ca._loop_task.cancel()
            ws_server.close()
            await ws_server.wait_closed()

    asyncio.run(run())


async def _cleanup(gw, name):
    try:
        await gw._teardown_resource(name)   # idempotent; kills any leftover dummy processes
        await asyncio.sleep(0.5)
    except Exception:
        pass
