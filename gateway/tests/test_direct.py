# Direct mode: tenancy, durable state, reaper, reconciler, heartbeat, EU floor, billing hooks.
import json
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
from gateway.direct.store import Box, Store  # noqa: E402


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


def test_config_takes_a_ca_for_box_certificates():
    assert parse(_conf()).acme_ca is None
    assert parse(_conf(acme_ca="letsencrypt")).acme_ca is None
    assert parse(_conf(acme_ca="zerossl")).acme_ca == "zerossl"
    with pytest.raises(ConfigError, match="acme_ca"):
        parse(_conf(acme_ca="zerossl\nRIXI_JWKS_URL=https://evil"))
    cfg = parse(_conf(acme_ca="zerossl"))
    assert "RIXI_ACME_CA=zerossl" in cloudinit.render(cfg, box_id="x", tenant="t", hostname="h",
                                                      box_secret="s")


# ── claim + provisioning ────────────────────────────────────────────────

def test_claim_provisions_box_with_dns_and_scoped_user_data():
    svc = _svc()
    box = svc.claim(A, "cpu")
    _settle(svc)
    cur = svc.store.get(box.id)
    assert cur.state == "booting" and cur.ip and cur.provider_id
    assert svc.dns.records[cur.hostname] == cur.ip
    assert svc.dns.records["*." + cur.hostname] == cur.ip      # preview URLs per port
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


def test_a_box_boots_only_after_its_record_is_published(gw_unused=None):
    """The record must be on every nameserver before the box boots: it asks for its
    certificate seconds later, and a zone wildcard would otherwise answer for its name."""
    order = []

    class SlowDns(DummyDns):
        def set_a(self, fqdn, ip):
            order.append(("set", fqdn))
            super().set_a(fqdn, ip)

        def wait_published(self, fqdn, ip, timeout=180.0):
            order.append(("wait", fqdn))
            return super().wait_published(fqdn, ip, timeout)

    class WatchingProvider(DummyProvider):
        def create(self, spec, on_ip):
            created = super().create(spec, on_ip)
            order.append(("boot", spec.box_id))
            return created

    cfg = parse(_conf())
    store = Store(f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'd.db')}")
    dns = SlowDns()
    svc = DirectService(cfg, store, {"dummy": WatchingProvider()}, dns, audit=lambda e, **a: None,
                        clock=Clock(), workers=2)
    box = svc.claim(A, "cpu")
    _settle(svc)
    assert [o[0] for o in order] == ["set", "set", "wait", "boot"], order
    assert order[1][1] == "*." + order[0][1]                  # preview names, before boot
    assert dns.published == [(box.hostname, svc.store.get(box.id).ip)]


def test_a_slow_record_is_audited_but_still_boots():
    class NeverPublishes(DummyDns):
        def wait_published(self, fqdn, ip, timeout=180.0):
            return False

    cfg = parse(_conf())
    store = Store(f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'd.db')}")
    events = []
    svc = DirectService(cfg, store, {"dummy": DummyProvider()}, NeverPublishes(),
                        audit=lambda e, **a: events.append(e), clock=Clock(), workers=2)
    box = svc.claim(A, "cpu")
    _settle(svc)
    assert "box.dns_slow" in events
    assert svc.store.get(box.id).state == "booting"      # still boots; Caddy retries


# ── serving a model ─────────────────────────────────────────────────────

def _serve_box(svc, caller=A, model="qwen3:0.6b", **kw):
    box = svc.claim(caller, "cpu", serve={"kind": "ollama", "model": model}, **kw)
    _settle(svc)
    return box


def test_a_served_box_carries_its_model_and_a_key_only_it_holds():
    svc = _svc()
    box = _serve_box(svc)
    key = box.endpoint_key                       # handed back once
    assert len(key) > 20
    cur = svc.store.get(box.id)
    assert cur.serve_kind == "ollama" and cur.serve_model == "qwen3:0.6b"
    assert key not in json.dumps(cur.public())   # never stored or shown again
    ud = next(iter(svc.providers["dummy"].servers.values()))["user_data"]
    assert f"RIXI_ENDPOINT_KEY={key}" in ud and "RIXI_SERVE_MODEL=qwen3:0.6b" in ud


