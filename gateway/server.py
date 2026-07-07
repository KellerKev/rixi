"""rixi gateway — brokered multi-agent reverse-tunnel rendezvous.

Servers and clients dial OUT to the gateway over the open tunnel wire format (protocol.py) and are
identified by the `node_id` they send at auth; a `role` field in the auth frame distinguishes them
(servers via the unmodified open `rixi-tunnel connect` omit it → default "server").

  • server (role default): registered + given a dedicated local TCP port (direct ops); its sessions
    are also bridgeable to clients.
  • client (role="client", from gateway/client.py): sends control ops and routed `session_open`
    frames; the gateway BRIDGES each to a chosen server's tunnel — neither side exposes inbound.

Control ops (over the encrypted channel): list · route · request_compute · release · teardown ·
list_resources. request_compute either mints a one-time claim token (ephemeral) or — given a
`resource` name from the gateway-side catalog (config.py) — provisions/reuses a named box per its
lifecycle policy (reuse-if-up, auto-teardown on idle/ttl).
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import os
import time
import uuid

import websockets

from . import audit as audit_mod
from . import offerings
from .auth import ANON
from .claims import Claims
from .config import load_catalog, load_policy
from .policy import GlobalPolicy, PolicyEngine, UsageView
from .protocol import AgentConn, dec, derive_keys, enc, proof
from .registry import RegisteredNode, Registry


class Gateway:
    def __init__(self, secret: str, ws_host: str = "0.0.0.0", ws_port: int = 7100,
                 local_host: str = "127.0.0.1", public_ws_url: str | None = None,
                 config_path: str | None = None, reap_interval: float = 60.0,
                 verifier=None, policy=None, auditor=None,
                 admin_host: str = "127.0.0.1", admin_port: int = 0, kdf_salt: str = ""):
        self.secret = secret
        self.kdf_salt = kdf_salt            # per-deployment KDF salt (must reach provisioned boxes)
        self.key, self.proof_key = derive_keys(secret, kdf_salt)
        self.ws_host = ws_host
        self.ws_port = ws_port
        self.local_host = local_host
        # URL a provisioned box should dial back to (defaults derived from ws_host:ws_port).
        self.public_ws_url = public_ws_url
        self.registry = Registry()
        self.claims = Claims()
        # brokered routing: (id(conn), sid) -> (peer_conn, peer_sid)
        self._bridges: dict = {}
        # named resource catalog + live state (config.py)
        self.catalog = load_catalog(config_path) if config_path else {}
        self.resources: dict = {}   # name -> {workdir, created_at, last_active, status, waiters}
        self._spent_eur = 0.0        # cumulative estimated spend (torn-down boxes), for budget caps
        self.reap_interval = reap_interval
        # control plane: identity (JWT), policy (admin floor), audit. All None-safe.
        glob = load_policy(config_path) if config_path else GlobalPolicy()
        self.policy = policy if policy is not None else PolicyEngine(glob, self.catalog)
        self.verifier = verifier            # JwtVerifier | None
        self.auditor = auditor              # audit.Auditor | None
        self.admin_host = admin_host
        self.admin_port = admin_port        # 0 = no management API
        self._provision_count: dict = {}    # identity -> lifetime fresh-box provisions (quota)
        if self.catalog:
            print(f"📚 resource catalog: {', '.join(sorted(self.catalog))}", flush=True)
        if getattr(self.policy.glob, "enabled", False):
            print(f"🛡️  policy active (require_jwt={self.policy.glob.require_jwt}, "
                  f"require_e2e={self.policy.glob.require_e2e})", flush=True)

    # -- bridge helpers --------------------------------------------------
    def _bridge(self, a_conn, a_sid, b_conn, b_sid):
        self._bridges[(id(a_conn), a_sid)] = (b_conn, b_sid)
        self._bridges[(id(b_conn), b_sid)] = (a_conn, a_sid)

    def _peer(self, conn, sid):
        return self._bridges.get((id(conn), sid))

    async def _unbridge(self, conn, sid):
        peer = self._bridges.pop((id(conn), sid), None)
        if peer is not None:
            pc, ps = peer
            self._bridges.pop((id(pc), ps), None)
            try:
                await pc.send({"type": "session_close", "sid": ps})
            except Exception:
                pass

    async def _on_session_data(self, conn, obj):
        """Relay to a bridged peer if any; else fall back to the per-node-port local writer."""
        peer = self._peer(conn, obj["sid"])
        if peer is not None:
            pc, ps = peer
            try:
                await pc.send({"type": "session_data", "sid": ps, "data": obj["data"]})
            except Exception:
                await self._unbridge(conn, obj["sid"])
        else:
            await conn.on_session_data(obj["sid"], obj["data"])

    async def _on_session_close(self, conn, sid):
        if (id(conn), sid) in self._bridges:
            await self._unbridge(conn, sid)
        else:
            await conn.close_session(sid, notify=False)

    # -- audit / usage helpers -------------------------------------------
    async def _audit(self, event, actor, action, **kw):
        if self.auditor is not None:
            try:
                await self.auditor.record(event, actor, action, **kw)
            except Exception:
                pass

    def _usage(self, exclude_resource: str = None) -> UsageView:
        """Live usage derived from the bridge table (per-identity concurrency) + provision counts,
        plus the live fleet burn rate and cumulative spend for budget caps. `exclude_resource`
        drops one resource from the burn sum so re-requesting an already-up box isn't double-counted."""
        per_id: dict = {}
        for node in self.registry.list("client"):
            ident = getattr(node.conn, "identity", ANON)
            actor = ident.sub or ident.label
            cnt = sum(1 for k in self._bridges if k[0] == id(node.conn))
            per_id[actor] = per_id.get(actor, 0) + cnt
        # Fleet burn = sum of active boxes' hourly rates; cumulative spend = torn-down cost so far
        # + the running cost of still-active boxes.
        now = time.time()
        burn, running = 0.0, 0.0
        for name, rdef in self.catalog.items():
            st = self.resources.get(name, {})
            if st.get("status") != "active":
                continue
            rate = offerings.price_per_hour(rdef.provider, rdef.vars.get("instance_type")) or 0.0
            if name != exclude_resource:
                burn += rate
            created = st.get("created_at")
            if created:
                running += rate * (now - created) / 3600.0
        return UsageView(per_identity_concurrent=per_id,
                         total_provisions=dict(self._provision_count),
                         fleet_eur_per_hour=round(burn, 6),
                         total_eur_spent=round(self._spent_eur + running, 6))

    # -- connection entrypoint -------------------------------------------
    async def _on_agent(self, ws):
        nonce = os.urandom(16).hex()
        await ws.send(enc({"type": "challenge", "nonce": nonce}, self.key))
        try:
            reply = dec(await asyncio.wait_for(ws.recv(), timeout=10), self.key)
        except Exception:
            return
        if reply.get("type") != "auth" or not hmac.compare_digest(
                reply.get("proof", ""), proof(self.proof_key, nonce)):
            return

        node_id = str(reply.get("node_id") or uuid.uuid4().hex[:8])
        role = reply.get("role", "server")
        conn = AgentConn(ws, self.key)
        conn.identity = ANON

        if role == "client":
            # JWT RBAC applies to CLIENTS only; server agents auth by PSK + their claim token.
            identity = ANON
            token = reply.get("token")
            if self.verifier is not None and token:
                identity = await self.verifier.verify(token) or ANON
            if self.policy.glob.require_jwt and identity.anon and not self.policy.is_admin(identity):
                await self._audit(audit_mod.AUTH_DENIED, identity.label, "connect",
                                  decision="deny", reason="jwt required", attrs={"node_id": node_id})
                return
            conn.identity = identity
            await self._audit(audit_mod.AUTH_OK, identity.sub or node_id, "connect", decision="allow",
                              attrs={"role": self.policy.role_of(identity), "node_id": node_id})
            await self._serve_client(ws, conn, node_id, identity)
        else:
            await self._serve_server(ws, conn, node_id, reply)

    # -- server role -----------------------------------------------------
    async def _serve_server(self, ws, conn, node_id, reply):
        async def on_local(reader, writer):  # direct per-node-port access (not the brokered path)
            sid = uuid.uuid4().hex
            conn.sessions[sid] = writer
            try:
                await conn.send({"type": "session_open", "sid": sid})
            except Exception:
                writer.close()
                conn.sessions.pop(sid, None)
                return
            await conn.pump_tcp_to_ws(sid, reader)

        local = await asyncio.start_server(on_local, self.local_host, 0)
        port = local.sockets[0].getsockname()[1]
        self.registry.register(RegisteredNode(node_id=node_id, kind="server", identity=node_id,
                                              conn=conn, port=port,
                                              capabilities=reply.get("capabilities", []) or []))
        print(f"✅ registered server '{node_id}' → {self.local_host}:{port}", flush=True)
        # If this node_id is a pending claim token, redeem it; if it's a named resource, signal
        # any clients waiting on it.
        await self._redeem_claim(node_id)
        await self._on_resource_up(node_id)

        try:
            async for raw in ws:
                obj = dec(raw, self.key)
                t = obj.get("type")
                if t == "session_data":
                    await self._on_session_data(conn, obj)
                elif t == "session_close":
                    await self._on_session_close(conn, obj["sid"])
        except Exception:
            pass
        finally:
            await conn.close_all()
            local.close()
            self.registry.unregister(node_id)
            print(f"❌ deregistered server '{node_id}'", flush=True)
            await self._on_server_gone(node_id)

    async def _redeem_claim(self, token):
        claim = self.claims.redeem(token, token)
        if claim is None:
            return
        await self._signal_ready(claim.client_id, token)
        print(f"🎟️  claim redeemed: server '{token}' wired to client '{claim.client_id}'", flush=True)

    # -- client role -----------------------------------------------------
    async def _serve_client(self, ws, conn, node_id, identity=ANON):
        self.registry.register(RegisteredNode(node_id=node_id, kind="client",
                                              identity=identity.sub or node_id, conn=conn,
                                              auth=identity))
        print(f"✅ registered client '{node_id}'"
              + (f" as '{identity.sub}'" if identity.sub else ""), flush=True)
        try:
            async for raw in ws:
                obj = dec(raw, self.key)
                t = obj.get("type")
                if t == "control":
                    await self._handle_control(conn, node_id, obj)
                elif t == "session_open":
                    await self._open_routed(conn, obj)
                elif t == "session_data":
                    await self._on_session_data(conn, obj)
                elif t == "session_close":
                    await self._on_session_close(conn, obj["sid"])
        except Exception:
            pass
        finally:
            # tear down any bridges this client owned
            for (cid, sid) in [k for k in self._bridges if k[0] == id(conn)]:
                await self._unbridge(conn, sid)
            self.registry.unregister(node_id)
            print(f"❌ deregistered client '{node_id}'", flush=True)

    async def _open_routed(self, client_conn, obj):
        sid = obj["sid"]
        route = str(obj.get("route", ""))
        target = self.registry.get(route)
        if target is None or target.kind != "server" or target.conn is None:
            await client_conn.send({"type": "session_close", "sid": sid})
            return
        # per-session policy re-check (deny here closes the session)
        identity = getattr(client_conn, "identity", ANON)
        name = self._resource_name_for(route)
        rdef = self.catalog.get(name) if name else None
        decision = self.policy.evaluate(identity, "route", resource=rdef, usage=self._usage())
        if not decision.allow:
            await self._audit(audit_mod.RES_DENIED, identity.sub or identity.label, "route",
                              target=route, decision="deny", reason=decision.reason)
            await client_conn.send({"type": "session_close", "sid": sid})
            return
        if name is not None:
            self._res_state(name)["last_active"] = time.time()
        server_sid = uuid.uuid4().hex
        self._bridge(client_conn, sid, target.conn, server_sid)
        await self._audit(audit_mod.SESSION_OPENED, identity.sub or identity.label, "route",
                          target=route, decision="allow")
        try:
            await target.conn.send({"type": "session_open", "sid": server_sid})
        except Exception:
            await self._unbridge(client_conn, sid)

    def _pol_resource(self, op, obj):
        """The catalog resource a policy decision is scoped to, per op (None if not resource-scoped)."""
        if op in ("request_compute", "teardown") and obj.get("resource"):
            return self.catalog.get(str(obj["resource"]))
        if op == "route":
            name = self._resource_name_for(str(obj.get("node_id", "")))
            return self.catalog.get(name) if name else None
        return None

    def _secure_config_error(self, rdef, security):
        """A resource can't satisfy an enforced security floor if it isn't configured for it."""
        if security is None or rdef is None:
            return None
        if security.require_e2e and not rdef.key_secret:
            return f"resource '{rdef.name}' requires end-to-end encryption but has no key_secret configured"
        if security.require_jwt and not (rdef.jwt_public_key or rdef.jwt_jwks_url):
            return f"resource '{rdef.name}' requires jwt but has no jwt_public_key/jwt_jwks_url configured"
        return None

    async def _handle_control(self, conn, client_id, obj):
        op = obj.get("op")
        identity = getattr(conn, "identity", ANON)
        actor = identity.sub or client_id
        try:
            if op not in ("list", "route", "request_compute", "teardown", "list_resources", "release"):
                await conn.send({"type": "control", "op": op, "error": f"unknown op: {op}"})
                return

            # Capability-based selection: resolve `select` criteria to the cheapest matching
            # catalog resource, then treat it as a normal by-name request from here on.
            if op == "request_compute" and obj.get("select") and not obj.get("resource"):
                from .scheduler import select_resource
                chosen = select_resource(self.catalog, obj["select"])
                if not chosen:
                    await self._audit(audit_mod.RES_DENIED, actor, op, target="select",
                                      decision="deny", reason="no resource matches capabilities")
                    await conn.send({"type": "control", "op": op,
                                     "error": "no catalog resource matches the requested capabilities"})
                    return
                obj["resource"] = chosen

            # policy gate (admin floor; allow-all when no [policy] table is configured)
            rdef = self._pol_resource(op, obj)
            request = dict(obj.get("tighten") or {})
            if op == "request_compute" and not obj.get("resource"):
                request["provider"] = obj.get("provider")
            # For budget caps, don't count the requested resource's own rate in the fleet burn
            # (a reused, already-up box would otherwise be double-counted against the cap).
            usage = self._usage(exclude_resource=obj.get("resource") if op == "request_compute" else None)
            decision = self.policy.evaluate(identity, op, resource=rdef, request=request, usage=usage)
            if not decision.allow:
                await self._audit(audit_mod.RES_DENIED, actor, op, target=obj.get("resource"),
                                  decision="deny", reason=decision.reason)
                await conn.send({"type": "control", "op": op, "error": decision.reason})
                return

            if op == "list":
                result = {"nodes": self.list_nodes()}
            elif op == "route":
                node = self.registry.get(str(obj.get("node_id", "")))
                result = {"node_id": obj.get("node_id"), "exists": node is not None and node.kind == "server"}
            elif op == "request_compute":
                rname = obj.get("resource")
                if rname:
                    err = self._secure_config_error(rdef, decision.security)
                    if err:
                        await self._audit(audit_mod.RES_DENIED, actor, op, target=rname,
                                          decision="deny", reason=err)
                        await conn.send({"type": "control", "op": op, "error": err})
                        return
                    result = await self._request_resource(client_id, str(rname), decision.security)
                    if not result.get("reused"):
                        self._provision_count[actor] = self._provision_count.get(actor, 0) + 1
                    await self._audit(audit_mod.RES_REQUESTED, actor, op, target=rname,
                                      decision="allow", attrs={"reused": result.get("reused")})
                else:
                    claim = self.claims.create(client_id, str(obj.get("provider", "dummy")),
                                               obj.get("spec", {}) or {})
                    result = {"token": claim.token, "node_id": claim.token, "ttl": claim.ttl}
                    self._provision_count[actor] = self._provision_count.get(actor, 0) + 1
                    asyncio.create_task(self._provision(claim))
                    await self._audit(audit_mod.RES_REQUESTED, actor, op, target=claim.provider,
                                      decision="allow")
            elif op == "teardown":
                # _teardown_resource emits the TEARDOWN audit event (with cost) for both this
                # path and the reaper, so it isn't double-logged here.
                result = await self._teardown_resource(str(obj.get("resource", "")), actor=actor)
            elif op == "list_resources":
                result = {"resources": self.list_resources()}
            elif op == "release":
                claim = self.claims.release(str(obj.get("token", "")))
                if claim is not None:
                    asyncio.create_task(self._deprovision(claim))
                result = {"released": claim is not None}
            await conn.send({"type": "control", "op": op, "result": result})
        except Exception as e:  # noqa: BLE001 - report control errors over the wire
            await conn.send({"type": "control", "op": op, "error": str(e)})

    # -- named resources -------------------------------------------------
    def _res_state(self, name: str) -> dict:
        return self.resources.setdefault(name, {
            "workdir": None, "created_at": None, "last_active": time.time(),
            "status": "idle", "waiters": set(), "provisioning": False})

    def _resource_name_for(self, node_id: str):
        """Return the catalog name for a stable reuse node_id (res-<name>), else None."""
        if not node_id.startswith("res-"):
            return None
        name = node_id[4:]
        rdef = self.catalog.get(name)
        return name if (rdef is not None and rdef.reuse) else None

    async def _signal_ready(self, client_id: str, node_id: str):
        client = self.registry.get(client_id)
        if client is not None and client.conn is not None:
            try:
                await client.conn.send({"type": "control", "op": "compute_ready", "node_id": node_id})
            except Exception:
                pass

    async def _request_resource(self, client_id: str, rname: str, security=None) -> dict:
        rdef = self.catalog.get(rname)
        if rdef is None:
            raise RuntimeError(f"unknown resource: {rname}")
        if not rdef.reuse:
            # always-fresh: ride the ephemeral one-time-token claim flow, but resource-driven.
            claim = self.claims.create(client_id, rdef.provider, {})
            claim.resource = rdef
            claim.security = security
            asyncio.create_task(self._provision(claim))
            return {"node_id": claim.token, "token": claim.token, "ttl": claim.ttl, "reused": False}

        node_id = rdef.node_id
        existing = self.registry.get(node_id)
        st = self._res_state(rname)
        if existing is not None and existing.kind == "server" and existing.conn is not None:
            st["last_active"] = time.time()
            st["status"] = "active"
            asyncio.create_task(self._signal_ready(client_id, node_id))
            return {"node_id": node_id, "reused": True}
        # bring it up (or wait for it to reconnect); only one provision in flight per resource.
        st["waiters"].add(client_id)
        if not st["provisioning"]:
            st["provisioning"] = True
            st["status"] = "provisioning"
            asyncio.create_task(self._provision_resource(rdef, security))
        return {"node_id": node_id, "reused": False}

    async def _on_server_gone(self, node_id: str):
        """A box's tunnel dropped. For a spot resource that wasn't intentionally torn down, treat it
        as a preemption: audit it and re-provision (self-heal, riding the capacity-retry loop, which
        falls back to on-demand if spot capacity is gone)."""
        try:
            name = self._resource_name_for(node_id)
            if name is None:
                return
            rdef = self.catalog.get(name)
            st = self.resources.get(name)
            if rdef is None or not getattr(rdef, "spot", False):
                return
            if st is None or st.get("tearing_down"):
                return  # an intentional teardown, not a preemption
            await self._audit(audit_mod.SPOT_INTERRUPTED, "gateway", "preempted", target=name,
                              decision="n/a", reason="tunnel_dropped")
            print(f"⚡ spot box '{name}' preempted; re-provisioning", flush=True)
            self._prewarm(rdef)   # provision a reusable box proactively (guards against dup provisions)
        except Exception:  # noqa: BLE001 - never let self-heal break the disconnect path
            pass

    async def _provision_resource(self, rdef, security=None):
        st = self._res_state(rdef.name)
        try:
            out = await self.fire_action("provision", {"resource": rdef, "security": security})
            st["workdir"] = out.get("workdir")
            if st["created_at"] is None:
                st["created_at"] = time.time()
        except Exception as e:  # noqa: BLE001
            st["status"] = "failed"
            st["provisioning"] = False
            print(f"⚠️  provision failed for resource '{rdef.name}': {e}", flush=True)

    async def _on_resource_up(self, node_id: str):
        name = self._resource_name_for(node_id)
        if name is None:
            return
        st = self._res_state(name)
        if st["created_at"] is None:
            st["created_at"] = time.time()
        st["status"] = "active"
        st["last_active"] = time.time()
        st["provisioning"] = False
        rdef = self.catalog.get(name)
        # A reused box registers DURING `tofu apply`'s local-exec — before apply returns and sets
        # st["workdir"]. Its workdir is deterministic, so capture it now; otherwise a teardown that
        # races in before apply returns would find no workdir and skip the destroy.
        if rdef is not None and rdef.reuse and not st.get("workdir"):
            from .actions.provision import _resource_workdir
            st["workdir"] = str(_resource_workdir(name))
        if rdef is not None:
            itype = rdef.vars.get("instance_type")
            await self._audit(audit_mod.RES_PROVISIONED, "system", "provision", target=name,
                              decision="allow",
                              attrs={"provider": rdef.provider, "instance_type": itype,
                                     "rate_per_min": offerings.price_per_min(rdef.provider, itype),
                                     "currency": offerings.currency()})
        waiters, st["waiters"] = st["waiters"], set()
        for cid in waiters:
            await self._signal_ready(cid, node_id)

    def _resource_busy(self, node_id: str) -> bool:
        node = self.registry.get(node_id)
        if node is None or node.conn is None:
            return False
        cid = id(node.conn)
        return any(k[0] == cid for k in self._bridges)

    async def _teardown_resource(self, rname: str, actor: str = "system") -> dict:
        rdef = self.catalog.get(rname)
        st = self.resources.get(rname)
        workdir = st.get("workdir") if st else None
        if rdef is None or not workdir:
            if st is not None:
                self.resources.pop(rname, None)
            return {"destroyed": False}
        # Mark it so the disconnect handler treats the imminent tunnel drop (from `tofu destroy`) as
        # an intentional teardown, not a spot preemption to self-heal.
        st["tearing_down"] = True
        # Estimate the cost accrued over the box's lifetime before we drop its state.
        created = st.get("created_at")
        elapsed_min = (time.time() - created) / 60.0 if created else 0.0
        cost = offerings.cost_fields(rdef.provider, rdef.vars.get("instance_type"), elapsed_min)
        if cost.get("est_cost"):
            self._spent_eur += cost["est_cost"]
        await self.fire_action("deprovision", {"resource": rdef, "workdir": workdir})
        self.resources.pop(rname, None)
        print(f"🧹 tore down resource '{rname}' "
              f"(~{cost.get('est_cost')} {cost.get('currency')})", flush=True)
        await self._audit(audit_mod.TEARDOWN, actor, "teardown", target=rname,
                          decision="allow", attrs={"destroyed": True, **cost})
        return {"destroyed": True, "cost": cost}

    def list_resources(self):
        out = []
        now = time.time()
        for name, rdef in sorted(self.catalog.items()):
            st = self.resources.get(name, {})
            up = self.registry.get(rdef.node_id) is not None if rdef.reuse else False
            itype = rdef.vars.get("instance_type")
            rpm = offerings.price_per_min(rdef.provider, itype)
            created = st.get("created_at")
            est_so_far = (round(rpm * (now - created) / 60.0, 6)
                          if rpm is not None and created and st.get("status") == "active" else None)
            out.append({"name": name, "provider": rdef.provider, "instance_type": itype,
                        "reuse": rdef.reuse, "teardown": rdef.teardown,
                        "status": st.get("status", "idle"), "up": up,
                        "rate_per_min": rpm, "currency": offerings.currency(),
                        "est_cost_so_far": est_so_far})
        return out

    # -- warm pool (pre-warm) --------------------------------------------
    def _prewarm(self, rdef) -> None:
        """Provision a reusable box proactively (no client waiter) so it's ready on first request."""
        if not rdef.reuse or self.registry.get(rdef.node_id) is not None:
            return
        st = self._res_state(rdef.name)
        if st.get("provisioning"):
            return
        st["provisioning"] = True
        st["status"] = "provisioning"
        print(f"🔥 pre-warming '{rdef.name}'", flush=True)
        asyncio.create_task(self._provision_resource(rdef))

    def _prewarm_all(self) -> None:
        for rdef in self.catalog.values():
            if rdef.prewarm:
                self._prewarm(rdef)

    # -- auto-teardown reaper --------------------------------------------
    async def _reap_once(self):
        now = time.time()
        for name, rdef in list(self.catalog.items()):
            st = self.resources.get(name)
            if not st or st.get("status") != "active" or not st.get("workdir"):
                continue
            destroy = False
            if rdef.teardown == "ttl" and rdef.max_age and st["created_at"]:
                destroy = (now - st["created_at"]) > rdef.max_age
            elif rdef.teardown == "idle" and rdef.idle_timeout and not rdef.prewarm:
                destroy = (not self._resource_busy(rdef.node_id)
                           and (now - st["last_active"]) > rdef.idle_timeout)
            if destroy:
                print(f"🧹 auto-teardown '{name}' (teardown={rdef.teardown})", flush=True)
                await self._teardown_resource(name, actor="reaper")
        # keep pre-warmed resources up (re-provision after a ttl recycle or an unexpected drop)
        for rdef in self.catalog.values():
            if rdef.prewarm and self.registry.get(rdef.node_id) is None:
                self._prewarm(rdef)

    async def _reaper(self):
        while True:
            await asyncio.sleep(self.reap_interval)
            try:
                await self._reap_once()
            except Exception:  # noqa: BLE001
                pass

    async def _provision(self, claim):
        try:
            rdef = getattr(claim, "resource", None)
            args = {"claim": claim}
            if rdef is not None:
                args["resource"] = rdef
                args["security"] = getattr(claim, "security", None)
            else:
                args["provider"] = claim.provider
                args["spec"] = claim.spec
            await self.fire_action("provision", args)
        except Exception as e:  # noqa: BLE001
            claim.status = "failed"
            print(f"⚠️  provision failed for claim {claim.token[:12]}…: {e}", flush=True)

    async def _deprovision(self, claim):
        try:
            rdef = getattr(claim, "resource", None)
            args = {"claim": claim}
            if rdef is not None:
                args["resource"] = rdef
                args["workdir"] = claim.workdir
            await self.fire_action("deprovision", args)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  deprovision failed for claim {claim.token[:12]}…: {e}", flush=True)

    def reload_policy(self, global_dict: dict):
        """Atomically swap the global policy (admin API). Affects new requests/sessions only."""
        from .config import parse_global_policy
        self.policy = PolicyEngine(parse_global_policy(global_dict or {}), self.catalog)

    # -- lifecycle / control surface -------------------------------------
    async def serve(self):
        if self.verifier is not None:
            await self.verifier.start()
        if self.auditor is not None:
            await self.auditor.start()
        ws_server = await websockets.serve(self._on_agent, self.ws_host, self.ws_port,
                                           ping_interval=20, ping_timeout=20)
        print(f"🛰️  rixi gateway on ws://{self.ws_host}:{self.ws_port}", flush=True)
        self._prewarm_all()   # provision any prewarm resources proactively (no cold start)
        reaper = asyncio.create_task(self._reaper())
        api_server = api_task = None
        if self.admin_port:
            import uvicorn

            from .management import build_app
            cfg = uvicorn.Config(build_app(self), host=self.admin_host, port=self.admin_port,
                                 log_level="warning")
            api_server = uvicorn.Server(cfg)
            api_task = asyncio.create_task(api_server.serve())
            print(f"🖥️  management API on http://{self.admin_host}:{self.admin_port}", flush=True)
        try:
            async with ws_server:
                if api_task is not None:
                    await asyncio.gather(ws_server.wait_closed(), api_task)
                else:
                    await ws_server.wait_closed()
        finally:
            reaper.cancel()
            if api_server is not None:
                api_server.should_exit = True
            if self.verifier is not None:
                await self.verifier.stop()
            if self.auditor is not None:
                await self.auditor.close()

    def list_nodes(self):
        return [{"node_id": n.node_id, "kind": n.kind, "port": n.port,
                 "capabilities": n.capabilities} for n in self.registry.list()]

    async def fire_action(self, name: str, args: dict):
        from . import actions
        return await actions.dispatch(name, args, self)


