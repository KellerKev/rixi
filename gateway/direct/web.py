"""HTTP API for direct mode, on websaw-ng.

    GET    /api/templates                 what can be claimed, with prices
    POST   /api/boxes                     claim a box   {template, zone?, ttl?, idle_timeout?, ssh_key?}
    GET    /api/boxes                     your tenant's boxes (admin: all, or ?tenant=)
    GET    /api/boxes/<id>
    DELETE /api/boxes/<id>                release it now
    POST   /api/boxes/<id>/revoke         {jti} — the box refuses that token from its next heartbeat
    GET    /api/usage?since=              billable box time and volume GB-hours (admin: all, or ?tenant=)
    POST   /api/boxes/<id>/stop · /start   power a ready box off (disk kept) · back on
    POST   /api/volumes · GET · DELETE /api/volumes/<id>   persistent volumes
    POST   /api/buckets · GET · GET|PATCH|DELETE /api/buckets/<id>   S3 buckets (?force=1 empties)
    GET    /api/buckets/<id>/objects?prefix=&token=   one folder of a bucket (the console)
    POST   /api/buckets/<id>/presign      {op, key, upload_id?, part?} — a URL for the browser
    POST   /api/buckets/<id>/delete-objects   {keys?, prefixes?}
    POST   /api/s3-keys · GET · DELETE /api/s3-keys/<access key>     S3 keys (secret shown once)
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
        raise json_error(404, "not found")
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
        # The release the boxes install, so a caller can pin the client to the same one.
        out = {"templates": await _call(svc.templates), "rixi_ref": cfg.rixi_ref,
               "rixi_repo": cfg.rixi_repo}
        if cfg.storage is not None:
            st = cfg.storage
            out["storage"] = {"endpoint_url": st.endpoint_url, "regions": list(st.regions),
                              "eur_per_gb_hour": st.eur_per_gb_hour,
                              "eur_per_gb_hour_onezone": st.eur_per_gb_hour_onezone,
                              "eur_per_gb_egress": st.eur_per_gb_egress}
        return out

    @app.route("/api/boxes", method="POST")
    @app.use(ctxd.caller)
    async def claim(ctx):
        body = _body()
        serve = body.get("serve")
        if serve is not None and not isinstance(serve, dict):
            raise json_error(400, "serve must be an object: {kind, model}")
        policy = body.get("policy")
        if policy is not None and not isinstance(policy, dict):
            raise json_error(400, "policy must be an object: {egress, ingress}")
        llm = body.get("llm")
        if llm is not None and not isinstance(llm, dict):
            raise json_error(400, "llm must be an object: {base_url, token}")
        volumes = body.get("volumes") or []
        if not isinstance(volumes, list) or not all(isinstance(v, str) for v in volumes):
            raise json_error(400, "volumes must be a list of volume ids")
        storage = body.get("storage")
        if storage is not None and not isinstance(storage, dict):
            raise json_error(400, "storage must be an object: {buckets?, write?}")
        require, among = body.get("require"), body.get("among")
        if require is not None and not isinstance(require, dict):
            raise json_error(400, "require must be an object, e.g. {\"gpu_ram_gb\": 40}")
        if among is not None and not (isinstance(among, list)
                                      and all(isinstance(n, str) for n in among)):
            raise json_error(400, "among must be a list of template names")
        box = await _call(svc.claim, ctx.caller, _str(body, "template"),
                          zone=_str(body, "zone"), ttl=_num(body, "ttl"),
                          idle_timeout=_num(body, "idle_timeout", default=-1, allow_null=True),
                          ssh_key=_str(body, "ssh_key"), serve=serve, policy=policy, llm=llm,
                          volumes=volumes, on_idle=_str(body, "on_idle") or "release",
                          require=require, among=among, storage=storage)
        out = box.public(svc.clock())
        key = getattr(box, "endpoint_key", None)
        if key:
            # Shown once: the gateway keeps no copy, only the box has it.
            out["endpoint_key"] = key
            out["endpoint_url"] = f"https://{box.hostname}/v1"
        ombott_ng.response.status = 202
        return out

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

    @app.route("/api/boxes/<box_id>/stop", method="POST")
    @app.use(ctxd.caller)
    async def stop(ctx, box_id):
        return (await _call(svc.stop_box, ctx.caller, box_id)).public(svc.clock())

    @app.route("/api/boxes/<box_id>/start", method="POST")
    @app.use(ctxd.caller)
    async def start(ctx, box_id):
        return (await _call(svc.start_box, ctx.caller, box_id)).public(svc.clock())

    @app.route("/api/volumes", method="POST")
    @app.use(ctxd.caller)
    async def create_volume(ctx):
        body = _body()
        v = await _call(svc.create_volume, ctx.caller, body.get("name"), body.get("size_gb"),
                        _str(body, "zone"))
        ombott_ng.response.status = 202
        return v.public()

    @app.route("/api/volumes")
    @app.use(ctxd.caller)
    async def volumes(ctx):
        return {"volumes": [v.public() for v in await _call(svc.list_volumes, ctx.caller)]}

    @app.route("/api/volumes/<volume_id>", method="DELETE")
    @app.use(ctxd.caller)
    async def delete_volume(ctx, volume_id):
        return (await _call(svc.delete_volume, ctx.caller, volume_id)).public()

    @app.route("/api/usage")
    @app.use(ctxd.caller)
    async def usage(ctx):
        q = ombott_ng.request.query
        try:
            since = float(q.get("since", 0) or 0)
        except ValueError:
            raise json_error(400, "since must be a unix timestamp")
        rows = await _call(svc.usage, ctx.caller, since, q.get("tenant") or None)
        vols = await _call(svc.volume_usage, ctx.caller, since, q.get("tenant") or None)
        out = {"now": svc.clock(), "usage": rows, "volumes": vols}
        if svc.storage is not None:
            out["storage"] = await _call(svc.storage.usage, ctx.caller, since,
                                         q.get("tenant") or None)
        return out

    def _storage():
        if svc.storage is None:
            raise json_error(404, "this gateway offers no storage")
        return svc.storage

    @app.route("/api/buckets", method="POST")
    @app.use(ctxd.caller)
    async def create_bucket(ctx):
        body = _body()
        days = body.get("expire_days")
        b = await _call(_storage().create_bucket, ctx.caller, _str(body, "name", required=True),
                        _str(body, "region"), days)
        ombott_ng.response.status = 201
        return b.public()

    @app.route("/api/buckets")
    @app.use(ctxd.caller)
    async def buckets(ctx):
        st = _storage()
        rows = await _call(st.list_buckets, ctx.caller,
                           ombott_ng.request.query.get("tenant") or None)
        return {"buckets": [b.public() for b in rows], "endpoint_url": st.st.endpoint_url,
                "regions": list(st.st.regions)}

    @app.route("/api/buckets/<bucket_id>")
    @app.use(ctxd.caller)
    async def bucket(ctx, bucket_id):
        return (await _call(_storage().get_bucket, ctx.caller, bucket_id)).public()

    @app.route("/api/buckets/<bucket_id>", method="PATCH")
    @app.use(ctxd.caller)
    async def patch_bucket(ctx, bucket_id):
        body = _body()
        if "expire_days" not in body:
            raise json_error(400, "only expire_days can be changed")
        return (await _call(_storage().set_expiry, ctx.caller, bucket_id,
                            body["expire_days"])).public()

    @app.route("/api/buckets/<bucket_id>", method="DELETE")
    @app.use(ctxd.caller)
    async def delete_bucket(ctx, bucket_id):
        force = ombott_ng.request.query.get("force", "") in ("1", "true", "yes")
        return (await _call(_storage().delete_bucket, ctx.caller, bucket_id, force)).public()

    @app.route("/api/buckets/<bucket_id>/objects")
    @app.use(ctxd.caller)
    async def objects(ctx, bucket_id):
        q = ombott_ng.request.query
        try:
            max_keys = int(q.get("max_keys") or 200)
        except ValueError:
            raise json_error(400, "max_keys must be a number")
        return await _call(_storage().list_objects, ctx.caller, bucket_id, q.get("prefix") or "",
                           q.get("token") or None, max_keys)

    @app.route("/api/buckets/<bucket_id>/presign", method="POST")
    @app.use(ctxd.caller)
    async def presign(ctx, bucket_id):
        body = _body()
        return await _call(_storage().presign, ctx.caller, bucket_id,
                           _str(body, "op", required=True), body.get("key"),
                           _str(body, "upload_id"), body.get("part"), _str(body, "filename"))

    @app.route("/api/buckets/<bucket_id>/delete-objects", method="POST")
    @app.use(ctxd.caller)
    async def delete_objects(ctx, bucket_id):
        body = _body()
        keys, prefixes = body.get("keys") or [], body.get("prefixes") or []
        if not isinstance(keys, list) or not isinstance(prefixes, list):
            raise json_error(400, "keys and prefixes must be lists")
        if not keys and not prefixes:
            raise json_error(400, "nothing to delete")
        return await _call(_storage().delete_objects, ctx.caller, bucket_id, keys, prefixes)

    @app.route("/api/s3-keys", method="POST")
    @app.use(ctxd.caller)
    async def create_s3_key(ctx):
        body = _body()
        bks = body.get("buckets")
        write = body.get("write", True)
        if not isinstance(write, bool):
            raise json_error(400, "write must be true or false")
        st = _storage()
        k, secret = await _call(st.create_key, ctx.caller, _str(body, "name", required=True),
                                bks, write)
        ombott_ng.response.status = 201
        # The secret is shown here once; the gateway keeps it only encrypted, for checking.
        return {**k.public(), "secret_key": secret, "endpoint_url": st.st.endpoint_url,
                "region": st.st.regions[0]}

    @app.route("/api/s3-keys")
    @app.use(ctxd.caller)
    async def s3_keys(ctx):
        rows = await _call(_storage().list_keys, ctx.caller)
        return {"keys": [k.public() for k in rows]}

    @app.route("/api/s3-keys/<access_key>", method="DELETE")
    @app.use(ctxd.caller)
    async def revoke_s3_key(ctx, access_key):
        return (await _call(_storage().revoke_key, ctx.caller, access_key)).public()

    @app.route("/api/tenants/<tenant>/stop", method="POST")
    @app.use(ctxd.admin)
    async def stop_tenant(ctx, tenant):
        return {"released": [b.id for b in await _call(svc.stop_tenant, tenant)]}

    @app.route("/gw/heartbeat", method="POST")
    async def heartbeat(ctx):
        body = _body()

        def count(name):
            v = body.get(name, 0)
            if isinstance(v, bool) or not isinstance(v, int):
                raise json_error(400, f"{name} must be an integer")
            return v

        resolvers = body.get("resolvers")
        if resolvers is not None and not (isinstance(resolvers, list)
                                          and all(isinstance(r, str) for r in resolvers)):
            raise json_error(400, "resolvers must be a list of addresses")
        return await _call(svc.heartbeat, _str(body, "box_id", required=True), _bearer() or "",
                           count("active_tasks"), bool(body.get("tls_ready")),
                           requests=count("requests"),
                           model_ready=bool(body.get("model_ready")), resolvers=resolvers)

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
