"""Cloud-agnostic provisioning actions, driven by OpenTofu/Terraform.

`provision` renders a tfvars carrying the gateway URL + a node_id + the tunnel secret and runs
`tofu apply` on a provider module (provisioning/terraform/providers/<provider>) or a custom module.
The box installs the rixi server + tunnel agent and dials the gateway under that node_id.

Two paths:
  • resource-driven (args["resource"] is a ResourceDef): the catalog entry chooses the module,
    the tofu vars, the credentials (→ subprocess env), and — when `reuse` — a STABLE workdir
    (~/.rixi/resources/<name>) + node_id (res-<name>) so `apply` is idempotent (reuse if already up).
  • legacy spec (args["provider"] + args["spec"] + args["claim"]): a fresh tempdir + the one-time
    token as node_id (the dummy e2e path).
`deprovision` runs `tofu destroy` in the resource's / claim's workdir.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import register_action
from .. import audit as audit_mod
from .. import offerings
from ..provisioning.tofu import TofuRunner

_PROVIDERS = Path(__file__).resolve().parent.parent / "provisioning" / "terraform" / "providers"

# tofu/provider error text that means "no capacity here, try elsewhere" (vs. a config/auth error).
_CAPACITY_RE = re.compile(
    r"stock|exhausted|no server|not available|unavailable|capacity|resource_exhausted|quota|"
    r"insufficient|out of|no capacity|resourcenotfound", re.I)


def _is_capacity_error(text: str) -> bool:
    return bool(_CAPACITY_RE.search(text or ""))


def _candidates(rdef, base_type, base_region, spot=False):
    """Ordered (instance_type, region, spot) triples to try: primary first, then fallbacks. Fallback
    regions default to the other regions offerings.toml lists for the type. For a spot request, try
    all spot candidates first, then one on-demand attempt on the primary (type, region) so a spot
    stock-out still lands a box."""
    types = [base_type] + [t for t in (getattr(rdef, "fallback_types", ()) or ()) if t != base_type]
    fb_regions = list(getattr(rdef, "fallback_regions", ()) or ())
    if not fb_regions and rdef is not None:
        off = offerings.lookup(rdef.provider, base_type) or {}
        fb_regions = [r for r in (off.get("regions") or []) if r != base_region]
    regions = [base_region] + [r for r in fb_regions if r != base_region]
    out = []
    for t in (types or [None]):
        for r in (regions or [None]):
            out.append((t, r, bool(spot)))
    if spot:
        out.append((base_type, base_region, False))  # on-demand fallback once spot is exhausted
    return out


def _module(provider: str) -> Path:
    return _PROVIDERS / provider


def _resource_workdir(name: str) -> Path:
    d = Path(os.getenv("RIXI_STATE_DIR") or (Path.home() / ".rixi")) / "resources" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _default_rixi_dir() -> str:
    # env override, else the repo root (gateway/ lives inside the rixi checkout)
    return os.getenv("RIXI_DIR") or str(Path(__file__).resolve().parents[2])


def _gateway_url(gateway) -> str:
    if getattr(gateway, "public_ws_url", None):
        return gateway.public_ws_url
    host = "127.0.0.1" if gateway.ws_host in ("0.0.0.0", "") else gateway.ws_host
    return f"ws://{host}:{gateway.ws_port}"


def _dummy_defaults(tvars: dict) -> dict:
    tvars.setdefault("python_bin", sys.executable)
    tvars.setdefault("rixi_dir", _default_rixi_dir())
    return tvars


def _provider_vars(provider: str, spec: dict) -> dict:
    if provider == "dummy":
        return _dummy_defaults({"python_bin": spec.get("python_bin"),
                                "rixi_dir": spec.get("rixi_dir")})
    out = {}
    for k in ("server_port", "rixi_ref", "instance_type", "region"):
        if spec.get(k) is not None:
            out[k] = spec[k]
    return out


@register_action("provision")
async def provision(args: dict, gateway: Any):
    rdef = args.get("resource")
    claim = args.get("claim")

    # Secret variables never touch tfvars.json — passed to tofu as TF_VAR_* env (see tofu.py).
    secret_vars: dict = {}
    if rdef is not None:
        provider = rdef.provider
        module = Path(rdef.module) if rdef.module else _module(provider)
        tvars = dict(rdef.vars)
        if provider == "dummy":
            _dummy_defaults(tvars)
        if rdef.key_secret:   # secure mode: box runs the rixi server with the AES key handshake on
            secret_vars["key_secret"] = rdef.key_secret
            tvars["key_secret_uses"] = rdef.key_secret_uses
        if rdef.jwt_jwks_url:   # box runs rixi_server --jwks-url → rejects unauthenticated calls
            tvars["jwt_jwks_url"] = rdef.jwt_jwks_url
        if rdef.jwt_public_key:
            tvars["jwt_public_key"] = rdef.jwt_public_key
        env = rdef.cred_env() or None
        if rdef.reuse:
            node_id = rdef.node_id
            workdir = str(_resource_workdir(rdef.name))
        else:
            node_id = claim.token if claim is not None else rdef.node_id
            workdir = tempfile.mkdtemp(prefix=f"rixi-tofu-{provider}-")
        gw_url = _gateway_url(gateway)
    else:
        provider = str(args.get("provider", "dummy"))
        spec = args.get("spec", {}) or {}
        module = _module(provider)
        tvars = _provider_vars(provider, spec)
        env = None
        node_id = claim.token
        workdir = tempfile.mkdtemp(prefix=f"rixi-tofu-{provider}-")
        gw_url = spec.get("gateway_ws_url") or _gateway_url(gateway)

    if not module.exists():
        raise RuntimeError(f"unknown provider/module: {provider} ({module})")
    if claim is not None:
        claim.workdir = workdir
        claim.node_id = node_id

    variables = {
        "gateway_ws_url": gw_url,
        "node_id": node_id,
        **{k: v for k, v in tvars.items() if v is not None},
    }
    # The tunnel secret is sensitive — pass it via TF_VAR env, not tfvars.json.
    secret_vars["tunnel_secret"] = gateway.secret
    # per-deployment KDF salt must match the gateway, or the box's tunnel derives different keys
    # and can't authenticate (v2 tunnel crypto). Only pass a non-default salt.
    if getattr(gateway, "kdf_salt", ""):
        variables["kdf_salt"] = gateway.kdf_salt

    # Spot / preemptible: try spot candidates first, then fall back to on-demand (see _candidates).
    # Only inject the `spot` var when actually requesting it, so non-spot resources + custom modules
    # (which may not declare it) are unaffected.
    if rdef is not None:
        spot = bool(getattr(rdef, "spot", False))
    else:
        spot = bool((args.get("spec") or {}).get("spot", False))
    res_name = rdef.name if rdef is not None else provider

    # Capacity retry: try the primary (instance_type, region), then declared/derived fallbacks,
    # moving on only for capacity/stock-out failures (config/auth errors fail fast).
    runner = TofuRunner(module, workdir, env=env)
    last_err = None
    for itype, region, use_spot in _candidates(rdef, variables.get("instance_type"),
                                               variables.get("region"), spot):
        attempt = dict(variables)
        if itype is not None:
            attempt["instance_type"] = itype
        if region is not None:
            attempt["region"] = region
        if use_spot:
            attempt["spot"] = True
        else:
            attempt.pop("spot", None)   # rely on the module default (false)
        try:
            outputs = await runner.apply(attempt, secret_vars=secret_vars)
            if spot and not use_spot:    # asked for spot, landed on-demand — record the fallback
                print(f"ℹ️  spot capacity exhausted for {res_name}; provisioned on-demand", flush=True)
                await gateway._audit(audit_mod.SPOT_FALLBACK, "gateway", "provision",
                                     target=res_name, decision="allow", reason="spot_exhausted")
            return {"provider": provider, "workdir": workdir, "node_id": node_id, "outputs": outputs}
        except RuntimeError as e:
            if not _is_capacity_error(str(e)):
                raise
            last_err = e
            print(f"⚠️  {'spot ' if use_spot else ''}capacity unavailable for "
                  f"{itype or provider}/{region or '-'}; trying next…", flush=True)
    raise last_err or RuntimeError("provisioning failed with no candidates")


@register_action("deprovision")
async def deprovision(args: dict, gateway: Any):
    rdef = args.get("resource")
    claim = args.get("claim")
    # Required secret vars (tunnel_secret, key_secret) are passed via env, not tfvars.json, so
    # `destroy` must be given them too — OpenTofu demands values for required variables even on
    # destroy. Without this, teardown fails with "No value for required variable".
    if rdef is not None:
        workdir = args.get("workdir")
        if not workdir:
            return {"destroyed": False}
        module = Path(rdef.module) if rdef.module else _module(rdef.provider)
        secret_vars = {"tunnel_secret": gateway.secret}
        if rdef.key_secret:
            secret_vars["key_secret"] = rdef.key_secret
        await TofuRunner(module, workdir, env=rdef.cred_env() or None).destroy(secret_vars=secret_vars)
        return {"destroyed": True}
    if not getattr(claim, "workdir", None):
        return {"destroyed": False}
    await TofuRunner(_module(claim.provider), claim.workdir).destroy(
        secret_vars={"tunnel_secret": gateway.secret})
    return {"destroyed": True}
