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
