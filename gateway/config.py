"""Resource catalog — declare cloud compute in simple TOML, no OpenTofu required.

A gateway-side `rixi.toml` lists named resources; each says which `provider`, the server `type`,
`region`, credentials, and a lifecycle policy (reuse-if-up vs. always-fresh, and when to tear down).
Clients ask for a resource BY NAME and the gateway translates it into the existing OpenTofu
provisioning. Experts can instead point a resource at their own module (`module = "./my-tofu"`).

    [resource.gpu-box]
    provider = "scaleway"
    type     = "L4-1-24G"     # → instance_type
    region   = "fr-par-2"
    reuse    = true           # reuse if already up · false = a fresh box per request
    teardown = "idle"         # manual | on_release | idle | ttl
    idle_timeout = "30m"
    [resource.gpu-box.credentials]
    api_key = "${env:SCW_ACCESS_KEY}"
    secret  = "${env:SCW_SECRET_KEY}"

Credentials support ${env:VAR} / ${file:/path} substitution and are mapped to the provider's own
env vars (`_CRED_ENV`) — they reach tofu via the subprocess environment and are never written to
tfvars.json. Secret tf variables (tunnel_secret, key_secret) are likewise passed as TF_VAR_* env,
not tfvars.json. NOTE: OpenTofu still records applied values in terraform.tfstate, so provisioning
workdirs are created 0700/0600; use encrypted/remote state for stronger guarantees.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

try:  # py3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py3.10 fallback
    import tomli as tomllib

from .policy import GlobalPolicy, Quotas, ResourcePolicy, RolePolicy

# Friendly TOML key → tofu variable name. Unlisted keys pass through verbatim.
_VAR_ALIASES = {"type": "instance_type"}

# Per-provider credential key → that provider's env var. Adding a provider = one entry here
# (+ a module dir under provisioning/terraform/providers/<name>).
_CRED_ENV: Dict[str, Dict[str, str]] = {
    "scaleway": {
        "api_key": "SCW_ACCESS_KEY",
        "secret": "SCW_SECRET_KEY",
        "project_id": "SCW_DEFAULT_PROJECT_ID",
        "region": "SCW_DEFAULT_ZONE",
    },
    # Hetzner Cloud tokens are project-scoped, so the token alone suffices (project_id is
    # informational and ignored). CPU instances only — no GPUs on Hetzner Cloud.
    "hetzner": {
        "api_key": "HCLOUD_TOKEN",
    },
}

# Lifecycle/meta keys — everything else at the top level is treated as a tofu variable.
_META_KEYS = {"provider", "module", "reuse", "teardown", "idle_timeout", "max_age",
              "credentials", "vars", "key_secret", "key_secret_uses",
              "policy", "jwt_public_key", "jwt_jwks_url",
              "fallback_regions", "fallback_types", "prewarm", "spot"}


@dataclass
class ResourceDef:
    name: str
    provider: str = "dummy"
    module: Optional[str] = None            # custom OpenTofu dir (expert escape hatch)
    reuse: bool = True
    teardown: str = "manual"                # manual | on_release | idle | ttl
    idle_timeout: Optional[float] = None    # seconds, for teardown="idle"
    max_age: Optional[float] = None         # seconds, for teardown="ttl"
    # End-to-end secure mode: the rixi server's handshake secret (env RIXI_KEY_SECRET). When set,
    # the provisioned box runs the rixi server with the key handshake enabled, so a client can
    # negotiate an AES-256-GCM session that passes through the gateway opaquely. Clients use the
    # same secret via `rixi_client --handshake-secret …`. key_secret_uses bounds how many
    # handshakes succeed (0 = unlimited; needed when several clients reuse one warm box).
    key_secret: Optional[str] = None
    key_secret_uses: int = 0
    # Box-side JWT (enforced when policy require_jwt resolves true): the rixi server runs with
    # --public-key / --jwks-url so it rejects unauthenticated calls. Prefer the JWKS URL.
    jwt_public_key: Optional[str] = None
    jwt_jwks_url: Optional[str] = None
    policy: Optional[ResourcePolicy] = None          # per-resource admin floor (see policy.py)
    # Capacity retry: on a capacity/stock-out apply failure, retry across these regions/types
    # (in order) before giving up. Empty → derive fallback regions from offerings.toml.
    fallback_regions: tuple = ()
    fallback_types: tuple = ()
    # Pre-warm (warm pool of 1): keep this box provisioned + ready proactively, so the first
    # request routes instantly (no cold start) and it's re-warmed if it goes down. Idle teardown
    # is skipped for pre-warmed resources.
    prewarm: bool = False
    # Spot / preemptible: request cheaper interruptible capacity. Provisioning tries spot first and
    # falls back to on-demand on a capacity error (see actions/provision.py); if the cloud preempts
    # the box, the gateway re-provisions it (see server.py). Priced at the spot rate for scheduling.
    # NOTE: no shipped provider offers spot yet (Hetzner/Scaleway have none) — the wiring is ready
    # for an AWS/GCP module; other providers accept-and-ignore the flag.
    spot: bool = False
    vars: dict = field(default_factory=dict)         # tofu variables
    credentials: dict = field(default_factory=dict)  # resolved credential values

    @property
    def node_id(self) -> str:
        """Stable tunnel node_id a reused box dials in under."""
        return f"res-{self.name}"

    def cred_env(self) -> Dict[str, str]:
        """Credentials mapped to the provider's env vars (for the tofu subprocess env)."""
        mapping = _CRED_ENV.get(self.provider, {})
        env: Dict[str, str] = {}
        for k, v in self.credentials.items():
            if v in (None, ""):
                continue
            env[mapping.get(k, k)] = str(v)
        return env


