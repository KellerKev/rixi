"""Compute claims + one-time registration tokens.

When a client asks the gateway for compute, the gateway mints a single-use, TTL-bound token and
records a pending Claim. Provisioning configures the new box to dial the gateway with that token as
its tunnel `node_id` (works with the unmodified open `rixi-tunnel connect`). When a server registers
with a token that matches a pending claim, the gateway binds it to the requesting client and routes
them together.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class Claim:
    token: str
    client_id: str
    provider: str
    spec: dict = field(default_factory=dict)
    status: str = "pending"          # pending → active → released | expired | failed
    node_id: Optional[str] = None    # the registered server, once it dials in (== token)
    workdir: Optional[str] = None    # tofu workspace for this claim (for teardown)
    created_at: float = field(default_factory=time.time)
    ttl: float = 900.0               # token must be redeemed within this window

    @property
    def expired(self) -> bool:
        return self.status == "pending" and (time.time() - self.created_at) > self.ttl


class Claims:
    """In-memory claim store keyed by token. (Persistence is a later concern.)"""

    def __init__(self) -> None:
        self._by_token: Dict[str, Claim] = {}

    def create(self, client_id: str, provider: str, spec: dict, ttl: float = 900.0) -> Claim:
        token = "rxtok_" + secrets.token_urlsafe(24)
        claim = Claim(token=token, client_id=client_id, provider=provider, spec=spec or {}, ttl=ttl)
        self._by_token[token] = claim
        return claim

    def get(self, token: str) -> Optional[Claim]:
        return self._by_token.get(token)

    def redeem(self, token: str, node_id: str) -> Optional[Claim]:
        """Validate a token a registering server presents. Single-use + TTL.

        Returns the now-active claim, or None if the token is unknown/expired/already used.
        """
        claim = self._by_token.get(token)
        if claim is None or claim.status != "pending" or claim.expired:
            return None
        claim.status = "active"
        claim.node_id = node_id
        return claim

    def by_client(self, client_id: str):
        return [c for c in self._by_token.values() if c.client_id == client_id]

    def release(self, token: str) -> Optional[Claim]:
        claim = self._by_token.get(token)
        if claim is not None:
            claim.status = "released"
        return claim

    def list(self):
        return list(self._by_token.values())
