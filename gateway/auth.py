"""JWT identity for the gateway — RBAC for clients.

Clients present a JWT (in the auth frame, already AES-encrypted on the wire); the gateway verifies
it with a local public key OR a JWKS URL (RS256/ES256), exactly like the open rixi server
(server/rixi_server.py validate_token). The verified identity (`sub` + roles/scopes) is bound to
the connection so the policy engine can make RBAC decisions. Verification is None-safe: with no
verifier configured the gateway behaves as before (anonymous identities).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional, Tuple

import jwt

_ALGS = ("RS256", "ES256")


@dataclass(frozen=True)
class Identity:
    sub: str = ""
    roles: Tuple[str, ...] = ()
    scopes: Tuple[str, ...] = ()
    claims: dict = field(default_factory=dict)
    anon: bool = False

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    @property
    def label(self) -> str:
        return self.sub or ("anon" if self.anon else "?")


ANON = Identity(anon=True)


def _dig(payload: dict, dotted: str):
    """Fetch a (possibly nested, dotted) claim, e.g. 'realm_access.roles'."""
    cur = payload
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _as_tuple(value) -> Tuple[str, ...]:
    """Normalise a claim to a tuple of strings: list | space-delimited str | single str."""
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(v for v in value.split() if v)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return (str(value),)


def identity_from_claims(payload: dict, roles_claim: str = "roles",
                         scopes_claim: str = "scope") -> Identity:
    return Identity(
        sub=str(payload.get("sub", "")),
        roles=_as_tuple(_dig(payload, roles_claim)),
        scopes=_as_tuple(_dig(payload, scopes_claim)),
        claims=payload,
    )


class JwtVerifier:
    """Verifies client JWTs against a PEM public key or a JWKS endpoint.

    JWKS keys are fetched + cached by PyJWT's PyJWKClient (its `lifespan` cache is also the
    rate-limit: an unknown `kid` within the window fails without re-fetching). All verification runs
    in a worker thread so the asyncio loop never blocks on the network.
    """

    def __init__(self, public_key_pem: Optional[str] = None, jwks_url: Optional[str] = None,
                 algorithms=_ALGS, roles_claim: str = "roles", scopes_claim: str = "scope",
                 audience: Optional[str] = None, jwks_cache_lifespan: float = 300.0):
        self.public_key_pem = public_key_pem
        self.jwks_url = jwks_url
        self.algorithms = list(algorithms)
        self.roles_claim = roles_claim
        self.scopes_claim = scopes_claim
        self.audience = audience
        self._jwk_client = (
            jwt.PyJWKClient(jwks_url, lifespan=int(jwks_cache_lifespan)) if jwks_url else None)

    @property
    def enabled(self) -> bool:
        return bool(self.public_key_pem or self.jwks_url)

    async def start(self) -> None:
        # Best-effort prefetch of the JWKS so the first real verify is fast; failures are non-fatal.
        if self._jwk_client is not None:
            try:
                await asyncio.to_thread(self._jwk_client.get_signing_keys)
            except Exception:
                pass

    async def stop(self) -> None:
        return None

    async def verify(self, token: str) -> Optional[Identity]:
        if not token or not self.enabled:
            return None
        try:
            return await asyncio.to_thread(self._verify_sync, token)
        except Exception:
            return None

    def _verify_sync(self, token: str) -> Optional[Identity]:
        opts = {"verify_aud": self.audience is not None}
        try:
            if self._jwk_client is not None:
                key = self._jwk_client.get_signing_key_from_jwt(token).key
            else:
                key = self.public_key_pem
            payload = jwt.decode(token, key, algorithms=self.algorithms,
                                 audience=self.audience, options=opts)
        except jwt.PyJWTError:
            return None
        return identity_from_claims(payload, self.roles_claim, self.scopes_claim)
