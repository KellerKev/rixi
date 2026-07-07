# Enforcement tests — policy gates control ops; require_e2e/jwt force the box's secure config.
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytest.importorskip("websockets")
import websockets  # noqa: E402
from gateway.client import GatewayClient  # noqa: E402
from gateway.config import ResourceDef  # noqa: E402
from gateway.policy import GlobalPolicy, PolicyEngine, ResourcePolicy, RolePolicy  # noqa: E402
from gateway.provisioning.tofu import find_tofu  # noqa: E402
from gateway.server import Gateway  # noqa: E402

SECRET = "gw-enf-secret"
RIXI_DIR = os.getenv("RIXI_DIR", "/Users/kevin/DevelopmentStudio/rixi")
needs_tofu = pytest.mark.skipif(find_tofu() is None, reason="needs tofu")


def _glob():
    # anonymous clients resolve to role 'user'; user may request_compute + route.
    return GlobalPolicy(enabled=True, default_role="user",
                        roles={"user": RolePolicy(allowed_ops=("request_compute", "route",
                                                               "list", "list_resources"))})


async def _start(catalog, kdf_salt=""):
    gw = Gateway(SECRET, "127.0.0.1", 0, reap_interval=999.0, kdf_salt=kdf_salt)
    gw.catalog = catalog
    gw.policy = PolicyEngine(_glob(), catalog)
    ws = await websockets.serve(gw._on_agent, "127.0.0.1", 0, ping_interval=20)
    url = f"ws://127.0.0.1:{ws.sockets[0].getsockname()[1]}"
    gw.public_ws_url = url
    return gw, ws, url


def test_require_e2e_without_key_secret_is_denied():
    async def run():
        cat = {"sec": ResourceDef(name="sec", provider="dummy", reuse=True,
                                  policy=ResourcePolicy(require_e2e=True))}
        gw, ws, url = await _start(cat)
        ca = GatewayClient(url, SECRET, "c")
        try:
            await ca.connect()
            with pytest.raises(RuntimeError) as ei:
                await ca.request_resource("sec")
            assert "end-to-end" in str(ei.value) and "key_secret" in str(ei.value)
        finally:
            if ca._loop_task:
                ca._loop_task.cancel()
            ws.close()
            await ws.wait_closed()
    asyncio.run(run())


def test_rbac_role_denied():
    async def run():
        cat = {"mlonly": ResourceDef(name="mlonly", provider="dummy", reuse=True,
                                     policy=ResourcePolicy(allowed_roles=("ml",)))}
        gw, ws, url = await _start(cat)
        ca = GatewayClient(url, SECRET, "c")
        try:
            await ca.connect()
            with pytest.raises(RuntimeError) as ei:
                await ca.request_resource("mlonly")    # client is role 'user', resource wants 'ml'
            assert "not allowed for role 'user'" in str(ei.value)
        finally:
            if ca._loop_task:
                ca._loop_task.cancel()
            ws.close()
            await ws.wait_closed()
    asyncio.run(run())


def _tfvars(state_dir, name):
    with open(os.path.join(state_dir, "resources", name, "terraform.tfvars.json")) as f:
        return json.load(f)


@needs_tofu
def test_require_e2e_forces_key_secret_into_tofu(tmp_path, monkeypatch):
    if not os.path.exists(os.path.join(RIXI_DIR, "tunnel", "rixi_tunnel.py")):
        pytest.skip("open rixi checkout not found (set RIXI_DIR)")
    monkeypatch.setenv("RIXI_STATE_DIR", str(tmp_path))

    async def run():
        cat = {"e2e": ResourceDef(name="e2e", provider="dummy", reuse=True, key_secret="hs-123",
                                  jwt_jwks_url="https://idp.example/jwks",
                                  policy=ResourcePolicy(require_e2e=True, require_jwt=True))}
        gw, ws, url = await _start(cat, kdf_salt="deploy-A")  # gateway + box + client share the salt
        ca = GatewayClient(url, SECRET, "c", kdf_salt="deploy-A")
        try:
            await ca.connect()
            r = await ca.request_resource("e2e")
            assert r["reused"] is False
            assert await ca.wait_ready(timeout=180) == "res-e2e"
            tv = _tfvars(str(tmp_path), "e2e")
            # Secrets are passed to tofu via TF_VAR_* env and must NOT be written to tfvars.json.
            assert "key_secret" not in tv                     # secret kept off disk (env-only)
            assert "tunnel_secret" not in tv                  # secret kept off disk (env-only)
            # Non-secret config IS forced through tfvars (require_e2e/require_jwt effects).
            assert tv["jwt_jwks_url"] == "https://idp.example/jwks"   # jwt forced
            assert tv["kdf_salt"] == "deploy-A"               # salt reaches the box (or auth fails)
        finally:
            try:
                await gw._teardown_resource("e2e")
                await asyncio.sleep(0.5)
            except Exception:
                pass
            if ca._loop_task:
                ca._loop_task.cancel()
            ws.close()
            await ws.wait_closed()
    asyncio.run(run())
