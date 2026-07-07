# Headline end-to-end: client request_compute → REAL `tofu apply` (dummy provider) brings up a box
# that dials the gateway with the one-time token → gateway redeems the claim + bridges → client
# reaches its server through the brokered path → release runs `tofu destroy`.
#
# Skips cleanly when no `tofu`/`terraform` binary is on PATH.
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytest.importorskip("websockets")
import websockets  # noqa: E402
from gateway.client import GatewayClient  # noqa: E402
from gateway.provisioning.tofu import find_tofu  # noqa: E402
from gateway.server import Gateway  # noqa: E402

SECRET = "gw-e2e-secret"
RIXI_DIR = os.getenv("RIXI_DIR", "/Users/kevin/DevelopmentStudio/rixi")


@pytest.mark.skipif(find_tofu() is None, reason="needs `tofu`/`terraform` on PATH")
def test_provision_dummy_end_to_end():
    if not os.path.exists(os.path.join(RIXI_DIR, "tunnel", "rixi_tunnel.py")):
        pytest.skip("open rixi checkout not found (set RIXI_DIR)")

    async def run():
        gw = Gateway(SECRET, "127.0.0.1", 0)
        ws_server = await websockets.serve(gw._on_agent, "127.0.0.1", 0, ping_interval=20)
        gw_port = ws_server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{gw_port}"
        gw.public_ws_url = url  # the dummy box dials this back

        ca = GatewayClient(url, SECRET, "client-E")
        token = None
        try:
            await ca.connect()
            r = await ca.request_compute(
                "dummy", {"rixi_dir": RIXI_DIR, "python_bin": sys.executable})
            token = r["token"]

            # the gateway fires `tofu apply`; the dummy box dials in with the token; wait for it.
            node = await ca.wait_ready(timeout=180)
            assert node == token
            assert gw.claims.get(token).status == "active"

            # reach the provisioned box through the brokered path
            local, lport = await ca.serve_local(node, "127.0.0.1", 0)
            try:
                rd, wr = await asyncio.open_connection("127.0.0.1", lport)
                wr.write(b"orchestrated")
                await wr.drain()
                out = await asyncio.wait_for(rd.read(len(b"DUMMY:orchestrated")), timeout=10)
                assert out == b"DUMMY:orchestrated"
                wr.close()
            finally:
                local.close()
                await local.wait_closed()
        finally:
            if token:
                try:
                    await ca.release(token)            # → tofu destroy
                    await asyncio.sleep(1)
                except Exception:
                    pass
            if ca._loop_task:
                ca._loop_task.cancel()
            ws_server.close()
            await ws_server.wait_closed()

    asyncio.run(run())