def test_a_served_box_is_ready_only_once_the_model_is_pulled():
    svc = _svc()
    box = _serve_box(svc)
    secret = _secret_of(svc, box.id)
    assert svc.heartbeat(box.id, secret, 0, True, model_ready=False)["state"] == "booting"
    assert svc.heartbeat(box.id, secret, 0, True, model_ready=True)["state"] == "ready"
    assert svc.store.get(box.id).model_ready == 1


def test_requests_keep_an_endpoint_alive_and_are_counted():
    clock = Clock()
    svc = _svc(clock=clock)
    box = _serve_box(svc)
    secret = _secret_of(svc, box.id)
    svc.heartbeat(box.id, secret, 0, True, model_ready=True)
    clock.t += 400
    svc.heartbeat(box.id, secret, 0, True, requests=12, model_ready=True)   # traffic
    svc.reap_once()
    _settle(svc)
    assert svc.store.get(box.id).live
    assert svc.store.get(box.id).requests_total == 12
    clock.t += 601                                   # 10 min of silence, idle_timeout is 10m
    svc.heartbeat(box.id, secret, 0, True, requests=0, model_ready=True)
    svc.reap_once()
    _settle(svc)
    assert svc.store.get(box.id).end_reason == "idle"


def test_serve_requests_are_validated():
    svc = _svc()
    with pytest.raises(Denied, match="serve.model"):
        svc.claim(A, "cpu", serve={"kind": "ollama"})
    with pytest.raises(Denied, match="serve kind"):
        svc.claim(A, "cpu", serve={"kind": "vllm", "model": "x"})
    with pytest.raises(Denied, match="serve.model"):
        svc.claim(A, "cpu", serve={"kind": "ollama", "model": "evil\nRIXI_JWKS_URL=http://x"})


def test_api_returns_the_endpoint_url_and_key_once(gw=None):
    tc, svc, h = _api()
    r = tc.post("/api/boxes", json={"template": "cpu", "serve": {"kind": "ollama",
                                                                 "model": "qwen3:0.6b"}},
                headers=h("alice", "t-a"))
    assert r.status_code == 202
    body = r.json()
    assert body["endpoint_url"] == "https://%s/v1" % body["hostname"]
    assert body["endpoint_key"]
    _settle(svc)
    again = tc.get("/api/boxes/%s" % body["id"], headers=h("alice", "t-a")).json()
    assert "endpoint_key" not in again
    assert tc.post("/api/boxes", json={"template": "cpu", "serve": "ollama"},
                   headers=h("alice", "t-a")).status_code == 400


# ── network policy ──────────────────────────────────────────────────────

BLOCK = {"egress": {"mode": "block"}, "ingress": {"allow": ["192.0.2.0/24"]}}


def test_a_policy_is_validated_at_claim_and_gets_a_firewall_of_its_own():
    svc = _svc(control_plane=["65.21.252.62"])
    with pytest.raises(Denied, match="not an IP"):
        svc.claim(A, "cpu", policy={"egress": {"mode": "allowlist", "allow": ["example.com"]}})
    plain = svc.claim(A, "cpu")
    box = svc.claim(A, "cpu", policy=BLOCK)
    _settle(svc)
    assert svc.store.get(plain.id).firewall_id is None          # shares the zone-wide group
    cur = svc.store.get(box.id)
    assert cur.firewall_id == f"fw-{box.id}" and not cur.policy_applied
    assert cur.public()["policy"]["egress"]["mode"] == "block"
    assert "resolvers" not in cur.public()


def test_a_policed_box_is_ready_only_after_its_policy_is_live():
    svc = _svc(control_plane=["65.21.252.62"])
    box = svc.claim(A, "cpu", policy=BLOCK)
    _settle(svc)
    secret = _secret_of(svc, box.id)
    fw = svc.providers["dummy"].firewalls
    # no resolvers known yet: blocking egress now would cut DNS, so it waits
    assert svc.heartbeat(box.id, secret, 0, True)["state"] == "booting"
    _settle(svc)
    assert box.id not in fw and "resolvers" in svc.store.get(box.id).error
    assert svc.heartbeat(box.id, secret, 0, True,
                         resolvers=["51.159.47.28", "127.0.0.53"])["state"] == "booting"
    _settle(svc)
    compiled = fw[box.id]
    assert compiled.outbound_default == "drop"
    allowed = {(r.protocol, r.cidr, r.port_from) for r in compiled.rules
               if r.direction == "outbound" and r.action == "accept"}
    assert ("UDP", "51.159.47.28/32", 53) in allowed
    assert ("TCP", "65.21.252.62/32", 443) in allowed
    assert any(e == "box.policy_applied" for e, _ in svc.events)
    assert svc.heartbeat(box.id, secret, 0, True)["state"] == "ready"


