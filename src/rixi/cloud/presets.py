"""Size presets for `rixi up`: a name that means the same thing on every provider.

`sample-cpu` is a small general-purpose box for builds, tests, and light jobs; `sample-gpu` is a
single-GPU box sized for basic ML and model inference (a 24 GB L4 fits a 7B model, or a QLoRA
fine-tune of one). `--type` overrides the instance type and keeps everything else.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


class PresetError(ValueError):
    pass


SIZES = ("sample-cpu", "sample-gpu")
PROVIDERS = ("hetzner", "scaleway", "aws")

# size → provider → instance type (None = the provider has no such hardware)
_TYPES: Dict[str, Dict[str, Optional[str]]] = {
    "sample-cpu": {"hetzner": "cx23", "scaleway": "DEV1-M", "aws": "t3.medium"},
    "sample-gpu": {"hetzner": None, "scaleway": "L4-1-24G", "aws": "g6.xlarge"},
}

DEFAULT_REGION = {"hetzner": "nbg1", "scaleway": "fr-par-1", "aws": "eu-central-1"}

# Tried in order after the default region when the provider reports a stock-out.
FALLBACK_REGIONS: Dict[str, List[str]] = {
    "hetzner": ["fsn1", "hel1"],
    "scaleway": ["fr-par-2"],
    "aws": [],
}

# Instance types that need a GPU image (drivers + CUDA preinstalled).
GPU_TYPES = {"L4-1-24G", "L40S-1-48G", "H100-1-80G", "g6.xlarge", "g5.xlarge", "g4dn.xlarge"}

# Approximate list prices (EUR/hour) shown before a box is created. Informational only.
APPROX_EUR_PER_HOUR = {
    ("hetzner", "cx23"): 0.0088,
    ("scaleway", "DEV1-M"): 0.022,
    ("scaleway", "L4-1-24G"): 0.75,
    ("aws", "t3.medium"): 0.045,
    ("aws", "g6.xlarge"): 0.90,
}


@dataclass(frozen=True)
class Resolved:
    provider: str
    instance_type: str
    gpu: bool
    regions: List[str]          # default (or requested) region first, then fallbacks
    eur_per_hour: Optional[float]


def resolve(provider: str, size: str = "sample-cpu", instance_type: Optional[str] = None,
            region: Optional[str] = None) -> Resolved:
    """Turn (provider, size, optional overrides) into a concrete instance type + region list."""
    if provider not in PROVIDERS:
        raise PresetError(f"unknown provider {provider!r} (choose from: {', '.join(PROVIDERS)})")
    if size not in SIZES:
        raise PresetError(f"unknown size {size!r} (choose from: {', '.join(SIZES)})")
    itype = instance_type or _TYPES[size][provider]
    if not itype:
        raise PresetError(f"{provider} has no GPU instances — use --provider scaleway (or aws) "
                          f"for {size}")
    if region:
        regions = [region]          # an explicit region is honored exactly, no fallback
    else:
        regions = [DEFAULT_REGION[provider]] + FALLBACK_REGIONS[provider]
    gpu = size == "sample-gpu" or itype in GPU_TYPES
    return Resolved(provider, itype, gpu, regions, APPROX_EUR_PER_HOUR.get((provider, itype)))
