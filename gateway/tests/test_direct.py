# Direct mode: tenancy, durable state, reaper, reconciler, heartbeat, EU floor, billing hooks.
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytest.importorskip("websaw_ng")
import asyncio  # noqa: E402
import tempfile  # noqa: E402

import httpx  # noqa: E402
import jwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from gateway.auth import JwtVerifier  # noqa: E402
from gateway.direct import cloudinit  # noqa: E402
from gateway.direct.web import asgi, build_app  # noqa: E402
from gateway.direct.config import ConfigError, parse  # noqa: E402
from gateway.direct.providers import DummyDns, DummyProvider  # noqa: E402
from gateway.direct.service import Caller, Denied, DirectService, NotFound  # noqa: E402
from gateway.direct.store import Store  # noqa: E402


def _conf(**direct):
    base = {
        "box_domain": "run.example.test",
        "heartbeat_url": "https://gw.example.test/gw/heartbeat",
        "box_jwks_url": "https://portal.example.test/jwks.json",
        "rixi_ref": "v9.9.9",
        "jwt_audience": "rixi-gateway",
        "allowed_regions": ["fr-par-2", "pl-waw-2"],
        "limits": {"max_boxes": 2, "max_eur_per_hour": 2.0, "default_ttl": "1h",
                   "max_ttl": "4h", "idle_timeout": "10m"},
    }
    base.update(direct)
    return {"direct": base,
            "template": {"cpu": {"provider": "dummy", "type": "DEV1-S",
                                 "zones": ["fr-par-2", "pl-waw-2"], "eur_per_hour": 0.5},
                         "gpu": {"provider": "dummy", "type": "L4-1-24G",
                                 "zones": ["fr-par-2"], "eur_per_hour": 1.5}}}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def _svc(tmp_path=None, store=None, provider=None, clock=None, authorize=None, **direct):
    cfg = parse(_conf(**direct))
    # A file, never :memory: — the store keeps one connection per thread.
    folder = tmp_path or tempfile.mkdtemp(prefix="rixi-direct-")
    store = store or Store(f"sqlite:///{os.path.join(str(folder), 'd.db')}")
    provider = provider or DummyProvider()
    events = []
    svc = DirectService(cfg, store, {"dummy": provider}, DummyDns(), authorize=authorize,
                        audit=lambda e, **a: events.append((e, a)), clock=clock or Clock(),
                        workers=2)
    svc.events = events
    return svc


def _settle(svc):
    """Wait for background provision/destroy jobs."""
    deadline = time.time() + 5
    while time.time() < deadline:
        with svc._lock:
            if not svc._inflight:
                return
        time.sleep(0.01)
    raise AssertionError("background jobs did not finish")


A = Caller(sub="alice", tenant="t-a")
B = Caller(sub="bob", tenant="t-b")
ADMIN = Caller(sub="portal", tenant=None, admin=True)


def _secret_of(svc, box_id):
    """The heartbeat secret only exists inside the rendered user-data."""
    for srv in svc.providers["dummy"].servers.values():
        if f"rixi-box={box_id}" in srv["tags"]:
            for line in srv["user_data"].split("\\n"):
                if line.startswith("RIXI_BOX_SECRET="):
                    return line.split("=", 1)[1]
    raise AssertionError("no user-data for box")


# ── config ──────────────────────────────────────────────────────────────

def test_config_refuses_template_outside_eu_floor():
    c = _conf()
    c["template"]["us"] = {"provider": "dummy", "type": "x", "zones": ["us-east-1"]}
    with pytest.raises(ConfigError, match="outside allowed_regions"):
        parse(c)


def test_config_requires_api_audience():
    with pytest.raises(ConfigError, match="jwt_audience"):
        parse(_conf(jwt_audience=None))


def test_api_refuses_box_tokens():
    tc, svc, h = _api()
    r = tc.post("/api/boxes", json={"template": "cpu"}, headers=h("alice", "t-a", aud="box-x"))
    assert r.status_code == 401


