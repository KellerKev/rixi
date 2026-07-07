"""Policy engine — admin sets an enforced floor; users may only TIGHTEN, never loosen.

The admin authors a global `[policy]` table + per-resource `[resource.<n>.policy]` floors on the
gateway box. Every gateway decision (connect, control op, route, provision) runs through
`PolicyEngine.evaluate(...)` → an allow/deny `Decision` plus the resolved `SecurityReqs` that
provisioning must enforce on the box. "Admin is more important": a user request can make things
stricter (turn require_e2e on, narrow an allowlist, lower a quota) but never weaker.

Data shapes live here (pure, no I/O); parsing from TOML lives in config.py (which owns _subst /
_duration). With no `[policy]` table the engine allows everything → today's behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


class PolicyViolation(Exception):
    """A user request tried to widen an admin-defined allowlist (a 'loosen')."""


# ---- data ----------------------------------------------------------------

@dataclass(frozen=True)
class Quotas:
    max_concurrent_resources: Optional[int] = None
    max_total_provisions: Optional[int] = None
    # Budget guardrails (priced from gateway/offerings.toml). Fleet-wide, enforced for everyone
    # (admins included) since they cap spend, not permissions.
    max_eur_per_hour: Optional[float] = None    # cap on the live fleet burn rate
    max_fleet_eur: Optional[float] = None        # cap on cumulative estimated spend


@dataclass(frozen=True)
class RolePolicy:
    allowed_ops: Tuple[str, ...] = ()
    allowed_providers: Optional[Tuple[str, ...]] = None
    quotas: Quotas = field(default_factory=Quotas)


@dataclass(frozen=True)
class GlobalPolicy:
    require_jwt: bool = False
    require_e2e: bool = False
    require_tls: bool = False
    admin_role: str = "admin"
    default_role: str = "user"
    allowed_providers: Optional[Tuple[str, ...]] = None
    quotas: Quotas = field(default_factory=Quotas)
    roles: Dict[str, RolePolicy] = field(default_factory=dict)
    enabled: bool = False   # True iff a [policy] table was present


@dataclass(frozen=True)
class ResourcePolicy:
    require_e2e: bool = False
    require_jwt: bool = False
    require_tls: bool = False
    allowed_roles: Optional[Tuple[str, ...]] = None
    allowed_identities: Optional[Tuple[str, ...]] = None
    max_concurrent: Optional[int] = None
    max_age: Optional[float] = None
    allowed_regions: Optional[Tuple[str, ...]] = None


@dataclass(frozen=True)
class SecurityReqs:
    require_e2e: bool = False
    require_jwt: bool = False
    require_tls: bool = False


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str = ""
    security: Optional[SecurityReqs] = None


@dataclass
class UsageView:
    """Live usage, derived on read from the gateway's bridges (no drift-prone counters)."""
    per_identity_concurrent: Dict[str, int] = field(default_factory=dict)
    total_provisions: Dict[str, int] = field(default_factory=dict)
    fleet_eur_per_hour: float = 0.0     # sum of active boxes' hourly rates (excl. the requested one)
    total_eur_spent: float = 0.0        # cumulative estimated spend so far


# ---- tighten-only combinators --------------------------------------------

def tighten_bool(admin: bool, user) -> bool:
    """Floor: admin True forces True; if admin False, the user may opt in."""
    return bool(admin) or bool(user)


def tighten_allowlist(admin: Optional[tuple], user: Optional[tuple]) -> Optional[tuple]:
    """admin None = unbounded. The user set must be a subset of admin's (can't widen)."""
    if admin is None:
        return tuple(user) if user is not None else None
    if user is None:
        return admin
    if not set(user) <= set(admin):
        raise PolicyViolation("request widens an admin-defined allowlist")
    return tuple(sorted(set(admin) & set(user)))


def tighten_quota(admin: Optional[int], user: Optional[int]) -> Optional[int]:
    """Ceiling: the smaller of the two (None = unbounded on that side)."""
    vals = [v for v in (admin, user) if v is not None]
    return min(vals) if vals else None


# ---- engine --------------------------------------------------------------

