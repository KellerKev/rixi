"""Direct-mode configuration — the `[direct]` and `[template.<name>]` tables of rixi.toml.

    [direct]
    box_domain     = "run.example.com"        # boxes are https://b-<id>.run.example.com
    heartbeat_url  = "https://gw.example.com/gw/heartbeat"
    box_jwks_url   = "https://portal.example.com/.well-known/jwks.json"   # boxes verify tokens here
    rixi_ref       = "v0.2.0"                 # pinned release the boxes install (never "main")
    store          = "sqlite:///var/lib/rixi/direct.db"                   # or postgresql://…
    allowed_regions = ["fr-par-2", "pl-waw-2"]                            # hard floor, checked at load
    authorizer_url = "http://127.0.0.1:8030/internal/authorize"           # optional
    authorizer_token = "${env:RIXI_AUTHORIZER_TOKEN}"
    jwt_jwks_url   = "https://portal.example.com/.well-known/jwks.json"   # who may call the API

    [direct.limits]      # per tenant
    max_boxes = 2
    max_eur_per_hour = 5.0
    default_ttl = "2h"
    max_ttl = "12h"
    idle_timeout = "30m"

    [direct.providers.scaleway]
    secret_key = "${env:SCW_SECRET_KEY}"
    project_id = "${env:SCW_DEFAULT_PROJECT_ID}"

    [direct.dns]
    provider = "scaleway"          # or "none"
    zone = "example.com"

    [template.l4]
    provider = "scaleway"
    type = "L4-1-24G"
    zones = ["fr-par-2", "pl-waw-2"]
    image = "ubuntu_noble_gpu_os_12"
    root_volume_gb = 100
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

try:
    import tomllib
except ModuleNotFoundError:  # py3.10
    import tomli as tomllib  # type: ignore

from ..config import _duration, _subst

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Template:
    name: str
    provider: str
    instance_type: str
    zones: Tuple[str, ...]
    image: str
    root_volume_gb: Optional[int] = None
    eur_per_hour: Optional[float] = None   # overrides offerings.toml when set


@dataclass(frozen=True)
class Limits:
    max_boxes: Optional[int] = None
    max_eur_per_hour: Optional[float] = None
    default_ttl: float = 2 * 3600
    max_ttl: float = 12 * 3600
    idle_timeout: Optional[float] = 30 * 60


@dataclass(frozen=True)
class DirectConfig:
    box_domain: str
    heartbeat_url: str
    box_jwks_url: str
    rixi_ref: str
    store: str = "sqlite:///rixi-direct.db"
    rixi_repo: str = "https://github.com/KellerKev/rixi"
    bootstrap_url: Optional[str] = None
    allowed_regions: Optional[Tuple[str, ...]] = None
    authorizer_url: Optional[str] = None
    authorizer_token: Optional[str] = None
    jwt_public_key: Optional[str] = None
    jwt_jwks_url: Optional[str] = None
    tenant_claim: str = "tenant"
    roles_claim: str = "roles"
    admin_role: str = "admin"
    acme_email: Optional[str] = None
    heartbeat_timeout: float = 10 * 60
    boot_timeout: float = 25 * 60
    reap_interval: float = 30.0
    reconcile_interval: float = 300.0
    limits: Limits = field(default_factory=Limits)
    providers: Dict[str, dict] = field(default_factory=dict)
    dns: dict = field(default_factory=dict)
    templates: Dict[str, Template] = field(default_factory=dict)

    @property
    def bootstrap(self) -> str:
        if self.bootstrap_url:
            return self.bootstrap_url.replace("{ref}", self.rixi_ref)
        raw = self.rixi_repo.replace("https://github.com/", "https://raw.githubusercontent.com/")
        return f"{raw}/{self.rixi_ref}/box/bootstrap-direct.sh"

    def zones_for(self, provider: str) -> Tuple[str, ...]:
        return tuple(sorted({z for t in self.templates.values() if t.provider == provider
                             for z in t.zones}))


def _opt_float(v) -> Optional[float]:
    return float(v) if v is not None else None


def parse(data: dict) -> DirectConfig:
    d = _subst(dict(data.get("direct") or {}))
    if not d:
        raise ConfigError("no [direct] table")
    for key in ("box_domain", "heartbeat_url", "box_jwks_url", "rixi_ref"):
        if not d.get(key):
            raise ConfigError(f"[direct] needs {key}")
    if d["rixi_ref"] in ("main", "master", "HEAD") and not d.get("allow_unpinned_ref"):
        raise ConfigError("[direct] rixi_ref must be a pinned tag or commit, not a branch "
                          "(set allow_unpinned_ref = true for development)")
    lim = d.get("limits") or {}
    limits = Limits(
        max_boxes=lim.get("max_boxes"),
        max_eur_per_hour=_opt_float(lim.get("max_eur_per_hour")),
        default_ttl=_duration(lim.get("default_ttl", "2h")),
        max_ttl=_duration(lim.get("max_ttl", "12h")),
        idle_timeout=_duration(lim.get("idle_timeout", "30m")) if lim.get(
            "idle_timeout", "30m") not in (0, "0", "off") else None,
    )
    if limits.default_ttl > limits.max_ttl:
        raise ConfigError("[direct.limits] default_ttl exceeds max_ttl")
    allowed = tuple(d["allowed_regions"]) if d.get("allowed_regions") is not None else None

    templates: Dict[str, Template] = {}
    for name, body in (_subst(dict(data.get("template") or {}))).items():
        if not _NAME.match(name):
            raise ConfigError(f"template name {name!r}: use lowercase letters, digits, '-'")
        zones = tuple(body.get("zones") or ((body["zone"],) if body.get("zone") else ()))
        if not zones or not body.get("type") or not body.get("provider"):
            raise ConfigError(f"[template.{name}] needs provider, type and zones")
        if allowed is not None:
            outside = [z for z in zones if z not in allowed]
            if outside:
                raise ConfigError(f"[template.{name}] zones {outside} are outside "
                                  f"allowed_regions {list(allowed)}")
        templates[name] = Template(
            name=name, provider=str(body["provider"]), instance_type=str(body["type"]),
            zones=zones, image=str(body.get("image", "ubuntu_noble")),
            root_volume_gb=int(body["root_volume_gb"]) if body.get("root_volume_gb") else None,
            eur_per_hour=_opt_float(body.get("eur_per_hour")))
    if not templates:
        raise ConfigError("no [template.<name>] tables")

    return DirectConfig(
        box_domain=str(d["box_domain"]).strip("."), heartbeat_url=d["heartbeat_url"],
        box_jwks_url=d["box_jwks_url"], rixi_ref=str(d["rixi_ref"]),
        store=d.get("store", "sqlite:///rixi-direct.db"),
        rixi_repo=d.get("rixi_repo", "https://github.com/KellerKev/rixi"),
        bootstrap_url=d.get("bootstrap_url"), allowed_regions=allowed,
        authorizer_url=d.get("authorizer_url") or None,
        authorizer_token=d.get("authorizer_token") or None,
        jwt_public_key=d.get("jwt_public_key") or None, jwt_jwks_url=d.get("jwt_jwks_url") or None,
        tenant_claim=d.get("tenant_claim", "tenant"), roles_claim=d.get("roles_claim", "roles"),
        admin_role=d.get("admin_role", "admin"), acme_email=d.get("acme_email") or None,
        heartbeat_timeout=_duration(d.get("heartbeat_timeout", "10m")),
        boot_timeout=_duration(d.get("boot_timeout", "25m")),
        reap_interval=_duration(d.get("reap_interval", "30s")),
        reconcile_interval=_duration(d.get("reconcile_interval", "5m")),
        limits=limits, providers=dict(d.get("providers") or {}), dns=dict(d.get("dns") or {}),
        templates=templates)


def load(path) -> DirectConfig:
    return parse(tomllib.loads(Path(path).read_text()))