def test_config_refuses_unpinned_ref():
    with pytest.raises(ConfigError, match="pinned"):
        parse(_conf(rixi_ref="main"))
    assert parse(_conf(rixi_ref="main", allow_unpinned_ref=True)).rixi_ref == "main"


# ── claim + provisioning ────────────────────────────────────────────────

def test_claim_provisions_box_with_dns_and_scoped_user_data():
    svc = _svc()
    box = svc.claim(A, "cpu")
    _settle(svc)
    cur = svc.store.get(box.id)
    assert cur.state == "booting" and cur.ip and cur.provider_id
    assert svc.dns.records[cur.hostname] == cur.ip
    assert cur.hostname == f"b-{box.id}.run.example.test"
    ud = next(iter(svc.providers["dummy"].servers.values()))["user_data"]
    assert f"RIXI_BOX_ID={box.id}" in ud and "RIXI_TENANT=t-a" in ud
    assert "v9.9.9/box/bootstrap-direct.sh" in ud
    assert "tunnel_secret" not in ud.lower() and "RIXI_TUNNEL_SECRET" not in ud
    assert cur.hb_hash and _secret_of(svc, box.id) not in cur.hb_hash   # stored hashed only


def test_claim_refuses_zone_outside_template_and_floor():
    svc = _svc()
    with pytest.raises(Denied):
        svc.claim(A, "gpu", zone="pl-waw-2")         # template does not run there
    with pytest.raises(Denied):
        svc.claim(A, "cpu", zone="us-east-1")


def test_claim_needs_tenant_claim():
    svc = _svc()
    with pytest.raises(Denied):
        svc.claim(Caller(sub="x", tenant=None), "cpu")
    with pytest.raises(Denied):
        svc.claim(Caller(sub="x", tenant="bad tenant!"), "cpu")


def test_tenant_limits_count_boxes_and_spend():
    svc = _svc()
    svc.claim(A, "cpu")
    svc.claim(A, "cpu")
    with pytest.raises(Denied, match="box limit"):
        svc.claim(A, "cpu")
    svc.claim(B, "gpu")                               # other tenant unaffected: 1.5 €/h
    with pytest.raises(Denied, match="spend limit"):
        svc.claim(B, "gpu")                           # 3.0 > 2.0 €/h
    svc.claim(B, "cpu")                               # 2.0 == cap is fine
    _settle(svc)


def test_ttl_above_max_refused():
    svc = _svc()
    with pytest.raises(Denied):
        svc.claim(A, "cpu", ttl=5 * 3600)


def test_authorizer_denial_blocks_claim_before_anything_exists():
    svc = _svc(authorize=lambda p: (p["eur_per_hour"] < 1.0, "insufficient balance"))
    svc.claim(A, "cpu")
    with pytest.raises(Denied, match="insufficient balance"):
        svc.claim(A, "gpu")
    _settle(svc)
    assert len(svc.store.list()) == 1
    assert any(e == "box.denied" for e, _ in svc.events)


def test_failed_create_cleans_up_and_ends_failed():
    svc = _svc(provider=DummyProvider(fail_create=True))
    box = svc.claim(A, "cpu")
    _settle(svc)
    _settle(svc)
    cur = svc.store.get(box.id)
    assert cur.state == "failed" and cur.end_reason == "provision_failed" and cur.ended_at
    assert cur.hostname not in svc.dns.records


# ── tenancy ─────────────────────────────────────────────────────────────

def test_other_tenant_cannot_see_release_or_revoke():
    svc = _svc()
    box = svc.claim(A, "cpu")
    _settle(svc)
    with pytest.raises(NotFound):
        svc.get(B, box.id)
    with pytest.raises(NotFound):
        svc.release(box.id, "released", caller=B)
    with pytest.raises(NotFound):
        svc.revoke(B, box.id, "j1")
    assert svc.list(B) == []
    assert [b.id for b in svc.list(A)] == [box.id]
    assert svc.usage(B, 0) == []
    assert svc.store.get(box.id).live


