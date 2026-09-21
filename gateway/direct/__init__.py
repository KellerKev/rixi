"""Direct mode: the gateway provisions, meters and reaps boxes; clients reach them directly.

See service.py for the model and web.py for the HTTP surface (websaw-ng). Run with `python -m gateway.direct`.
"""
from __future__ import annotations

import logging
from typing import Optional

from .config import DirectConfig, load
from .service import Caller, Denied, DirectService, NotFound
from .store import Box, Store

__all__ = ["Box", "Caller", "Denied", "DirectConfig", "DirectService", "NotFound", "Store",
           "build_service", "load"]

log = logging.getLogger("rixi.direct")


def make_provider(name: str, conf: dict):
    from .providers import DummyProvider, ScalewayProvider
    if name == "dummy":
        return DummyProvider()
    if name == "scaleway":
        return ScalewayProvider(secret_key=conf.get("secret_key", ""),
                                project_id=conf.get("project_id", ""))
    raise ValueError(f"direct mode has no provider {name!r} (available: scaleway, dummy)")


def make_dns(conf: dict, providers_conf: dict):
    from .providers import DummyDns, NullDns, ScalewayDns
    kind = conf.get("provider", "none")
    if kind == "none":
        return NullDns()
    if kind == "dummy":
        return DummyDns()
    if kind == "scaleway":
        key = conf.get("secret_key") or providers_conf.get("scaleway", {}).get("secret_key", "")
        return ScalewayDns(secret_key=key, zone=conf["zone"], ttl=int(conf.get("ttl", 60)))
    raise ValueError(f"unknown dns provider {kind!r}")


def http_authorizer(url: str, token: Optional[str], timeout: float = 5.0):
    """POST the claim to an external service (e.g. a billing portal). Fails closed."""
    import requests

    def authorize(payload: dict):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
            body = r.json() if r.content else {}
        except Exception as exc:
            log.warning("authorizer unreachable: %s", exc)
            return False, "authorizer unavailable"
        return (r.status_code == 200 and body.get("allow") is True), body.get("reason", "")
    return authorize


def build_service(cfg: DirectConfig, audit=None) -> DirectService:
    providers = {name: make_provider(name, cfg.providers.get(name, {}))
                 for name in {t.provider for t in cfg.templates.values()}}
    authorize = http_authorizer(cfg.authorizer_url, cfg.authorizer_token) \
        if cfg.authorizer_url else None
    return DirectService(cfg, Store(cfg.store), providers, make_dns(cfg.dns, cfg.providers),
                         authorize=authorize, audit=audit)
