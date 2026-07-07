"""Tests for spot / preemptible support: spot-aware pricing, spot→on-demand candidate ordering,
cheapest-wins scheduling at the spot rate, and the provision-time fallback to on-demand."""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway import offerings, scheduler  # noqa: E402
from gateway.actions import provision as prov  # noqa: E402
from gateway.config import ResourceDef, parse_resource  # noqa: E402
from gateway.provisioning.tofu import TofuRunner  # noqa: E402

# A synthetic catalog: one type priced with an explicit spot rate, one without.
_SYNTH = {
    "meta": {"currency": "EUR"},
    "acme": {
        "big": {"vcpu": 8, "ram_gb": 32, "eur_per_hour": 1.00, "spot_eur_per_hour": 0.30,
                "regions": ["r1", "r2"]},
        "plain": {"vcpu": 2, "ram_gb": 8, "eur_per_hour": 0.20, "regions": ["r1"]},
    },
}


@pytest.fixture()
def synth(monkeypatch):
    monkeypatch.setattr(offerings, "_load", lambda: _SYNTH)


# ── config parsing ───────────────────────────────────────────────────────────
def test_parse_spot_flag():
    assert parse_resource("x", {"provider": "acme", "spot": True}).spot is True
    assert parse_resource("y", {"provider": "acme"}).spot is False
    # spot is a meta key, so it must NOT leak into the tofu vars
    assert "spot" not in parse_resource("x", {"provider": "acme", "spot": True}).vars


# ── offerings pricing ────────────────────────────────────────────────────────
def test_price_prefers_spot_rate_when_present(synth):
    assert offerings.price_per_hour("acme", "big") == pytest.approx(1.00)
    assert offerings.price_per_hour("acme", "big", spot=True) == pytest.approx(0.30)


def test_price_falls_back_to_on_demand_without_spot_rate(synth):
    # 'plain' has no spot rate → spot=True still returns the on-demand price, never None/error
    assert offerings.price_per_hour("acme", "plain", spot=True) == pytest.approx(0.20)


def test_cost_fields_carry_spot(synth):
    f = offerings.cost_fields("acme", "big", 60, spot=True)
    assert f["spot"] is True
    assert f["est_cost"] == pytest.approx(0.30)


# ── candidate ordering ───────────────────────────────────────────────────────
def test_candidates_spot_first_then_on_demand():
    rdef = ResourceDef(name="s", provider="acme", spot=True)
    cands = prov._candidates(rdef, "big", "r1", spot=True)
    assert cands[0] == ("big", "r1", True)              # spot attempted first
    assert cands[-1] == ("big", "r1", False)            # on-demand is the last resort
    assert any(s for *_, s in cands) and any(not s for *_, s in cands)


def test_candidates_no_spot_stays_on_demand():
    rdef = ResourceDef(name="s", provider="acme")
    cands = prov._candidates(rdef, "big", "r1", spot=False)
    assert all(s is False for *_, s in cands)


# ── scheduler prices spot resources at the spot rate ─────────────────────────
def test_scheduler_prefers_cheaper_spot(synth):
    catalog = {
        "ondemand": ResourceDef(name="ondemand", provider="acme", vars={"instance_type": "big"}),
        "spotbox": ResourceDef(name="spotbox", provider="acme", vars={"instance_type": "big"},
                               spot=True),
    }
    # both are the same type, but the spot box is priced at 0.30 vs 1.00 → it wins
    assert scheduler.select_resource(catalog, {"min_vcpu": 4}) == "spotbox"


# ── provision-time spot → on-demand fallback ─────────────────────────────────
class _GW:
    secret = "s"
    kdf_salt = ""
    public_ws_url = "ws://gw:7100"
    ws_host = "gw"
    ws_port = 7100

    def __init__(self):
        self.audits = []

    async def _audit(self, event, actor, action, **kw):
        self.audits.append((event, kw.get("target"), kw.get("reason")))


def test_spot_capacity_exhausted_falls_back_to_on_demand(monkeypatch, tmp_path):
    monkeypatch.setenv("RIXI_STATE_DIR", str(tmp_path))
    rdef = ResourceDef(name="s", provider="dummy", spot=True,
                       vars={"instance_type": "big", "region": "r1"})
    seen = []

    async def apply_impl(self, variables, secret_vars=None):
        seen.append(variables.get("spot", False))
        if variables.get("spot"):                       # every spot attempt is "out of capacity"
            raise RuntimeError("Error: insufficient spot capacity available")
        return {"ok": True}                             # on-demand succeeds

    monkeypatch.setattr(TofuRunner, "apply", apply_impl)
    gw = _GW()
    result = asyncio.run(prov.provision({"resource": rdef}, gw))
    assert result["node_id"] == "res-s"
    assert True in seen and seen[-1] is False           # tried spot, then landed on-demand
    assert any(e == "spot.fallback" and t == "s" for e, t, _ in gw.audits)


def test_on_demand_attempt_omits_spot_var(monkeypatch, tmp_path):
    """The successful on-demand attempt must NOT pass spot to the module (custom modules may not
    declare it; built-ins default to false)."""
    monkeypatch.setenv("RIXI_STATE_DIR", str(tmp_path))
    rdef = ResourceDef(name="s", provider="dummy", spot=True,
                       vars={"instance_type": "big", "region": "r1"})
    last_vars = {}

    async def apply_impl(self, variables, secret_vars=None):
        last_vars.clear()
        last_vars.update(variables)
        if variables.get("spot"):
            raise RuntimeError("no capacity")
        return {"ok": True}

    monkeypatch.setattr(TofuRunner, "apply", apply_impl)
    asyncio.run(prov.provision({"resource": rdef}, _GW()))
    assert "spot" not in last_vars                      # omitted on the on-demand attempt
