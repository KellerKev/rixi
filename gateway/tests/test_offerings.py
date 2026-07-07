"""Tests for the offerings catalog, cost estimation, and the Hetzner credential mapping."""
import pytest

from gateway import offerings
from gateway.config import _CRED_ENV, ResourceDef

try:
    import tomllib as _toml
except ModuleNotFoundError:
    import tomli as _toml


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def test_fetch_hetzner_builds_entries(monkeypatch):
    import requests
    payload = {"server_types": [
        {"name": "cx23", "cores": 2, "memory": 4, "architecture": "x86", "deprecated": False,
         "prices": [{"location": "nbg1", "price_hourly": {"net": "0.0088"}},
                    {"location": "ash", "price_hourly": {"net": "0.028"}}]},
        {"name": "cax11", "cores": 2, "memory": 4, "architecture": "arm", "deprecated": False,
         "prices": [{"location": "fsn1", "price_hourly": {"net": "0.0096"}}]},
        {"name": "old", "cores": 1, "memory": 1, "deprecated": True, "prices": []},
    ]}
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(payload))
    out = offerings.fetch_hetzner("tok")
    assert "old" not in out                          # deprecated skipped
    assert out["cx23"]["eur_per_hour"] == 0.0088     # cheapest (EU) across locations
    assert out["cx23"]["regions"] == ["ash", "nbg1"]
    assert out["cax11"]["arch"] == "arm"


def test_dump_toml_round_trips():
    catalog = {
        "meta": {"currency": "EUR", "updated": "2026-07", "note": "x"},
        "hetzner": {"cx23": {"vcpu": 2, "ram_gb": 4, "eur_per_hour": 0.0088,
                             "regions": ["nbg1", "fsn1"]}},
        "scaleway": {"L4-1-24G": {"gpu": "L4", "gpu_count": 1, "eur_per_hour": 0.75}},
    }
    text = offerings._dump_toml(catalog)
    parsed = _toml.loads(text)
    assert parsed["hetzner"]["cx23"]["eur_per_hour"] == 0.0088
    assert parsed["hetzner"]["cx23"]["regions"] == ["nbg1", "fsn1"]
    assert parsed["scaleway"]["L4-1-24G"]["gpu"] == "L4"


def test_refresh_skips_provider_without_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("HCLOUD_TOKEN", raising=False)
    monkeypatch.delenv("SCW_SECRET_KEY", raising=False)
    out = tmp_path / "off.toml"
    # No creds → no provider refreshed, but the existing static catalog is preserved + rewritten.
    cat = offerings.refresh(providers=["hetzner"], write=True, path=out)
    assert "scaleway" in cat and out.exists()


def test_price_lookup_known_and_unknown():
    assert offerings.price_per_min("hetzner", "cx23") is not None
    assert offerings.price_per_min("hetzner", "cx23") == pytest.approx(0.0088 / 60)
    assert offerings.price_per_min("scaleway", "L4-1-24G") == pytest.approx(0.75 / 60)
    # unknown provider/type → None, never an error
    assert offerings.price_per_min("aws", "p4d.24xlarge") is None
    assert offerings.price_per_min("hetzner", "nope") is None
    assert offerings.price_per_min(None, None) is None


def test_estimate_and_cost_fields():
    assert offerings.estimate_cost("hetzner", "cx23", 60) == pytest.approx(0.0088)
    fields = offerings.cost_fields("scaleway", "L4-1-24G", 30)
    assert fields["provider"] == "scaleway"
    assert fields["instance_type"] == "L4-1-24G"
    assert fields["elapsed_min"] == 30
    assert fields["est_cost"] == pytest.approx(0.375)
    assert fields["currency"] == "EUR"
    # unknown type → est_cost None but still structured
    unknown = offerings.cost_fields("aws", "x", 10)
    assert unknown["est_cost"] is None and unknown["rate_per_min"] is None


def test_hetzner_cred_env_mapping():
    # Adding a cloud = one _CRED_ENV entry: Hetzner maps api_key -> HCLOUD_TOKEN.
    assert _CRED_ENV["hetzner"] == {"api_key": "HCLOUD_TOKEN"}
    rdef = ResourceDef(name="h", provider="hetzner", credentials={"api_key": "tok123"})
    assert rdef.cred_env() == {"HCLOUD_TOKEN": "tok123"}
