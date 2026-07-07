"""Tests for warm pools (pre-warming reusable boxes)."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.config import ResourceDef  # noqa: E402
from gateway.server import Gateway  # noqa: E402

SECRET = "s3cr3t"


def _gw(catalog):
    gw = Gateway(SECRET, "127.0.0.1", 0, reap_interval=999.0)
    gw.catalog = catalog
    return gw


def test_prewarm_all_provisions_only_prewarm_resources():
    catalog = {
        "warm": ResourceDef(name="warm", provider="dummy", reuse=True, prewarm=True),
        "cold": ResourceDef(name="cold", provider="dummy", reuse=True, prewarm=False),
    }
    gw = _gw(catalog)
    called = []

    async def fake_provision(rdef, security=None):
        called.append(rdef.name)
        gw._res_state(rdef.name)["status"] = "active"

    gw._provision_resource = fake_provision

    async def run():
        gw._prewarm_all()
        await asyncio.sleep(0.05)   # let the spawned task run

    asyncio.run(run())
    assert called == ["warm"]        # only the prewarm resource was provisioned proactively


def test_reaper_skips_idle_teardown_for_prewarm():
    rdef = ResourceDef(name="warm", provider="dummy", reuse=True, prewarm=True,
                       teardown="idle", idle_timeout=1.0)
    gw = _gw({"warm": rdef})
    st = gw._res_state("warm")
    st.update(status="active", workdir="/tmp/x", created_at=time.time() - 100,
              last_active=time.time() - 100)   # long idle → would be torn down if not prewarm
    torn = []
    gw._teardown_resource = lambda name, actor="system": torn.append(name)

    async def fake_provision(rdef, security=None):
        pass
    gw._provision_resource = fake_provision

    asyncio.run(gw._reap_once())
    assert torn == []                # prewarm resource is kept warm, not idle-reaped


def test_reaper_rewarms_a_downed_prewarm_resource():
    rdef = ResourceDef(name="warm", provider="dummy", reuse=True, prewarm=True)
    gw = _gw({"warm": rdef})
    rewarmed = []

    async def fake_provision(rdef, security=None):
        rewarmed.append(rdef.name)
    gw._provision_resource = fake_provision

    async def run():
        # not in registry (down) → reaper should re-warm it
        await gw._reap_once()
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert rewarmed == ["warm"]
