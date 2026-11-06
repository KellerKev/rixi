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
    jwt_audience   = "rixi-gateway"           # API tokens must carry this aud; box tokens never do

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

    [direct.storage]               # optional: S3 buckets behind the proxy (gateway.direct.s3proxy)
    endpoint_url = "https://s3.example.com"           # where clients and boxes reach the proxy
    upstream     = "https://s3.{region}.scw.cloud"    # the real object storage, per region
    regions      = ["fr-par"]
    access_key   = "${env:RIXI_S3_ACCESS_KEY}"        # the operator's key; never leaves the proxy
    secret_key   = "${env:RIXI_S3_SECRET_KEY}"
    key_secret   = "${env:RIXI_S3_KEY_SECRET}"        # Fernet key: customer secrets at rest
    share_url    = "https://files.example.com/s"      # optional: share links; route /s/ here

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
    # What the machine has (overrides offerings.toml), for "the cheapest box with at least …"
    vcpu: Optional[int] = None
    ram_gb: Optional[float] = None
    gpu: Optional[str] = None
    gpu_ram_gb: Optional[float] = None


@dataclass(frozen=True)
class Limits:
    max_boxes: Optional[int] = None
    max_eur_per_hour: Optional[float] = None
    default_ttl: float = 2 * 3600
    max_ttl: float = 12 * 3600
    idle_timeout: Optional[float] = 30 * 60
    max_volumes: int = 10                  # persistent volumes per tenant
    max_volume_gb: int = 1000              # size of one volume
    max_stopped: float = 7 * 24 * 3600     # a stopped box is released after this long
    max_buckets: int = 20                  # S3 buckets per tenant
    max_storage_gb: float = 1000.0         # what a tenant's buckets may hold in all
    max_s3_keys: int = 20                  # S3 access keys per tenant (box keys not counted)


@dataclass(frozen=True)
class StorageConfig:
    """S3 buckets for tenants, on one operator account: the proxy checks each request against
    the tenant's own key, keeps it inside the tenant's buckets, and signs it again upstream."""
    endpoint_url: str
    upstream: str
    regions: Tuple[str, ...]
    access_key: str
    secret_key: str
    key_secret: str
    bucket_prefix: str = "rixi-"
    # Scaleway Object Storage, catalog 2026-11: Standard (multi-AZ), One Zone IA, egress per GB
    eur_per_gb_hour: float = 0.000022
    eur_per_gb_hour_onezone: float = 0.000011
    eur_per_gb_egress: float = 0.01
    scan_interval: float = 3600.0          # how often each bucket's size is measured
    abort_multipart_days: int = 7          # unfinished uploads are dropped after this long
    cors_origins: Tuple[str, ...] = ()     # web pages that may call the proxy (a browser console)
    share_url: str = ""                    # where the gateway's GET /s/<token> is reached; "" = no links


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
    jwt_audience: Optional[str] = None     # required `aud` on API tokens (see parse())
    tenant_claim: str = "tenant"
    roles_claim: str = "roles"
    admin_role: str = "admin"
    acme_email: Optional[str] = None
    acme_ca: Optional[str] = None        # "zerossl" or an ACME directory URL; Let's Encrypt if unset
    ollama_version: str = "v0.35.1"      # pinned; boxes install exactly this
    dns_publish_timeout: float = 180.0   # wait for all nameservers before booting a box
    control_plane: Tuple[str, ...] = ()  # addresses a policed box must keep reaching (default: resolved)
    heartbeat_timeout: float = 10 * 60
    boot_timeout: float = 25 * 60
    reap_interval: float = 30.0
    reconcile_interval: float = 300.0
    # Storage: persistent volumes, and the disk of a stopped box, are billed per GB-hour.
    volume_eur_per_gb_hour: float = 0.00013   # Scaleway SBS 5K IOPS, fr-par (catalog, 2026-11)
    root_disk_gb: int = 20                     # a template's disk when it does not set one
    limits: Limits = field(default_factory=Limits)
    providers: Dict[str, dict] = field(default_factory=dict)
    dns: dict = field(default_factory=dict)
    templates: Dict[str, Template] = field(default_factory=dict)
    storage: Optional[StorageConfig] = None

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


def _acme_ca(v) -> Optional[str]:
    """Which CA boxes take certificates from: Let's Encrypt (unset), "zerossl", or a directory."""
    v = (v or "").strip()
    if v in ("", "letsencrypt"):
        return None
    if v == "zerossl" or re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._-]+)*/?", v):
        return v
    raise ConfigError(f"acme_ca must be letsencrypt, zerossl or an https ACME directory, not {v!r}")