def main():
    ap = argparse.ArgumentParser(description="rixi gateway (brokered reverse-tunnel rendezvous)")
    ap.add_argument("--ws-bind", default="0.0.0.0:7100", help="address servers/clients dial into")
    ap.add_argument("--public-ws-url", default=os.getenv("RIXI_GATEWAY_PUBLIC_URL"),
                    help="ws:// URL provisioned boxes dial back (must be reachable from them; "
                         "e.g. ws://host.docker.internal:7100 for k8s pods). Defaults to --ws-bind.")
    ap.add_argument("--kdf-salt", default=os.getenv("RIXI_GATEWAY_SALT", ""), help="per-deployment KDF salt (or RIXI_GATEWAY_SALT); must match peers")
    ap.add_argument("--secret", default=os.getenv("RIXI_GATEWAY_SECRET"),
                    help="shared tunnel secret (or RIXI_GATEWAY_SECRET)")
    ap.add_argument("--config", default=os.getenv("RIXI_GATEWAY_CONFIG", "rixi.toml"),
                    help="resource catalog + [policy] TOML (default rixi.toml; ignored if missing)")
    ap.add_argument("--jwt-public-key", default=os.getenv("RIXI_JWT_PUBLIC_KEY"),
                    help="PEM (file or text) to verify client JWTs for RBAC")
    ap.add_argument("--jwt-jwks-url", default=os.getenv("RIXI_JWT_JWKS_URL"),
                    help="JWKS URL to verify client JWTs for RBAC")
    ap.add_argument("--admin-bind", default=os.getenv("RIXI_ADMIN_BIND", ""),
                    help="management API host:port (e.g. 127.0.0.1:7101); empty = off")
    ap.add_argument("--audit-db", default=os.getenv("RIXI_AUDIT_DB"), help="DuckDB audit file")
    ap.add_argument("--audit-log", default=os.getenv("RIXI_AUDIT_LOG"), help="JSON audit log file")
    ap.add_argument("--otlp-endpoint", default=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
                    help="OTLP logs endpoint for audit export")
    args = ap.parse_args()
    if not args.secret:
        ap.error("a --secret (or RIXI_GATEWAY_SECRET) is required")
    host, _, port = args.ws_bind.rpartition(":")

    verifier = None
    pub = args.jwt_public_key
    if pub and os.path.exists(pub):
        pub = open(pub).read()
    if pub or args.jwt_jwks_url:
        from .auth import JwtVerifier
        verifier = JwtVerifier(public_key_pem=pub, jwks_url=args.jwt_jwks_url)

    from .audit import build_auditor
    auditor = build_auditor(json_path=args.audit_log, duckdb_path=args.audit_db,
                            otlp_endpoint=args.otlp_endpoint)

    admin_host, admin_port = "127.0.0.1", 0
    if args.admin_bind:
        ah, _, ap_ = args.admin_bind.rpartition(":")
        admin_host, admin_port = ah or "127.0.0.1", int(ap_)

    gw = Gateway(args.secret, host or "0.0.0.0", int(port), config_path=args.config, kdf_salt=args.kdf_salt,
                 public_ws_url=args.public_ws_url,
                 verifier=verifier, auditor=auditor, admin_host=admin_host, admin_port=admin_port)
    try:
        asyncio.run(gw.serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