def test_admin_sees_all_and_stops_a_tenant():
    svc = _svc()
    a1 = svc.claim(A, "cpu")
    b1 = svc.claim(B, "cpu")
    _settle(svc)
    assert {b.id for b in svc.list(ADMIN)} == {a1.id, b1.id}
    svc.stop_tenant("t-a")
    _settle(svc)
    assert svc.store.get(a1.id).state == "gone"
    assert svc.store.get(a1.id).end_reason == "tenant_stopped"
    assert svc.store.get(b1.id).live


# ── heartbeat ───────────────────────────────────────────────────────────

def test_heartbeat_needs_the_boxes_own_secret():
    svc = _svc()
    a = svc.claim(A, "cpu")
    b = svc.claim(B, "cpu")
    _settle(svc)
    with pytest.raises(Denied):
        svc.heartbeat(a.id, _secret_of(svc, b.id), 0, True)     # another box's secret
    with pytest.raises(Denied):
        svc.heartbeat(a.id, "", 0, True)
    r = svc.heartbeat(a.id, _secret_of(svc, a.id), 0, True)
    assert r["state"] == "ready"


def test_heartbeat_carries_revocations():
    svc = _svc()
    a = svc.claim(A, "cpu")
    _settle(svc)
    svc.revoke(A, a.id, "tok-1")
    assert svc.heartbeat(a.id, _secret_of(svc, a.id), 0, False)["revoked_jti"] == ["tok-1"]


# ── reaper ──────────────────────────────────────────────────────────────

def _ready(svc, caller, clock, template="cpu", **kw):
    box = svc.claim(caller, template, **kw)
    _settle(svc)
    svc.heartbeat(box.id, _secret_of(svc, box.id), 0, True)
    return box


def test_reaper_ttl_idle_and_lost_heartbeat():
    clock = Clock()
    svc = _svc(clock=clock)
    ttl_box = _ready(svc, A, clock, ttl=300, idle_timeout=None)
    idle_box = _ready(svc, A, clock)                     # idle_timeout 10m from config
    busy_box = _ready(svc, B, clock)

    def beat(box, tasks):
        svc.heartbeat(box.id, _secret_of(svc, box.id), tasks, True)

    clock.t += 301
    beat(busy_box, 2)
    beat(idle_box, 0)
    svc.reap_once()
    _settle(svc)
    assert svc.store.get(ttl_box.id).end_reason == "ttl"
    assert svc.store.get(idle_box.id).live               # idle 301s < 600s
    clock.t += 300
    beat(busy_box, 1)
    beat(idle_box, 0)
    svc.reap_once()
    _settle(svc)
    assert svc.store.get(idle_box.id).end_reason == "idle"
    assert svc.store.get(busy_box.id).live               # running tasks → never idle
    clock.t += svc.cfg.heartbeat_timeout + 1              # busy box stops reporting
    svc.reap_once()
    _settle(svc)
    assert svc.store.get(busy_box.id).end_reason == "heartbeat_lost"


def test_reaper_boot_timeout():
    clock = Clock()
    svc = _svc(clock=clock)
    box = svc.claim(A, "cpu")
    _settle(svc)
    clock.t += svc.cfg.boot_timeout + 1
    svc.reap_once()
    _settle(svc)
    assert svc.store.get(box.id).end_reason == "boot_timeout"


# ── durability + reconciliation ─────────────────────────────────────────

