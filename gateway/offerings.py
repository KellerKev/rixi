"""Compute offerings catalog + cost lookup.

Loads `offerings.toml` (provider → instance_type → specs + list price) and answers "what does a
box of this type cost per minute?" so the gateway can estimate the cost of each provisioned
resource. Prices are static estimates (see the TOML header); unknown types return None (cost
"unknown"), never an error.

A future `refresh-offerings` job will repopulate the TOML from provider pricing APIs. For now
`python -m gateway.offerings` prints the catalog and any known rates.
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Optional

try:  # py3.11+
    import tomllib as _toml
    _READ_BINARY = True
except ModuleNotFoundError:  # py3.10
    import tomli as _toml  # type: ignore
    _READ_BINARY = True

_OFFERINGS_PATH = Path(__file__).resolve().parent / "offerings.toml"


@functools.lru_cache(maxsize=1)
def _load() -> dict:
    if not _OFFERINGS_PATH.exists():
        return {}
    with open(_OFFERINGS_PATH, "rb") as f:
        return _toml.load(f)


def currency() -> str:
    return _load().get("meta", {}).get("currency", "EUR")


def lookup(provider: Optional[str], instance_type: Optional[str]) -> Optional[dict]:
    """Return the offering dict for (provider, instance_type), or None if unknown."""
    if not provider or not instance_type:
        return None
    entry = _load().get(provider, {})
    if not isinstance(entry, dict):
        return None
    off = entry.get(instance_type)
    return off if isinstance(off, dict) else None


def price_per_hour(provider: Optional[str], instance_type: Optional[str],
                   spot: bool = False) -> Optional[float]:
    """Hourly rate for (provider, instance_type). With spot=True, prefer the type's
    `spot_eur_per_hour` when the catalog lists one, else fall back to the on-demand rate."""
    off = lookup(provider, instance_type)
    if not off:
        return None
    if spot and off.get("spot_eur_per_hour") is not None:
        return float(off["spot_eur_per_hour"])
    rate = off.get("eur_per_hour")
    return float(rate) if rate is not None else None


def price_per_min(provider: Optional[str], instance_type: Optional[str],
                  spot: bool = False) -> Optional[float]:
    hr = price_per_hour(provider, instance_type, spot=spot)
    return hr / 60.0 if hr is not None else None


def estimate_cost(provider: Optional[str], instance_type: Optional[str],
                  elapsed_min: float, spot: bool = False) -> Optional[float]:
    """Estimated cost for `elapsed_min` minutes of (provider, instance_type). None if unknown."""
    rpm = price_per_min(provider, instance_type, spot=spot)
    return round(rpm * max(0.0, elapsed_min), 6) if rpm is not None else None


def cost_fields(provider: Optional[str], instance_type: Optional[str],
                elapsed_min: float, spot: bool = False) -> dict:
    """Ready-to-log audit attrs for a resource's cost over `elapsed_min`."""
    rpm = price_per_min(provider, instance_type, spot=spot)
    return {
        "provider": provider,
        "instance_type": instance_type,
        "spot": bool(spot),
        "elapsed_min": round(elapsed_min, 3),
        "rate_per_min": rpm,
        "currency": currency(),
        "est_cost": estimate_cost(provider, instance_type, elapsed_min, spot=spot),
    }


# ── auto-refresh from provider pricing APIs ────────────────────────────────
def fetch_hetzner(token: str) -> dict:
    """Live Hetzner Cloud server types → offering entries, from the hcloud API (CPU only)."""
    import requests
    r = requests.get("https://api.hetzner.cloud/v1/server_types?per_page=50",
                     headers={"Authorization": "Bearer %s" % token}, timeout=20)
    r.raise_for_status()
    out = {}
    for s in r.json().get("server_types", []):
        if s.get("deprecated"):
            continue
        prices = s.get("prices", [])
        if not prices:
            continue
        # cheapest hourly (net) across locations + the locations offered
        rate = min(float(p["price_hourly"]["net"]) for p in prices)
        regions = sorted({p["location"] for p in prices})
        entry = {"vcpu": s["cores"], "ram_gb": int(s["memory"]),
                 "eur_per_hour": round(rate, 5), "regions": regions}
        if s.get("architecture") == "arm":
            entry["arch"] = "arm"
        out[s["name"]] = entry
    return out