def test_an_open_policy_needs_no_resolvers():
    svc = _svc(control_plane=["65.21.252.62"])
    box = svc.claim(A, "cpu", policy={"ingress": {"allow": ["192.0.2.0/24"]}})
    _settle(svc)
    secret = _secret_of(svc, box.id)
    svc.heartbeat(box.id, secret, 0, True)
    _settle(svc)
    assert svc.heartbeat(box.id, secret, 0, True)["state"] == "ready"


def test_a_provider_without_firewalls_refuses_a_policy():
    prov = DummyProvider()
    prov.supports_firewall = False
    svc = _svc(provider=prov)
    with pytest.raises(Denied, match="cannot enforce"):
        svc.claim(A, "cpu", policy=BLOCK)
    svc.claim(A, "cpu", policy={})                              # the default is fine


def test_api_takes_a_policy_and_heartbeats_report_resolvers():
    tc, svc, h = _api()
    r = tc.post("/api/boxes", json={"template": "cpu", "policy": BLOCK},
                headers=h("alice", "t-a"))
    assert r.status_code == 202 and r.json()["policy"]["egress"]["mode"] == "block"
    assert tc.post("/api/boxes", json={"template": "cpu", "policy": "block"},
                   headers=h("alice", "t-a")).status_code == 400
    assert tc.post("/api/boxes", json={"template": "cpu",
                                       "policy": {"egress": {"mode": "nope"}}},
                   headers=h("alice", "t-a")).status_code == 400
    _settle(svc)
    box_id = r.json()["id"]
    secret = _secret_of(svc, box_id)
    hb = {"Authorization": f"Bearer {secret}"}
    assert tc.post("/gw/heartbeat", json={"box_id": box_id, "active_tasks": 0,
                                          "resolvers": "1.1.1.1"},
                   headers=hb).status_code == 400
    assert tc.post("/gw/heartbeat", json={"box_id": box_id, "active_tasks": 0,
                                          "resolvers": ["51.159.47.28"]},
                   headers=hb).status_code == 200
    assert json.loads(svc.store.get(box_id).resolvers) == ["51.159.47.28"]


def test_an_upgrade_adds_columns_the_live_table_is_missing():
    """A release that adds a field must not query a column the database has never had."""
    import sqlite3
    d = tempfile.mkdtemp()
    path = os.path.join(d, "old.db")
    url = f"sqlite:///{path}"
    store = Store(url)
    store.insert(Box(id="old1", tenant="t-a", owner="u1", template="cpu", provider="dummy",
                     instance_type="DEV1-M", zone="fr-par-2", eur_per_hour=0.02,
                     hostname="b-old1.run.test", created_at=1.0))
    store.close()
    # simulate the older release: the serving columns were not there yet
    con = sqlite3.connect(path)
    for col in ("serve_kind", "serve_model", "model_ready", "requests_total", "last_request"):
        con.execute(f"ALTER TABLE rixi_box DROP COLUMN {col}")
    con.commit()
    cols = {r[1] for r in con.execute("PRAGMA table_info(rixi_box)")}
    con.close()
    assert "serve_kind" not in cols

    store2 = Store(url)                       # the upgrade opens it
    live = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(rixi_box)")}
    assert {"serve_kind", "serve_model", "model_ready", "requests_total"} <= live
    assert store2.get("old1").serve_kind is None      # existing rows still readable
    assert store2.list()[0].id == "old1"


# ── governed model calls ────────────────────────────────────────────────

def test_a_box_gets_a_model_gateway_and_token_but_the_store_keeps_neither():
    svc = _svc()
    tok = "rxl_" + "a" * 40
    box = svc.claim(A, "cpu", llm={"base_url": "https://rixi.example.test/llm", "token": tok})
    _settle(svc)
    ud = next(iter(svc.providers["dummy"].servers.values()))["user_data"]
    assert "RIXI_LLM_OPENAI_URL=https://rixi.example.test/llm/v1" in ud
    assert "RIXI_LLM_ANTHROPIC_URL=https://rixi.example.test/llm" in ud
    assert f"RIXI_LLM_TOKEN={tok}" in ud
    assert tok not in json.dumps(svc.store.get(box.id).public())


