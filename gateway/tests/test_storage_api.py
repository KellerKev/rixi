"""Storage through the gateway API: buckets, keys, a box's own key, usage."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytest.importorskip("websaw_ng")

from cryptography.fernet import Fernet  # noqa: E402

from gateway.auth import JwtVerifier  # noqa: E402
from gateway.direct.config import ConfigError, parse  # noqa: E402
from gateway.direct.s3client import S3Error  # noqa: E402
from gateway.direct.storage import StorageService  # noqa: E402
from gateway.direct.web import asgi, build_app  # noqa: E402

from test_direct import SyncClient, _conf, _keys, _settle, _svc  # noqa: E402

STORAGE = {"endpoint_url": "https://s3.example.test", "upstream": "https://s3.{region}.up.test",
           "regions": ["fr-par"], "access_key": "OP", "secret_key": "OPS",
           "key_secret": Fernet.generate_key().decode()}


class StubS3:
    """The operator-side calls, recorded."""

    def __init__(self):
        self.buckets = {}

    def endpoint(self, region):
        return f"https://s3.{region}.up.test"

    def create_bucket(self, region, name, tags):
        self.buckets[name] = {"region": region, "tags": tags, "objects": {}, "lifecycle": None}

    def put_lifecycle(self, region, name, days, abort_days=7):
        self.buckets[name]["lifecycle"] = (days, abort_days)

    def delete_bucket(self, region, name):
        if self.buckets.get(name, {}).get("objects"):
            raise S3Error(409, "BucketNotEmpty", "")
        self.buckets.pop(name, None)

    def empty(self, region, name):
        self.buckets[name]["objects"].clear()

    def measure(self, region, name):
        objs = self.buckets[name]["objects"]
        return {"STANDARD": float(sum(objs.values())), "ONEZONE_IA": 0.0, "objects": len(objs)}


def _api(**extra):
    priv, pub = _keys()
    svc = _svc(storage=dict(STORAGE), **extra)
    stub = StubS3()
    svc.storage = StorageService(svc.cfg, svc.store, client=stub, clock=svc.clock)
    tc = SyncClient(asgi(build_app(svc, JwtVerifier(public_key_pem=pub,
                                                    audience="rixi-gateway"))))

    def h(sub, tenant=None):
        import jwt
        claims = {"sub": sub, "roles": [], "exp": int(time.time()) + 600, "aud": "rixi-gateway"}
        if tenant:
            claims["tenant"] = tenant
        return {"Authorization": f"Bearer {jwt.encode(claims, priv, 'RS256')}"}
    return tc, svc, stub, h


def test_config_checks_storage():
    assert parse(_conf()).storage is None
    with pytest.raises(ConfigError, match="key_secret"):
        parse(_conf(storage={**STORAGE, "key_secret": "not-fernet"}))
    with pytest.raises(ConfigError, match="outside allowed_regions"):
        parse(_conf(storage={**STORAGE, "regions": ["us-east-1"]}))
    with pytest.raises(ConfigError, match="https"):
        parse(_conf(storage={**STORAGE, "endpoint_url": "http://s3.example.test"}))


def test_buckets_through_the_api_are_per_tenant():
    tc, svc, stub, h = _api()
    r = tc.post("/api/buckets", json={"name": "results", "expire_days": 30},
                headers=h("alice", "t-a"))
    assert r.status_code == 201, r.text
    b = r.json()
    assert "real_name" not in b and b["name"] == "results" and b["expire_days"] == 30
    real = svc.store.get_bucket(b["id"]).real_name
    assert stub.buckets[real]["tags"]["rixi-tenant"] == "t-a"
    assert stub.buckets[real]["lifecycle"] == (30, 7)
    assert tc.post("/api/buckets", json={"name": "results"},
                   headers=h("alice", "t-a")).status_code == 409
    assert tc.post("/api/buckets", json={"name": "Bad_Name"},
                   headers=h("alice", "t-a")).status_code == 400
    listing = tc.get("/api/buckets", headers=h("alice", "t-a")).json()
    assert [x["name"] for x in listing["buckets"]] == ["results"]
    assert listing["endpoint_url"] == "https://s3.example.test"
    assert tc.get("/api/buckets", headers=h("bob", "t-b")).json()["buckets"] == []
    assert tc.get(f"/api/buckets/{b['id']}", headers=h("bob", "t-b")).status_code == 404
    assert tc.delete(f"/api/buckets/{b['id']}", headers=h("bob", "t-b")).status_code == 404
    r = tc.patch(f"/api/buckets/{b['id']}", json={"expire_days": None}, headers=h("alice", "t-a"))
    assert r.status_code == 200 and stub.buckets[real]["lifecycle"] == (None, 7)
    stub.buckets[real]["objects"]["k"] = 10
    assert tc.delete(f"/api/buckets/{b['id']}", headers=h("alice", "t-a")).status_code == 409
    assert svc.store.get_bucket(b["id"]).state == "ready"
    r = tc.delete(f"/api/buckets/{b['id']}?force=1", headers=h("alice", "t-a"))
    assert r.status_code == 200
    deadline = time.time() + 5
    while svc.store.get_bucket(b["id"]).state != "deleted" and time.time() < deadline:
        time.sleep(0.02)
    assert svc.store.get_bucket(b["id"]).state == "deleted" and real not in stub.buckets


def test_keys_show_their_secret_once_and_can_be_limited_and_revoked():
    tc, svc, stub, h = _api()
    tc.post("/api/buckets", json={"name": "one"}, headers=h("alice", "t-a"))
    r = tc.post("/api/s3-keys", json={"name": "ci", "buckets": ["one"], "write": False},
                headers=h("alice", "t-a"))
    assert r.status_code == 201, r.text
    k = r.json()
    assert k["secret_key"] and k["endpoint_url"] == "https://s3.example.test" and not k["write"]
    listed = tc.get("/api/s3-keys", headers=h("alice", "t-a")).json()["keys"]
    assert len(listed) == 1 and "secret_key" not in listed[0] and "secret_enc" not in listed[0]
    stored = svc.store.get_s3_key(k["access_key"])
    assert k["secret_key"] not in stored.secret_enc          # encrypted at rest
    assert svc.storage.secret(stored) == k["secret_key"]
    assert tc.post("/api/s3-keys", json={"name": "x", "buckets": ["nope"]},
                   headers=h("alice", "t-a")).status_code == 404
    assert tc.delete(f"/api/s3-keys/{k['access_key']}", headers=h("bob", "t-b")).status_code == 404
    assert tc.delete(f"/api/s3-keys/{k['access_key']}",
                     headers=h("alice", "t-a")).status_code == 200
    assert tc.get("/api/s3-keys", headers=h("alice", "t-a")).json()["keys"] == []


def test_a_box_gets_its_own_storage_key_and_loses_it_when_it_ends():
    tc, svc, stub, h = _api()
    tc.post("/api/buckets", json={"name": "data"}, headers=h("alice", "t-a"))
    r = tc.post("/api/boxes", json={"template": "cpu", "storage": {"buckets": ["data"]}},
                headers=h("alice", "t-a"))
    assert r.status_code == 202, r.text
    box = r.json()
    _settle(svc)
    keys = svc.store.list_s3_keys(box_id=box["id"])
    assert len(keys) == 1 and keys[0].write == 1
    assert json.loads(keys[0].buckets) == [svc.store.find_bucket("t-a", "data").id]
    ud = [s["user_data"] for s in svc.providers["dummy"].servers.values()
          if f"rixi-box={box['id']}" in s["tags"]][0]
    assert f"RIXI_S3_ACCESS_KEY={keys[0].access_key}" in ud
    assert "RIXI_S3_ENDPOINT=https://s3.example.test" in ud
    assert "OPS" not in ud.split("RIXI_S3_SECRET_KEY=")[1].split("\\n")[0]   # never the operator's
    # box keys are not account keys
    assert tc.get("/api/s3-keys", headers=h("alice", "t-a")).json()["keys"][0]["box_id"] == box["id"]
    tc.delete(f"/api/boxes/{box['id']}", headers=h("alice", "t-a"))
    _settle(svc)
    assert svc.store.list_s3_keys(box_id=box["id"]) == []
    assert tc.post("/api/boxes", json={"template": "cpu", "storage": {"buckets": ["none"]}},
                   headers=h("alice", "t-a")).status_code == 404


def test_usage_reports_storage_totals():
    tc, svc, stub, h = _api()
    b = tc.post("/api/buckets", json={"name": "metered"}, headers=h("alice", "t-a")).json()
    real = svc.store.get_bucket(b["id"]).real_name
    stub.buckets[real]["objects"]["k"] = 4e9
    svc.clock.t += 3600
    svc.storage.scan_once(force=True)
    svc.store.add_bucket_traffic(b["id"], 2e9, 0, 3)
    u = tc.get("/api/usage?since=0", headers=h("alice", "t-a")).json()
    row = [x for x in u["storage"] if x["bucket"] == b["id"]][0]
    assert row["gb_hours"] == pytest.approx(2.0) and row["egress_gb"] == pytest.approx(2.0)
    assert row["eur_per_gb_hour"] == svc.cfg.storage.eur_per_gb_hour
    assert tc.get("/api/usage?since=0", headers=h("bob", "t-b")).json()["storage"] == []


def test_a_gateway_without_storage_says_so():
    priv, pub = _keys()
    svc = _svc()
    tc = SyncClient(asgi(build_app(svc, JwtVerifier(public_key_pem=pub, audience="rixi-gateway"))))
    import jwt
    hdr = {"Authorization": "Bearer " + jwt.encode(
        {"sub": "a", "tenant": "t-a", "exp": int(time.time()) + 60, "aud": "rixi-gateway"},
        priv, "RS256")}
    assert tc.get("/api/buckets", headers=hdr).status_code == 404
    r = tc.post("/api/boxes", json={"template": "cpu", "storage": {}}, headers=hdr)
    assert r.status_code == 400 and "no storage" in r.text
