"""Tests for capacity retry (fallback across region/type on stock-outs)."""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.actions import provision as prov  # noqa: E402
from gateway.config import ResourceDef  # noqa: E402
from gateway.provisioning.tofu import TofuRunner  # noqa: E402


class _GW:
    secret = "s"
    kdf_salt = ""
    public_ws_url = "ws://gw:7100"
    ws_host = "gw"
    ws_port = 7100


def test_is_capacity_error():
    assert prov._is_capacity_error("Error: no servers available (out of stock)")
    assert prov._is_capacity_error("resource_exhausted: capacity")
    assert not prov._is_capacity_error("Error: invalid credentials")


def test_candidates_primary_then_fallbacks():
    rdef = ResourceDef(name="gpu", provider="scaleway",
                       fallback_regions=("fr-par-2",), fallback_types=("L40S-1-48G",))
    cands = prov._candidates(rdef, "L4-1-24G", "fr-par-1")
    assert cands[0] == ("L4-1-24G", "fr-par-1", False)
    assert ("L4-1-24G", "fr-par-2", False) in cands
    assert ("L40S-1-48G", "fr-par-1", False) in cands


def test_candidates_derives_fallback_regions_from_offerings():
    # No explicit fallback_regions → derive from offerings.toml (L4 is offered in fr-par-1 + fr-par-2)
    rdef = ResourceDef(name="gpu", provider="scaleway")
    regions = [r for _, r, _ in prov._candidates(rdef, "L4-1-24G", "fr-par-1")]
    assert "fr-par-2" in regions


def _provision(rdef, monkeypatch, tmp_path, apply_impl):
    monkeypatch.setenv("RIXI_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(TofuRunner, "apply", apply_impl)
    return asyncio.run(prov.provision({"resource": rdef}, _GW()))


def test_retry_on_capacity_then_succeeds(monkeypatch, tmp_path):
    rdef = ResourceDef(name="gpu", provider="scaleway",
                       vars={"instance_type": "L4-1-24G", "region": "fr-par-1"},
                       fallback_regions=("fr-par-2",))
    attempts = []

    async def apply_impl(self, variables, secret_vars=None):
        attempts.append((variables.get("instance_type"), variables.get("region")))
        if len(attempts) == 1:
            raise RuntimeError("Error: no servers available (stock exhausted)")
        return {"public_ip": "1.2.3.4"}

    result = _provision(rdef, monkeypatch, tmp_path, apply_impl)
    assert result["node_id"] == "res-gpu"
    assert attempts == [("L4-1-24G", "fr-par-1"), ("L4-1-24G", "fr-par-2")]


def test_non_capacity_error_fails_fast(monkeypatch, tmp_path):
    rdef = ResourceDef(name="gpu", provider="scaleway",
                       vars={"instance_type": "L4-1-24G", "region": "fr-par-1"},
                       fallback_regions=("fr-par-2",))
    attempts = []

    async def apply_impl(self, variables, secret_vars=None):
        attempts.append(1)
        raise RuntimeError("Error: invalid credentials")

    with pytest.raises(RuntimeError, match="credentials"):
        _provision(rdef, monkeypatch, tmp_path, apply_impl)
    assert len(attempts) == 1   # did not retry a non-capacity error