@pytest.mark.parametrize("llm", [
    {"base_url": "http://plain.example/llm", "token": "x" * 30},       # TLS only
    {"base_url": "https://x.example/llm", "token": "short"},
    {"openai_base_url": "https://x.example/v1", "token": "x" * 30, "anthropic_base_url": "ftp://y"},
    {"openai_base_url": "https://x.example/v1", "token": "sk proj with spaces"},
    {"base_url": "https://x.example/llm\nRIXI_JWKS_URL=http://evil", "token": "x" * 30},
    {"base_url": "https://x.example/llm", "token": "x" * 30 + "\n"},
    {"token": "x" * 30},
])
def test_llm_settings_are_validated(llm):
    with pytest.raises(Denied, match="llm"):
        _svc().claim(A, "cpu", llm=llm)


def test_a_box_can_call_the_customers_own_endpoint_directly():
    svc = _svc()
    key = "sk-proj-Abc123_def.456+/="
    box = svc.claim(A, "cpu", llm={"openai_base_url": "https://models.customer.example/v1",
                                    "token": key})
    _settle(svc)
    ud = next(iter(svc.providers["dummy"].servers.values()))["user_data"]
    assert "RIXI_LLM_OPENAI_URL=https://models.customer.example/v1" in ud
    assert "RIXI_LLM_ANTHROPIC_URL" not in ud and f"RIXI_LLM_TOKEN={key}" in ud
    assert key not in json.dumps(svc.store.get(box.id).public())


def test_released_boxes_leave_no_dns_behind():
    svc = _svc()
    box = svc.claim(A, "cpu")
    _settle(svc)
    svc.release(box.id, "test", caller=A)
    _settle(svc)
    assert svc.dns.records == {}


# ── persistence: volumes, stop / start ──────────────────────────────────

def _make_ready(svc, box):
    _settle(svc)
    svc.heartbeat(box.id, _secret_of(svc, box.id), 0, True)
    assert svc.store.get(box.id).state == "ready"


def _volume(svc, caller=A, name="data", size=20, zone=None):
    v = svc.create_volume(caller, name, size, zone)
    _settle_volumes(svc)
    return svc.store.get_volume(v.id)


def _settle_volumes(svc):
    deadline = time.time() + 5
    while time.time() < deadline:
        with svc._lock:
            if not any(k.startswith("vol:") for k in svc._inflight):
                return
        time.sleep(0.01)


def test_a_volume_outlives_its_box_and_moves_to_the_next():
    svc = _svc()
    v = _volume(svc)
    assert v.state == "available" and v.provider_id and v.public()["mount"] == "/workspace/data/data"
    box = svc.claim(A, "cpu", volumes=[v.id])
    assert box.zone == v.zone                                  # the box goes where its volume is
    assert svc.store.get_volume(v.id).state == "attached"
    _settle(svc)
    ud = next(iter(svc.providers["dummy"].servers.values()))["user_data"]
    assert f"RIXI_VOLUMES={v.provider_id}:data" in ud
    with pytest.raises(Denied, match="not available"):
        svc.claim(A, "cpu", volumes=[v.id])                   # one box at a time
    svc.release(box.id, "test", caller=A)
    _settle(svc)
    assert svc.store.get_volume(v.id).state == "available"     # freed, not deleted
    assert v.provider_id in svc.providers["dummy"].volumes
    second = svc.claim(A, "cpu", volumes=[v.id])
    _settle(svc)
    assert svc.store.get_volume(v.id).box_id == second.id


def test_volumes_are_private_validated_and_limited():
    svc = _svc()
    v = _volume(svc)
    with pytest.raises(Denied, match="no volume"):
        svc.claim(B, "cpu", volumes=[v.id])                   # someone else's
    with pytest.raises(NotFound):
        svc.delete_volume(B, v.id)
    for name, size in (("Bad Name", 10), ("ok", 0), ("ok", 5000), ("ok", "10")):
        with pytest.raises(Denied):
            svc.create_volume(A, name, size)
    with pytest.raises(Denied, match="already have"):
        svc.create_volume(A, "data", 10)
    with pytest.raises(Denied, match="the box must be there"):
        svc.claim(A, "gpu", zone="fr-par-2", volumes=[_volume(svc, name="far", zone="pl-waw-2").id])