_DUR = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([smhd]?)\s*$")
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _duration(v) -> Optional[float]:
    """Parse '90s' / '30m' / '2h' / '1d' (or a bare number of seconds) → seconds."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _DUR.match(str(v))
    if not m:
        raise ValueError(f"bad duration: {v!r} (use e.g. 90s, 30m, 2h, 1d)")
    return float(m.group(1)) * _UNIT[m.group(2)]


_SUBST = re.compile(r"\$\{(env|file):([^}]+)\}")


def _subst(value):
    """Expand ${env:VAR} and ${file:/path} in strings; recurse through dicts/lists."""
    if isinstance(value, str):
        def repl(m):
            kind, arg = m.group(1), m.group(2).strip()
            if kind == "env":
                return os.environ.get(arg, "")
            return Path(arg).expanduser().read_text().strip()
        return _SUBST.sub(repl, value)
    if isinstance(value, dict):
        return {k: _subst(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v) for v in value]
    return value


def _default_teardown(reuse: bool) -> str:
    return "manual" if reuse else "on_release"


def parse_resource(name: str, body: dict, base_dir: Optional[Path] = None) -> ResourceDef:
    body = _subst(dict(body))
    reuse = bool(body.get("reuse", True))
    module = body.get("module")
    if module and base_dir is not None and not os.path.isabs(module):
        module = str((base_dir / module).resolve())
    # tofu vars: an explicit [vars] table plus any loose non-meta top-level keys (with aliases).
    tvars = dict(body.get("vars") or {})
    for k, v in body.items():
        if k in _META_KEYS:
            continue
        tvars[_VAR_ALIASES.get(k, k)] = v
    key_secret = body.get("key_secret")
    return ResourceDef(
        name=name,
        provider=str(body.get("provider", "dummy")),
        module=module,
        reuse=reuse,
        teardown=str(body.get("teardown") or _default_teardown(reuse)),
        idle_timeout=_duration(body.get("idle_timeout")),
        max_age=_duration(body.get("max_age")),
        key_secret=str(key_secret) if key_secret not in (None, "") else None,
        key_secret_uses=int(body.get("key_secret_uses", 0)),
        jwt_public_key=body.get("jwt_public_key") or None,
        jwt_jwks_url=body.get("jwt_jwks_url") or None,
        policy=parse_resource_policy(body.get("policy")),
        fallback_regions=tuple(body.get("fallback_regions") or ()),
        fallback_types=tuple(body.get("fallback_types") or ()),
        prewarm=bool(body.get("prewarm", False)),
        spot=bool(body.get("spot", False)),
        vars=tvars,
        credentials=dict(body.get("credentials") or {}),
    )


def _tup(d: dict, key) -> Optional[tuple]:
    v = d.get(key)
    return tuple(v) if v is not None else None


def _quotas(d: dict) -> Quotas:
    return Quotas(max_concurrent_resources=d.get("max_concurrent_resources"),
                  max_total_provisions=d.get("max_total_provisions"),
                  max_eur_per_hour=d.get("max_eur_per_hour"),
                  max_fleet_eur=d.get("max_fleet_eur"))


def parse_resource_policy(d) -> Optional[ResourcePolicy]:
    if not d:
        return None
    d = _subst(dict(d))
    return ResourcePolicy(
        require_e2e=bool(d.get("require_e2e", False)),
        require_jwt=bool(d.get("require_jwt", False)),
        require_tls=bool(d.get("require_tls", False)),
        allowed_roles=_tup(d, "allowed_roles"),
        allowed_identities=_tup(d, "allowed_identities"),
        max_concurrent=d.get("max_concurrent"),
        max_age=_duration(d.get("max_age")),
        allowed_regions=_tup(d, "allowed_regions"),
    )


def parse_global_policy(d) -> GlobalPolicy:
    if not d:
        return GlobalPolicy()
    d = _subst(dict(d))
    roles = {}
    for rname, rd in (d.get("roles", {}) or {}).items():
        rd = dict(rd)
        roles[rname] = RolePolicy(
            allowed_ops=tuple(rd.get("allowed_ops", ()) or ()),
            allowed_providers=_tup(rd, "allowed_providers"),
            quotas=_quotas(rd.get("quotas", {}) or {}),
        )
    return GlobalPolicy(
        require_jwt=bool(d.get("require_jwt", False)),
        require_e2e=bool(d.get("require_e2e", False)),
        require_tls=bool(d.get("require_tls", False)),
        admin_role=str(d.get("admin_role", "admin")),
        default_role=str(d.get("default_role", "user")),
        allowed_providers=_tup(d, "allowed_providers"),
        quotas=_quotas(d.get("quotas", {}) or {}),
        roles=roles,
        enabled=True,
    )


def load_catalog(path) -> Dict[str, ResourceDef]:
    """Load `[resource.<name>]` tables from a TOML file. Missing file → empty catalog."""
    p = Path(path)
    if not p.exists():
        return {}
    data = tomllib.loads(p.read_text())
    resources = data.get("resource", {}) or {}
    return {name: parse_resource(name, body, base_dir=p.parent)
            for name, body in resources.items()}


def load_policy(path) -> GlobalPolicy:
    """Load the global `[policy]` table from a TOML file. Missing → an empty (disabled) policy."""
    p = Path(path)
    if not p.exists():
        return GlobalPolicy()
    return parse_global_policy(tomllib.loads(p.read_text()).get("policy"))
