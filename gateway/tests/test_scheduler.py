"""Tests for capability-based resource selection (cheapest match)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.config import ResourceDef  # noqa: E402
from gateway.scheduler import select_resource  # noqa: E402


def _res(name, provider, itype, **v):
    return ResourceDef(name=name, provider=provider, vars={"instance_type": itype, **v})


# Priced from the committed offerings.toml: hetzner cx23 (2c/4GB, 0.0088), scaleway DEV1-S
# (2c/2GB, 0.011), scaleway L4-1-24G (L4 GPU, 48GB, 0.75), hetzner cax21 (arm 4c/8GB, 0.0168).
CATALOG = {
    "hetzner-cpu": _res("hetzner-cpu", "hetzner", "cx23"),
    "scw-cpu": _res("scw-cpu", "scaleway", "DEV1-S"),
    "gpu-box": _res("gpu-box", "scaleway", "L4-1-24G"),
    "arm-box": _res("arm-box", "hetzner", "cax21"),
}


def test_gpu_requirement_selects_gpu_box():
    assert select_resource(CATALOG, {"gpu": "L4"}) == "gpu-box"
    assert select_resource(CATALOG, {"gpu": "l4", "min_gpu_ram_gb": 24}) == "gpu-box"


def test_cheapest_match_wins():
    # both CPU boxes have >=2GB; cheapest is the hetzner one
    assert select_resource(CATALOG, {"min_ram_gb": 2}) == "hetzner-cpu"


def test_arch_and_ram_filter():
    assert select_resource(CATALOG, {"arch": "arm", "min_ram_gb": 8}) == "arm-box"


def test_no_match_returns_none():
    assert select_resource(CATALOG, {"gpu": "H100"}) is None       # no H100 in this catalog
    assert select_resource(CATALOG, {"min_ram_gb": 9999}) is None
    assert select_resource(CATALOG, {"provider": "aws"}) is None


def test_provider_and_region_filter():
    assert select_resource(CATALOG, {"provider": "hetzner", "min_ram_gb": 2}) == "hetzner-cpu"
    # cax21 (arm) is offered in fsn1/hel1/nbg1, not fr-par-2
    assert select_resource(CATALOG, {"arch": "arm", "region": "fr-par-2"}) is None
