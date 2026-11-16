# Management API tests — JWT-gated; admin role for mutations.
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytest.importorskip("fastapi")
import jwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gateway.auth import JwtVerifier  # noqa: E402
from gateway.management import build_app  # noqa: E402
from gateway.policy import GlobalPolicy, PolicyEngine, RolePolicy  # noqa: E402
from gateway.server import Gateway  # noqa: E402


def _keys():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()
    pub = k.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub


def _tok(priv, roles):
    return jwt.encode({"sub": "u", "roles": roles, "exp": int(time.time()) + 600}, priv, "RS256")


def _client():
    priv, pub = _keys()
    gw = Gateway("s", "127.0.0.1", 0)
    gw.verifier = JwtVerifier(public_key_pem=pub)
    gw.policy = PolicyEngine(GlobalPolicy(enabled=True, admin_role="admin",
                                          roles={"admin": RolePolicy(allowed_ops=("*",))}), {})
    return TestClient(build_app(gw)), priv


def test_health_is_open():
    tc, _ = _client()
    assert tc.get("/api/health").status_code == 200


def test_protected_requires_jwt():
    tc, priv = _client()
    assert tc.get("/api/nodes").status_code == 401                       # no token
    ok = tc.get("/api/nodes", headers={"Authorization": f"Bearer {_tok(priv, ['user'])}"})
    assert ok.status_code == 200 and "nodes" in ok.json()


def test_policy_put_requires_admin():
    tc, priv = _client()
    user_h = {"Authorization": f"Bearer {_tok(priv, ['user'])}"}
    admin_h = {"Authorization": f"Bearer {_tok(priv, ['admin'])}"}
    assert tc.get("/api/policy", headers=user_h).status_code == 200      # read: any identity
    assert tc.put("/api/policy", headers=user_h, json={"global": {}}).status_code == 403
    r = tc.put("/api/policy", headers=admin_h,
               json={"global": {"require_jwt": True, "admin_role": "admin"}})
    assert r.status_code == 200 and r.json()["global"]["require_jwt"] is True


def test_resource_actions_admin_only():
    tc, priv = _client()
    user_h = {"Authorization": f"Bearer {_tok(priv, ['user'])}"}
    admin_h = {"Authorization": f"Bearer {_tok(priv, ['admin'])}"}
    assert tc.post("/api/resources/none/teardown", headers=user_h).status_code == 403
    r = tc.post("/api/resources/none/teardown", headers=admin_h)
    assert r.status_code == 200 and r.json()["destroyed"] is False   # nothing to tear down
    assert tc.post("/api/resources/none/provision", headers=admin_h).status_code == 404  # unknown


def test_audit_admin_only_and_empty_without_store():
    tc, priv = _client()
    user_h = {"Authorization": f"Bearer {_tok(priv, ['user'])}"}
    admin_h = {"Authorization": f"Bearer {_tok(priv, ['admin'])}"}
    assert tc.get("/api/audit", headers=user_h).status_code == 403
    r = tc.get("/api/audit", headers=admin_h)
    assert r.status_code == 200 and r.json()["events"] == []


def test_a_policy_sent_over_the_api_is_taken_as_written(monkeypatch, tmp_path):
    """${env:…} and ${file:…} are for the operator's config file. Expanded in a policy that
    arrives over the API, they would hand whoever may edit the policy the gateway's environment
    and files, and show them to every signed-in user afterwards."""
    monkeypatch.setenv("RIXI_GATEWAY_SECRET", "the-tunnel-secret")
    secret_file = tmp_path / "state.key"
    secret_file.write_text("state-passphrase")
    tc, priv = _client()
    admin_h = {"Authorization": f"Bearer {_tok(priv, ['admin'])}"}
    r = tc.put("/api/policy", headers=admin_h, json={"global": {
        "default_role": "${env:RIXI_GATEWAY_SECRET}", "admin_role": f"${{file:{secret_file}}}"}})
    assert r.status_code == 200
    for text in (r.text, tc.get("/api/policy", headers=admin_h).text):
        assert "the-tunnel-secret" not in text and "state-passphrase" not in text
    # the config file still gets its placeholders
    from gateway.config import parse_global_policy
    assert parse_global_policy({"default_role": "${env:RIXI_GATEWAY_SECRET}"}).default_role == \
        "the-tunnel-secret"


def test_nodes_are_listed_to_those_who_may_know_them():
    from gateway.registry import RegisteredNode
    tc, priv = _client()
    gw = tc.app.state.gateway if hasattr(tc.app.state, "gateway") else None
    # the app closes over its gateway: reach it through a route's closure instead
    for route in tc.app.routes:
        if getattr(route, "path", "") == "/api/nodes":
            gw = route.endpoint.__closure__[0].cell_contents
    claim = gw.claims.create("alice-client", "dummy", {}, owner="alice")
    gw.registry.register(RegisteredNode(node_id=claim.token, kind="server", identity=claim.token))
    gw.registry.register(RegisteredNode(node_id="shared-box", kind="server", identity="shared-box"))

    def ids(sub, roles):
        tok = jwt.encode({"sub": sub, "roles": roles, "exp": int(time.time()) + 600}, priv, "RS256")
        return {n["node_id"] for n in tc.get(
            "/api/nodes", headers={"Authorization": f"Bearer {tok}"}).json()["nodes"]}
    assert ids("bob", ["user"]) == {"shared-box"}
    assert ids("alice", ["user"]) == {"shared-box", claim.token}
    assert ids("op", ["admin"]) == {"shared-box", claim.token}
