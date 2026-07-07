"""Management HTTP API for the gateway (the Preact console talks to this).

A small FastAPI app, co-hosted in the gateway's asyncio loop on a separate admin port. Read access
needs a valid JWT (same verifier as the tunnel); mutations (policy changes) need the admin role.
Endpoints are intentionally thin views over live gateway state + the DuckDB audit store. With no
verifier configured, protected routes return 401 (so the API is closed by default).
"""
from __future__ import annotations

import dataclasses
import os
from typing import Optional

from . import audit as audit_mod
from .auth import ANON


def _identity_summary(gateway, identity):
    return {"sub": identity.sub, "roles": list(identity.roles),
            "role": gateway.policy.role_of(identity), "admin": gateway.policy.is_admin(identity),
            "anon": identity.anon}


def _policy_dict(gateway):
    glob = dataclasses.asdict(gateway.policy.glob)
    resources = {name: (dataclasses.asdict(r.policy) if r.policy else None)
                 for name, r in gateway.catalog.items()}
    return {"global": glob, "resources": resources}


def _sessions(gateway):
    out = []
    for node in gateway.registry.list("client"):
        cnt = sum(1 for k in gateway._bridges if k[0] == id(node.conn))
        if cnt:
            ident = getattr(node.conn, "auth", None)
            out.append({"client": node.node_id,
                        "identity": getattr(ident, "sub", "") if ident else "",
                        "sessions": cnt})
    return out


def build_app(gateway):
    from fastapi import Depends, FastAPI, Header, HTTPException, Query
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI(title="rixi gateway console API")
    # Restrict browser origins for the console API. Defaults to the local dev origin; set
    # RIXI_CONSOLE_ORIGINS (comma-separated) for real deployments.
    _origins = [o.strip() for o in os.getenv(
        "RIXI_CONSOLE_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=_origins,
                       allow_methods=["*"], allow_headers=["*"])

    async def require_identity(authorization: Optional[str] = Header(default=None)):
        token = None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        ident = ANON
        if gateway.verifier is not None and token:
            ident = await gateway.verifier.verify(token) or ANON
        if ident.anon:
            raise HTTPException(status_code=401, detail="valid JWT required")
        return ident

    async def require_admin(identity=Depends(require_identity)):
        if not gateway.policy.is_admin(identity):
            raise HTTPException(status_code=403, detail="admin role required")
        return identity

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "ws_port": gateway.ws_port,
                "policy": getattr(gateway.policy.glob, "enabled", False),
                "audit": gateway.auditor is not None}

    @app.get("/api/whoami")
    async def whoami(identity=Depends(require_identity)):
        return _identity_summary(gateway, identity)

    @app.get("/api/nodes")
    async def nodes(identity=Depends(require_identity)):
        return {"nodes": gateway.list_nodes()}

    @app.get("/api/resources")
    async def resources(identity=Depends(require_identity)):
        return {"resources": gateway.list_resources()}

    @app.get("/api/sessions")
    async def sessions(identity=Depends(require_identity)):
        return {"sessions": _sessions(gateway)}

    @app.get("/api/policy")
    async def get_policy(identity=Depends(require_identity)):
        return _policy_dict(gateway)

    @app.put("/api/policy")
    async def put_policy(body: dict, identity=Depends(require_admin)):
        gateway.reload_policy(body.get("global") or body.get("policy") or {})
        await gateway._audit(audit_mod.POLICY_CHANGED, identity.sub, "policy.put", decision="allow")
        return _policy_dict(gateway)

    @app.post("/api/resources/{name}/teardown")
    async def teardown_resource(name: str, identity=Depends(require_admin)):
        # _teardown_resource emits the TEARDOWN audit event (with cost) itself.
        return await gateway._teardown_resource(name, actor=identity.sub)

    @app.post("/api/resources/{name}/provision")
    async def provision_resource(name: str, identity=Depends(require_admin)):
        rdef = gateway.catalog.get(name)
        if rdef is None:
            raise HTTPException(status_code=404, detail="unknown resource")
        decision = gateway.policy.evaluate(identity, "request_compute", resource=rdef)
        err = gateway._secure_config_error(rdef, decision.security)
        if err:
            raise HTTPException(status_code=400, detail=err)
        res = await gateway._request_resource(identity.sub, name, decision.security)
        await gateway._audit(audit_mod.RES_REQUESTED, identity.sub, "api.provision", target=name,
                             decision="allow", attrs={"reused": res.get("reused")})
        return res

    @app.get("/api/costs")
    async def costs(identity=Depends(require_admin),
                    since: Optional[float] = None, until: Optional[float] = None):
        """Estimated spend from teardown events (est_cost recorded per box lifetime)."""
        store = gateway.auditor.duckdb if gateway.auditor is not None else None
        if store is None:
            return {"note": "no DuckDB audit store configured", "total": 0.0,
                    "by_resource": {}, "by_actor": {}}
        events = await store.query(event=audit_mod.TEARDOWN, since=since, until=until, limit=1000)
        total, by_res, by_actor, currency = 0.0, {}, {}, "EUR"
        for e in events:
            attrs = e.get("attrs") or {}
            c = attrs.get("est_cost")
            if c is None:
                continue
            currency = attrs.get("currency", currency)
            total += c
            tgt, act = e.get("target"), e.get("actor")
            by_res[tgt] = round(by_res.get(tgt, 0.0) + c, 6)
            by_actor[act] = round(by_actor.get(act, 0.0) + c, 6)
        # Live budget view (caps from the policy floor + current burn/spend).
        q = gateway.policy.glob.quotas
        u = gateway._usage()
        budget = {"max_eur_per_hour": q.max_eur_per_hour, "max_fleet_eur": q.max_fleet_eur,
                  "fleet_eur_per_hour": u.fleet_eur_per_hour, "total_eur_spent": u.total_eur_spent}
        if q.max_fleet_eur is not None:
            budget["remaining_eur"] = round(q.max_fleet_eur - u.total_eur_spent, 6)
        return {"currency": currency, "total": round(total, 6), "count": len(events),
                "by_resource": by_res, "by_actor": by_actor, "budget": budget}

    @app.get("/api/audit")
    async def audit(identity=Depends(require_admin),
                    since: Optional[float] = None, until: Optional[float] = None,
                    actor: Optional[str] = None, event: Optional[str] = None,
                    decision: Optional[str] = None, target: Optional[str] = None,
                    limit: int = Query(100, le=1000), offset: int = 0):
        store = gateway.auditor.duckdb if gateway.auditor is not None else None
        if store is None:
            return {"events": [], "note": "no DuckDB audit store configured"}
        events = await store.query(since=since, until=until, actor=actor, event=event,
                                   decision=decision, target=target, limit=limit, offset=offset)
        return {"events": events}

    # Serve the built PyLevate console (console/dist/web) at / — mounted LAST so /api/* wins.
    console = os.path.join(os.path.dirname(__file__), "console", "dist", "web")
    if os.path.isdir(console):
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=console, html=True), name="console")

    return app