def fetch_scaleway(secret_key: str, zones=("fr-par-1", "fr-par-2", "nl-ams-1")) -> dict:
    """Live Scaleway instance types → offering entries, from the products API (CPU + GPU)."""
    import requests
    merged: dict = {}
    for zone in zones:
        try:
            r = requests.get(
                "https://api.scaleway.com/instance/v1/zones/%s/products/servers" % zone,
                headers={"X-Auth-Token": secret_key}, timeout=20)
            if r.status_code != 200:
                continue
            for name, s in r.json().get("servers", {}).items():
                rate = float(s.get("hourly_price", 0.0) or 0.0)
                entry = merged.setdefault(name, {
                    "vcpu": s.get("ncpus"), "ram_gb": round((s.get("ram", 0) or 0) / 1e9),
                    "eur_per_hour": round(rate, 5), "regions": []})
                gpu = s.get("gpu")
                if gpu:
                    entry["gpu_count"] = gpu
                entry["regions"] = sorted(set(entry["regions"]) | {zone})
        except Exception:
            continue
    return merged


_REFRESHERS = {
    "hetzner": ("HCLOUD_TOKEN", fetch_hetzner),
    "scaleway": ("SCW_SECRET_KEY", fetch_scaleway),
}


def refresh(providers=None, write: bool = True, path: Optional[Path] = None) -> dict:
    """Rebuild the offerings catalog from provider pricing APIs.

    For each provider whose credential env var is set, fetch live specs + prices and replace that
    provider's block; providers without credentials keep their existing (hand-maintained) entries.
    Returns the new catalog dict; writes offerings.toml when `write` is True."""
    import os
    from datetime import datetime, timezone

    _load.cache_clear()
    catalog = dict(_load())
    targets = providers or list(_REFRESHERS)
    refreshed = []
    for prov in targets:
        env_var, fn = _REFRESHERS[prov]
        cred = os.environ.get(env_var)
        if not cred:
            continue
        entries = fn(cred)
        if entries:
            catalog[prov] = entries
            refreshed.append(prov)
    meta = dict(catalog.get("meta", {}))
    meta["currency"] = meta.get("currency", "EUR")
    meta["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    meta["note"] = ("auto-refreshed from provider pricing APIs (%s); other providers are static"
                    % ", ".join(refreshed) if refreshed else meta.get("note", ""))
    catalog["meta"] = meta
    if write:
        (path or _OFFERINGS_PATH).write_text(_dump_toml(catalog))
    _load.cache_clear()
    return catalog


def _toml_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_val(x) for x in v) + "]"
    return '"%s"' % str(v).replace('"', '\\"')


def _dump_toml(catalog: dict) -> str:
    lines = ["# Compute offerings catalog — per-provider instance specs + list price (EUR/hour).",
             "# Auto-generated by `python -m gateway.offerings --refresh`; edit by hand for",
             "# providers without a pricing-API refresher.", ""]
    meta = catalog.get("meta", {})
    lines.append("[meta]")
    for k in ("currency", "updated", "note"):
        if k in meta:
            lines.append("%s = %s" % (k, _toml_val(meta[k])))
    lines.append("")
    for prov, types in catalog.items():
        if prov == "meta" or not isinstance(types, dict):
            continue
        for tname, spec in types.items():
            # Quote instance-type keys that aren't bare TOML identifiers.
            key = tname if tname.replace("-", "").replace("_", "").isalnum() and \
                not tname[0].isdigit() else '"%s"' % tname
            lines.append("[%s.%s]" % (prov, key))
            for sk, sv in spec.items():
                lines.append("%s = %s" % (sk, _toml_val(sv)))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":  # pragma: no cover
    import sys
    if "--refresh" in sys.argv:
        cat = refresh()
        print("refreshed:", [p for p in cat if p != "meta"])
    data = _load()
    cur = currency()
    for provider, types in data.items():
        if provider == "meta" or not isinstance(types, dict):
            continue
        print(f"{provider}:")
        for t, spec in types.items():
            rpm = price_per_min(provider, t)
            rate = f"{rpm*60:.4f} {cur}/hr" if rpm is not None else "price unknown"
            gpu = f" gpu={spec.get('gpu')}x{spec.get('gpu_count', 1)}" if spec.get("gpu") else ""
            print(f"  {t:16} {spec.get('vcpu','?')}vcpu/{spec.get('ram_gb','?')}GB{gpu}  {rate}")