def test_state_survives_restart_and_interrupted_create_is_cleaned(tmp_path):
    provider = DummyProvider()
    clock = Clock()
    svc1 = _svc(tmp_path, provider=provider, clock=clock)
    kept = svc1.claim(A, "cpu")
    _settle(svc1)
    svc1.heartbeat(kept.id, _secret_of(svc1, kept.id), 0, True)
    svc1.store.update(kept.id, expires_at=clock.t + 3000)
    # simulate a crash mid-create: a row stuck in provisioning with nothing in flight
    stuck = svc1.claim(A, "cpu")
    _settle(svc1)
    svc1.store.update(stuck.id, state="provisioning")
    svc1.stop()
    svc1.store.close()

    svc2 = _svc(tmp_path, store=Store(f"sqlite:///{tmp_path / 'd.db'}"), provider=provider,
                clock=clock)
    assert svc2.store.get(kept.id).state == "ready"          # nothing lost
    svc2.reap_once()
    _settle(svc2)
    assert svc2.store.get(stuck.id).end_reason == "interrupted"
    assert svc2.store.get(stuck.id).state == "gone"
    assert svc2.store.get(kept.id).live
    assert {s["tags"][1] for s in provider.servers.values()} == {f"rixi-box={kept.id}"}


def test_reconciler_destroys_orphans_and_stops_billing_vanished_boxes():
    provider = DummyProvider()
    clock = Clock()
    svc = _svc(provider=provider, clock=clock)
    live = _ready(svc, A, clock)
    gone = _ready(svc, A, clock)
    # an instance the store does not know (e.g. created by hand, or a lost row)
    provider.servers["srv-x"] = {"zone": "fr-par-2", "tags": ["rixi-managed", "rixi-box=ffff"],
                                 "user_data": "", "type": "DEV1-S"}
    # a box the cloud lost (deleted in the console)
    for sid in [s for s, v in provider.servers.items() if f"rixi-box={gone.id}" in v["tags"]]:
        del provider.servers[sid]
    report = svc.reconcile_once()
    assert report["orphans_destroyed"] == ["ffff"] and "srv-x" not in provider.servers
    assert report["vanished"] == [gone.id]
    g = svc.store.get(gone.id)
    assert g.state == "gone" and g.end_reason == "vanished" and g.ended_at == clock.t
    assert svc.store.get(live.id).live


def test_usage_bills_from_creation_to_teardown():
    clock = Clock()
    svc = _svc(clock=clock)
    box = svc.claim(A, "gpu")
    _settle(svc)
    t0 = clock.t
    clock.t += 1800
    svc.release(box.id, "released", caller=A)
    _settle(svc)
    clock.t += 999
    (row,) = svc.usage(ADMIN, t0 - 1)
    assert row["tenant"] == "t-a" and row["eur_per_hour"] == 1.5
    assert row["minutes"] == 30.0 and row["ended_at"] == t0 + 1800
    assert svc.usage(A, t0 + 1801) == []                   # ended before `since`


# ── cloud-init ──────────────────────────────────────────────────────────

def test_cloudinit_rejects_injection():
    cfg = parse(_conf())
    with pytest.raises(ValueError):
        cloudinit.render(cfg, box_id="x", tenant="t\nRIXI_JWKS_URL=https://evil", hostname="h",
                         box_secret="s")
    with pytest.raises(ValueError):
        cloudinit.render(cfg, box_id="x", tenant="t", hostname="h", box_secret="s",
                         ssh_key="ssh-ed25519 AAAA\nruncmd: [evil]")
    ok = cloudinit.render(cfg, box_id="x", tenant="t", hostname="h", box_secret="s",
                          ssh_key="ssh-ed25519 AAAAC3Nza me@laptop")
    assert ok.startswith("#cloud-config\n") and "ssh_authorized_keys" in ok


# ── HTTP API ────────────────────────────────────────────────────────────