def test_a_volume_in_use_cannot_be_deleted_and_deleting_stops_its_billing():
    clock = Clock()
    svc = _svc(clock=clock)
    v = _volume(svc, size=100)
    box = svc.claim(A, "cpu", volumes=[v.id])
    _settle(svc)
    with pytest.raises(Denied, match="release its box"):
        svc.delete_volume(A, v.id)
    svc.release(box.id, "test", caller=A)
    _settle(svc)
    clock.t += 3600
    svc.delete_volume(A, v.id)
    _settle_volumes(svc)
    assert svc.store.get_volume(v.id).state == "deleted"
    assert v.provider_id not in svc.providers["dummy"].volumes
    clock.t += 3600                                            # no longer billed
    row = svc.volume_usage(ADMIN, 0)[0]
    assert row["gb_hours"] == pytest.approx(100.0) and row["eur_per_gb_hour"] > 0


def test_stop_and_start_keep_the_box_and_bill_compute_only_while_it_runs():
    clock = Clock()
    svc = _svc(clock=clock)
    box = svc.claim(A, "cpu", ttl=3600)
    _make_ready(svc, box)
    clock.t += 600
    svc.stop_box(A, box.id)
    _settle(svc)
    assert svc.store.get(box.id).state == "stopped"
    srv = next(iter(svc.providers["dummy"].servers.values()))
    assert srv["state"] == "stopped"
    assert svc.heartbeat(box.id, _secret_of(svc, box.id), 0, True)["state"] == "stopped"
    clock.t += 3 * 3600                                        # past its ttl, while stopped
    svc.reap_once()
    _settle(svc)
    assert svc.store.get(box.id).state == "stopped"            # ttl counts running time only
    svc.start_box(A, box.id)
    assert svc.store.get(box.id).state == "starting" and srv["state"] == "running"
    assert svc.heartbeat(box.id, _secret_of(svc, box.id), 0, True)["state"] == "ready"
    clock.t += 300
    row = svc.usage(ADMIN, 0)[0]
    assert row["minutes"] == pytest.approx(15.0)               # 10 before + 5 after
    assert row["stopped_minutes"] == pytest.approx(180.0)
    assert row["stopped_eur_per_hour"] > 0
    cur = svc.store.get(box.id)
    assert cur.expires_at == pytest.approx(cur.created_at + 3600 + 3 * 3600)


def test_only_ready_boxes_stop_and_only_stopped_boxes_start():
    svc = _svc()
    box = svc.claim(A, "cpu")
    _settle(svc)
    with pytest.raises(Denied, match="only a ready box"):
        svc.stop_box(A, box.id)
    with pytest.raises(Denied, match="only a stopped box"):
        svc.start_box(A, box.id)
    with pytest.raises(NotFound):
        svc.stop_box(B, box.id)


def test_an_idle_box_can_stop_instead_of_going_away():
    clock = Clock()
    svc = _svc(clock=clock)
    box = svc.claim(A, "cpu", on_idle="stop")
    _make_ready(svc, box)
    clock.t += 601
    svc.heartbeat(box.id, _secret_of(svc, box.id), 0, True)    # alive, but nothing running
    svc.reap_once()
    _settle(svc)
    assert svc.store.get(box.id).state == "stopped"
    clock.t += 8 * 24 * 3600                                   # max_stopped is 7 days
    svc.reap_once()
    _settle(svc)
    assert svc.store.get(box.id).end_reason == "stopped_too_long"
    with pytest.raises(Denied, match="on_idle"):
        svc.claim(A, "cpu", on_idle="sleep")


def test_a_start_that_never_comes_back_is_stopped_again():
    clock = Clock()
    svc = _svc(clock=clock)
    box = svc.claim(A, "cpu")
    _make_ready(svc, box)
    svc.stop_box(A, box.id)
    _settle(svc)
    svc.start_box(A, box.id)
    clock.t += svc.cfg.boot_timeout + 1
    svc.reap_once()
    _settle(svc)
    cur = svc.store.get(box.id)
    assert cur.state == "stopped" and "come back" in cur.error