class PolicyEngine:
    def __init__(self, glob: GlobalPolicy, catalog: dict):
        self.glob = glob
        self.catalog = catalog

    def role_of(self, identity) -> str:
        for r in identity.roles:
            if r in self.glob.roles:
                return r
        return self.glob.default_role

    def is_admin(self, identity) -> bool:
        return identity.has_role(self.glob.admin_role)

    def _actor(self, identity) -> str:
        return identity.sub or identity.label

    def evaluate(self, identity, action: str, resource=None, request=None,
                 usage: Optional[UsageView] = None) -> Decision:
        request = request or {}
        usage = usage or UsageView()
        admin = self.is_admin(identity)

        # 1. anon under a require_jwt floor (defense-in-depth; also enforced at connect)
        if self.glob.require_jwt and identity.anon and not admin:
            return Decision(False, "jwt required")

        role = self.role_of(identity)
        rpol = getattr(resource, "policy", None) or ResourcePolicy() if resource is not None else ResourcePolicy()

        if not admin:
            rp = self.glob.roles.get(role, RolePolicy())
            # 2. op allowed for role
            if rp.allowed_ops and "*" not in rp.allowed_ops and action not in rp.allowed_ops:
                return Decision(False, f"role '{role}' may not '{action}'")

            # provider allowlist (global ∩ role), applied to resource or ephemeral request
            provider = getattr(resource, "provider", None) or request.get("provider")
            if provider is not None:
                try:
                    providers = tighten_allowlist(self.glob.allowed_providers, rp.allowed_providers)
                except PolicyViolation as e:
                    return Decision(False, str(e))
                if providers is not None and provider not in providers:
                    return Decision(False, f"provider '{provider}' not allowed for role '{role}'")

            # 3. resource-scoped RBAC + quota
            if resource is not None:
                if rpol.allowed_roles is not None and role not in rpol.allowed_roles:
                    return Decision(False, f"resource '{resource.name}' not allowed for role '{role}'")
                if rpol.allowed_identities and identity.sub not in rpol.allowed_identities:
                    return Decision(False, f"identity not allowed on resource '{resource.name}'")
                region = (getattr(resource, "vars", None) or {}).get("region")
                if rpol.allowed_regions is not None and region is not None \
                        and region not in rpol.allowed_regions:
                    return Decision(False, f"region '{region}' not allowed for '{resource.name}'")

                actor = self._actor(identity)
                ceiling = tighten_quota(
                    tighten_quota(self.glob.quotas.max_concurrent_resources,
                                  rp.quotas.max_concurrent_resources),
                    rpol.max_concurrent)
                if ceiling is not None and usage.per_identity_concurrent.get(actor, 0) >= ceiling:
                    return Decision(False, "quota exceeded (max concurrent resources)")
                tot_ceiling = tighten_quota(self.glob.quotas.max_total_provisions,
                                            rp.quotas.max_total_provisions)
                if tot_ceiling is not None and usage.total_provisions.get(actor, 0) >= tot_ceiling:
                    return Decision(False, "quota exceeded (max total provisions)")

        # 3b. budget guardrails — spend caps apply to EVERYONE (admins included).
        if action == "request_compute":
            from . import offerings
            provider = getattr(resource, "provider", None) or request.get("provider")
            itype = (getattr(resource, "vars", None) or {}).get("instance_type")
            new_rate = offerings.price_per_hour(provider, itype) or 0.0
            if self.glob.quotas.max_eur_per_hour is not None and \
                    usage.fleet_eur_per_hour + new_rate > self.glob.quotas.max_eur_per_hour + 1e-9:
                return Decision(False, "budget cap exceeded (max €/hour)")
            if self.glob.quotas.max_fleet_eur is not None and \
                    usage.total_eur_spent >= self.glob.quotas.max_fleet_eur:
                return Decision(False, "budget cap exceeded (max fleet spend)")

        # 4. resolve the security floor the box must enforce (global ∨ resource ∨ user-tighten)
        security = SecurityReqs(
            tighten_bool(self.glob.require_e2e or rpol.require_e2e, request.get("require_e2e")),
            tighten_bool(self.glob.require_jwt or rpol.require_jwt, request.get("require_jwt")),
            tighten_bool(self.glob.require_tls or rpol.require_tls, request.get("require_tls")),
        )
        return Decision(True, "admin" if admin else "", security)