def _storage(st: dict, allowed: Optional[Tuple[str, ...]]) -> Optional[StorageConfig]:
    if not st:
        return None
    for key in ("endpoint_url", "upstream", "access_key", "secret_key", "key_secret"):
        if not st.get(key):
            raise ConfigError(f"[direct.storage] needs {key}")
    regions = tuple(st.get("regions") or ())
    if not regions:
        raise ConfigError("[direct.storage] needs regions")
    if allowed is not None:
        outside = [r for r in regions if not any(z.startswith(r + "-") or z == r for z in allowed)]
        if outside:
            raise ConfigError(f"[direct.storage] regions {outside} are outside allowed_regions")
    if not str(st["endpoint_url"]).startswith("https://") and not st.get("allow_http"):
        raise ConfigError("[direct.storage] endpoint_url must be https")
    try:
        from cryptography.fernet import Fernet
        Fernet(st["key_secret"])
    except Exception:
        raise ConfigError("[direct.storage] key_secret must be a Fernet key "
                          "(python -c 'from cryptography.fernet import Fernet; "
                          "print(Fernet.generate_key().decode())')")
    prefix = str(st.get("bucket_prefix", "rixi-"))
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", prefix):
        raise ConfigError("[direct.storage] bucket_prefix: lowercase letters, digits, '-'")
    return StorageConfig(
        endpoint_url=str(st["endpoint_url"]).rstrip("/"), upstream=str(st["upstream"]),
        regions=regions, access_key=st["access_key"], secret_key=st["secret_key"],
        key_secret=st["key_secret"], bucket_prefix=prefix,
        eur_per_gb_hour=float(st.get("eur_per_gb_hour", 0.000022)),
        eur_per_gb_hour_onezone=float(st.get("eur_per_gb_hour_onezone", 0.000011)),
        eur_per_gb_egress=float(st.get("eur_per_gb_egress", 0.01)),
        scan_interval=_duration(st.get("scan_interval", "1h")),
        abort_multipart_days=int(st.get("abort_multipart_days", 7)),
        cors_origins=_origins(st.get("cors_origins", [])),
        share_url=_share_url(st.get("share_url", ""), bool(st.get("allow_http"))))


def _share_url(v, allow_http: bool) -> str:
    if not v:
        return ""
    if not isinstance(v, str) or not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d+)?(/[\w./-]*)?", v):
        raise ConfigError("[direct.storage] share_url must be a URL like https://host/s")
    if not v.startswith("https://") and not allow_http:
        raise ConfigError("[direct.storage] share_url must be https")
    return v.rstrip("/")


def _origins(v) -> Tuple[str, ...]:
    if not isinstance(v, list) or not all(isinstance(o, str) for o in v):
        raise ConfigError("[direct.storage] cors_origins must be a list of origins")
    out = tuple(o.rstrip("/") for o in v)
    for o in out:
        if not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d+)?", o):
            raise ConfigError(f"[direct.storage] cors_origins: {o!r} is not an origin "
                              "(scheme://host[:port], no path, no wildcard)")
    return out


def parse(data: dict) -> DirectConfig:
    d = _subst(dict(data.get("direct") or {}))
    if not d:
        raise ConfigError("no [direct] table")
    for key in ("box_domain", "heartbeat_url", "box_jwks_url", "rixi_ref"):
        if not d.get(key):
            raise ConfigError(f"[direct] needs {key}")
    if not d.get("jwt_audience") and not d.get("allow_any_audience"):
        # Box tokens are usually signed by the same issuer (aud=box-<id>, with the tenant claim);
        # without an audience check here, a leaked box token could claim boxes on the API.
        raise ConfigError("[direct] needs jwt_audience (e.g. \"rixi-gateway\"), so tokens minted "
                          "for boxes are not accepted by the API")
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
        max_volumes=int(lim.get("max_volumes", 10)),
        max_volume_gb=int(lim.get("max_volume_gb", 1000)),
        max_stopped=_duration(lim.get("max_stopped", "7d")),
        max_buckets=int(lim.get("max_buckets", 20)),
        max_storage_gb=float(lim.get("max_storage_gb", 1000)),
        max_s3_keys=int(lim.get("max_s3_keys", 20)),
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
            eur_per_hour=_opt_float(body.get("eur_per_hour")),
            vcpu=int(body["vcpu"]) if body.get("vcpu") else None,
            ram_gb=_opt_float(body.get("ram_gb")), gpu=body.get("gpu") or None,
            gpu_ram_gb=_opt_float(body.get("gpu_ram_gb")))
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
        jwt_audience=d.get("jwt_audience") or None,
        tenant_claim=d.get("tenant_claim", "tenant"), roles_claim=d.get("roles_claim", "roles"),
        admin_role=d.get("admin_role", "admin"), acme_email=d.get("acme_email") or None,
        acme_ca=_acme_ca(d.get("acme_ca")),
        dns_publish_timeout=_duration(d.get("dns_publish_timeout", "3m")),
        control_plane=tuple(d.get("control_plane") or ()),
        heartbeat_timeout=_duration(d.get("heartbeat_timeout", "10m")),
        boot_timeout=_duration(d.get("boot_timeout", "25m")),
        reap_interval=_duration(d.get("reap_interval", "30s")),
        reconcile_interval=_duration(d.get("reconcile_interval", "5m")),
        volume_eur_per_gb_hour=float(d.get("volume_eur_per_gb_hour", 0.00013)),
        root_disk_gb=int(d.get("root_disk_gb", 20)),
        limits=limits, providers=dict(d.get("providers") or {}), dns=dict(d.get("dns") or {}),
        templates=templates, storage=_storage(dict(d.get("storage") or {}), allowed))


def load(path) -> DirectConfig:
    return parse(tomllib.loads(Path(path).read_text()))