def test_a_stopped_box_does_not_count_toward_the_spend_limit():
    svc = _svc(limits={"max_boxes": 2, "max_eur_per_hour": 1.9, "default_ttl": "1h",
                       "max_ttl": "4h", "idle_timeout": "10m"})
    a = svc.claim(A, "cpu")                                     # 0.5 €/h, limit 1.9
    _make_ready(svc, a)
    svc.stop_box(A, a.id)
    _settle(svc)
    svc.claim(A, "gpu")                                         # 1.5 €/h: fits next to a stopped box
    with pytest.raises(Denied, match="spend limit"):
        svc.start_box(A, a.id)                                  # 1.5 + 0.5 would not


def test_api_volumes_and_stop_start(gw=None):
    tc, svc, h = _api()
    r = tc.post("/api/volumes", json={"name": "cache", "size_gb": 10}, headers=h("alice", "t-a"))
    assert r.status_code == 202
    _settle_volumes(svc)
    vid = r.json()["id"]
    assert tc.get("/api/volumes", headers=h("bob", "t-b")).json()["volumes"] == []
    r = tc.post("/api/boxes", json={"template": "cpu", "volumes": [vid], "on_idle": "stop"},
                headers=h("alice", "t-a"))
    assert r.status_code == 202 and r.json()["volumes"] == [vid]
    box_id = r.json()["id"]
    _settle(svc)
    svc.heartbeat(box_id, _secret_of(svc, box_id), 0, True)
    assert tc.post(f"/api/boxes/{box_id}/stop", headers=h("alice", "t-a")).status_code == 200
    _settle(svc)
    assert tc.post(f"/api/boxes/{box_id}/start", headers=h("alice", "t-a")).json()["state"] == \
        "starting"
    assert tc.post(f"/api/boxes/{box_id}/stop", headers=h("bob", "t-b")).status_code == 404
    usage = tc.get("/api/usage", headers=h("alice", "t-a")).json()
    assert usage["volumes"][0]["volume"] == vid and "stopped_minutes" in usage["usage"][0]


# ── choosing a box by what it has ───────────────────────────────────────

def _spec_svc():
    conf = _conf()
    conf["template"] = {
        "small": {"provider": "dummy", "type": "S", "zones": ["fr-par-2"], "eur_per_hour": 0.02,
                  "vcpu": 3, "ram_gb": 4},
        "big": {"provider": "dummy", "type": "B", "zones": ["fr-par-2"], "eur_per_hour": 0.22,
                "vcpu": 8, "ram_gb": 32},
        "l4": {"provider": "dummy", "type": "L4", "zones": ["fr-par-2"], "eur_per_hour": 0.80,
               "vcpu": 8, "ram_gb": 48, "gpu": "L4", "gpu_ram_gb": 24},
        "l40s": {"provider": "dummy", "type": "L40S", "zones": ["fr-par-2", "pl-waw-2"],
                 "eur_per_hour": 1.47, "vcpu": 8, "ram_gb": 96, "gpu": "L40S", "gpu_ram_gb": 48},
    }
    conf["direct"]["limits"]["max_eur_per_hour"] = 10.0
    svc = _svc()
    svc.cfg = parse(conf)
    return svc


@pytest.mark.parametrize("require,expect", [
    ({"ram_gb": 8}, "big"),
    ({"vcpu": 2}, "small"),
    ({"gpu": "any"}, "l4"),
    ({"gpu_ram_gb": 40}, "l40s"),
    ({"gpu": "l40s"}, "l40s"),
    ({"ram_gb": 40, "zone": "pl-waw-2"}, "l40s"),
])
def test_the_cheapest_box_that_has_what_you_need(require, expect):
    assert _spec_svc().match(require).name == expect


def test_matching_respects_the_allowed_list_and_says_when_nothing_fits():
    svc = _spec_svc()
    assert svc.match({"ram_gb": 8}, among=["small", "l4"]).name == "l4"
    with pytest.raises(Denied, match="no box type"):
        svc.match({"gpu": "any"}, among=["small", "big"])
    with pytest.raises(Denied, match="unknown keys"):
        svc.match({"cores": 4})
    with pytest.raises(Denied, match="must be a number"):
        svc.match({"ram_gb": "lots"})


def test_a_claim_can_say_what_it_needs_instead_of_a_template():
    svc = _spec_svc()
    box = svc.claim(A, None, require={"gpu_ram_gb": 20})
    assert box.template == "l4"
    with pytest.raises(Denied, match="name a template"):
        svc.claim(A, None)
    assert {t["name"]: t.get("gpu_ram_gb") for t in svc.templates()}["l40s"] == 48