def _keys():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()
    pub = k.public_key().public_bytes(serialization.Encoding.PEM,
                                      serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub


class SyncClient:
    """Drive the ASGI app in-process with httpx, from sync tests."""

    def __init__(self, app):
        self.app = app

    def _req(self, method, url, **kw):
        async def go():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
                return await c.request(method, url, **kw)
        return asyncio.run(go())

    def get(self, url, **kw):
        return self._req("GET", url, **kw)

    def post(self, url, **kw):
        return self._req("POST", url, **kw)

    def delete(self, url, **kw):
        return self._req("DELETE", url, **kw)


def _api():
    priv, pub = _keys()
    svc = _svc()
    tc = SyncClient(asgi(build_app(svc, JwtVerifier(public_key_pem=pub,
                                                    audience="rixi-gateway"))))

    def h(sub, tenant=None, roles=(), aud="rixi-gateway"):
        claims = {"sub": sub, "roles": list(roles), "exp": int(time.time()) + 600, "aud": aud}
        if tenant:
            claims["tenant"] = tenant
        return {"Authorization": f"Bearer {jwt.encode(claims, priv, 'RS256')}"}
    return tc, svc, h


def test_api_requires_token_and_scopes_by_tenant():
    tc, svc, h = _api()
    assert tc.get("/api/boxes").status_code == 401
    assert tc.get("/api/boxes", headers={"Authorization": "Bearer junk"}).status_code == 401
    assert tc.get("/healthz").json() == {"ok": True}
    r = tc.post("/api/boxes", json={"template": "cpu"}, headers=h("alice", "t-a"))
    assert r.status_code == 202
    box = r.json()
    assert box["url"].startswith("https://b-") and "hb_hash" not in box
    _settle(svc)
    assert tc.get(f"/api/boxes/{box['id']}", headers=h("bob", "t-b")).status_code == 404
    assert tc.delete(f"/api/boxes/{box['id']}", headers=h("bob", "t-b")).status_code == 404
    assert tc.get("/api/boxes", headers=h("bob", "t-b")).json()["boxes"] == []
    assert tc.get("/api/boxes?tenant=t-a", headers=h("bob", "t-b")).json()["boxes"] == []
    assert len(tc.get("/api/boxes", headers=h("alice", "t-a")).json()["boxes"]) == 1


def test_api_admin_only_routes_and_heartbeat():
    tc, svc, h = _api()
    box = tc.post("/api/boxes", json={"template": "cpu"}, headers=h("alice", "t-a")).json()
    _settle(svc)
    assert tc.post("/api/tenants/t-a/stop", headers=h("alice", "t-a")).status_code == 403
    hb = {"box_id": box["id"], "active_tasks": 0, "tls_ready": True}
    assert tc.post("/gw/heartbeat", json=hb).status_code == 401
    ok = tc.post("/gw/heartbeat", json=hb,
                 headers={"Authorization": f"Bearer {_secret_of(svc, box['id'])}"})
    assert ok.status_code == 200 and ok.json()["state"] == "ready"
    r = tc.post("/api/tenants/t-a/stop", headers=h("portal", roles=["admin"]))
    assert r.status_code == 200 and r.json()["released"] == [box["id"]]
    _settle(svc)
    assert tc.get("/api/usage", headers=h("portal", roles=["admin"])).json()["usage"][0][
        "state"] == "gone"


def test_api_denials_map_to_http_codes():
    tc, svc, h = _api()
    assert tc.post("/api/boxes", json={"template": "nope"},
                   headers=h("alice", "t-a")).status_code == 404
    assert tc.post("/api/boxes", json={"template": "cpu"},
                   headers=h("alice")).status_code == 403      # no tenant claim
    for _ in range(2):
        tc.post("/api/boxes", json={"template": "cpu"}, headers=h("alice", "t-a"))
    assert tc.post("/api/boxes", json={"template": "cpu"},
                   headers=h("alice", "t-a")).status_code == 429
    assert tc.post("/api/boxes", json={"template": "cpu", "ttl": "long"},
                   headers=h("bob", "t-b")).status_code == 400
    assert tc.post("/api/boxes", content=b"[1]", headers={**h("bob", "t-b"),
                   "Content-Type": "application/json"}).status_code == 400
    _settle(svc)
