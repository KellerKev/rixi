"""Capability-based resource selection.

Instead of asking for a box BY NAME, a client can ask by CAPABILITY (e.g. "an L4 GPU with >=24 GB
RAM") and the gateway picks the **cheapest matching** resource from its catalog, priced via
offerings.toml. This turns the named catalog into a menu the scheduler chooses from; with several
clouds in the catalog it naturally picks the cheapest provider/region for the requirement.
"""
from __future__ import annotations

from typing import Optional

from . import offerings


def matches(off: dict, req: dict) -> bool:
    """True if an offering `off` satisfies capability requirements `req`."""
    if req.get("gpu") and (off.get("gpu") or "").lower() != str(req["gpu"]).lower():
        return False
    if req.get("gpu_count") and (off.get("gpu_count") or 0) < int(req["gpu_count"]):
        return False
    if req.get("min_gpu_ram_gb") and (off.get("gpu_ram_gb") or 0) < float(req["min_gpu_ram_gb"]):
        return False
    if req.get("min_ram_gb") and (off.get("ram_gb") or 0) < float(req["min_ram_gb"]):
        return False
    if req.get("min_vcpu") and (off.get("vcpu") or 0) < int(req["min_vcpu"]):
        return False
    if req.get("arch") and (off.get("arch") or "x86") != req["arch"]:
        return False
    return True


def select_resource(catalog: dict, req: dict) -> Optional[str]:
    """Return the name of the cheapest catalog resource satisfying `req`, or None.

    `req` keys (all optional): provider, region, gpu, gpu_count, min_gpu_ram_gb, min_ram_gb,
    min_vcpu, arch. `catalog` maps name → ResourceDef (with .provider and .vars['instance_type']).
    """
    best_name, best_price = None, None
    for name, rdef in catalog.items():
        if req.get("provider") and rdef.provider != req["provider"]:
            continue
        itype = (getattr(rdef, "vars", None) or {}).get("instance_type")
        off = offerings.lookup(rdef.provider, itype)
        if off is None:
            continue
        if req.get("region"):
            regions = off.get("regions") or []
            if req["region"] not in regions:
                continue
        if not matches(off, req):
            continue
        # Price at the spot rate for spot resources, so a spot box wins on cost when it's cheaper.
        price = offerings.price_per_hour(rdef.provider, itype, spot=getattr(rdef, "spot", False))
        price = price if price is not None else float("inf")
        # Cheapest wins; ties broken by name for determinism.
        if best_price is None or price < best_price or (price == best_price and name < best_name):
            best_name, best_price = name, price
    return best_name
