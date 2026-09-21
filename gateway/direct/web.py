"""HTTP API for direct mode, on websaw-ng.

    GET    /api/templates                 what can be claimed, with prices
    POST   /api/boxes                     claim a box   {template, zone?, ttl?, idle_timeout?, ssh_key?}
    GET    /api/boxes                     your tenant's boxes (admin: all, or ?tenant=)
    GET    /api/boxes/<id>
    DELETE /api/boxes/<id>                release it now
    POST   /api/boxes/<id>/revoke         {jti} — the box refuses that token from its next heartbeat
    GET    /api/usage?since=              billable box time (admin: all tenants, or ?tenant=)
    POST   /api/tenants/<tenant>/stop     admin: release every box of a tenant
    POST   /gw/heartbeat                  box → gateway; authenticated by the box's own secret
    GET    /healthz

Callers present a JWT; the tenant comes from a configurable claim and every read and write is
scoped to it. Another tenant's box answers 404, never 403, so ids cannot be probed.

Each `build_app()` returns an isolated websaw-ng app (its own router), so tests and embedders can
mount several side by side. Service calls may block (provider APIs, the authorizer), so handlers
run them in a worker thread.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

import ombott_ng
from websaw_ng import DefaultApp
from websaw_ng.core import BaseContext
from websaw_ng.core.fixture import Fixture
from websaw_ng.renders import jsonfy

from ..auth import JwtVerifier
from .service import Caller, Denied, DirectService, NotFound


def json_error(status: int, detail: str) -> ombott_ng.HTTPResponse:
    return ombott_ng.HTTPResponse(body=json.dumps({"detail": detail}), status=status,
                                  headers={"Content-Type": "application/json"})


def _bearer() -> Optional[str]:
    h = ombott_ng.request.headers.get("Authorization") or ""
    return h[7:].strip() if h.lower().startswith("bearer ") else None


def _body() -> dict:
    try:
        body = ombott_ng.request.json
    except Exception:
        raise json_error(400, "body must be JSON")
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise json_error(400, "body must be a JSON object")
    return body


def _num(body: dict, key: str, default=None, allow_null: bool = False):
    if key not in body:
        return default
    v = body[key]
    if v is None and allow_null:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise json_error(400, f"{key} must be a number of seconds")
    return float(v)


def _str(body: dict, key: str, required: bool = False) -> Optional[str]:
    v = body.get(key)
    if v is None:
        if required:
            raise json_error(400, f"{key} is required")
        return None
    if not isinstance(v, str):
        raise json_error(400, f"{key} must be a string")
    return v


class CallerFixture(Fixture):
    """Verifies the bearer JWT and exposes `ctx.caller` (tenant-scoped), or answers 401."""

    def __init__(self, verifier: JwtVerifier, tenant_claim: str, admin_role: str,
                 require_admin: bool = False):
        self.verifier = verifier
        self.tenant_claim = tenant_claim
        self.admin_role = admin_role
        self.require_admin = require_admin

    async def atake_on(self, ctx):
        token = _bearer()
        ident = await asyncio.to_thread(self.verifier._verify_sync, token) \
            if token and self.verifier.enabled else None
        if ident is None:
            raise json_error(401, "valid bearer token required")
        tenant = ident.claims.get(self.tenant_claim)
        caller = Caller(sub=ident.sub,
                        tenant=str(tenant) if tenant not in (None, "") else None,
                        admin=ident.has_role(self.admin_role))
        if self.require_admin and not caller.admin:
            raise json_error(403, "admin role required")
        return caller


async def _call(fn, *a, **kw):
    try:
        return await asyncio.to_thread(fn, *a, **kw)
    except NotFound:
        raise json_error(404, "no such box")
    except Denied as d:
        raise json_error(d.status, d.reason)


def build_app(svc: DirectService, verifier: JwtVerifier) -> DefaultApp:
    cfg = svc.cfg

    class Context(BaseContext):
        caller = CallerFixture(verifier, cfg.tenant_claim, cfg.admin_role)
        admin = CallerFixture(verifier, cfg.tenant_claim, cfg.admin_role, require_admin=True)

    ctxd = Context()
    app = DefaultApp(ctxd, name=__package__, isolated=True)
    app.default_config["render_map"][list] = jsonfy

    @app.route("/healthz")
    def healthz(ctx):
        return {"ok": True}

    @app.route("/api/templates")
    @app.use(ctxd.caller)
    async def templates(ctx):
        return {"templates": await _call(svc.templates)}

    @app.route("/api/boxes", method="POST")
    @app.use(ctxd.caller)
    async def claim(ctx):
        body = _body()
        box = await _call(svc.claim, ctx.caller, _str(body, "template", required=True),
                          zone=_str(body, "zone"), ttl=_num(body, "ttl"),
                          idle_timeout=_num(body, "idle_timeout", default=-1, allow_null=True),
                          ssh_key=_str(body, "ssh_key"))
        ombott_ng.response.status = 202
        return box.public(svc.clock())

    @app.route("/api/boxes")
    @app.use(ctxd.caller)
    async def boxes(ctx):
        q = ombott_ng.request.query
        live = q.get("live", "") in ("1", "true", "yes")
        rows = await _call(svc.list, ctx.caller, q.get("tenant") or None, live_only=live)
        return {"boxes": [b.public(svc.clock()) for b in rows]}

    @app.route("/api/boxes/<box_id>")
    @app.use(ctxd.caller)
    async def box(ctx, box_id):
        return (await _call(svc.get, ctx.caller, box_id)).public(svc.clock())

    @app.route("/api/boxes/<box_id>", method="DELETE")
    @app.use(ctxd.caller)
    async def release(ctx, box_id):
        box = await _call(svc.release, box_id, "released", caller=ctx.caller)
        return box.public(svc.clock())

    @app.route("/api/boxes/<box_id>/revoke", method="POST")
    @app.use(ctxd.caller)
    async def revoke(ctx, box_id):
        await _call(svc.revoke, ctx.caller, box_id, _str(_body(), "jti", required=True))
        return {"revoked": True}

    @app.route("/api/usage")
    @app.use(ctxd.caller)
    async def usage(ctx):
        q = ombott_ng.request.query
        try:
            since = float(q.get("since", 0) or 0)
        except ValueError:
            raise json_error(400, "since must be a unix timestamp")
        rows = await _call(svc.usage, ctx.caller, since, q.get("tenant") or None)
        return {"now": svc.clock(), "usage": rows}

    @app.route("/api/tenants/<tenant>/stop", method="POST")
    @app.use(ctxd.admin)
    async def stop_tenant(ctx, tenant):
        return {"released": [b.id for b in await _call(svc.stop_tenant, tenant)]}

    @app.route("/gw/heartbeat", method="POST")
    async def heartbeat(ctx):
        body = _body()
        tasks = body.get("active_tasks", 0)
        if isinstance(tasks, bool) or not isinstance(tasks, int):
            raise json_error(400, "active_tasks must be an integer")
        return await _call(svc.heartbeat, _str(body, "box_id", required=True), _bearer() or "",
                           tasks, bool(body.get("tls_ready")))

    app.mount()
    return app


def asgi(app: DefaultApp):
    """A plain ASGI3 callable for uvicorn (a bound method trips its ASGI2/3 autodetection)."""
    inner = app.asgi

    async def application(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        await inner(scope, receive, send)
    return application
