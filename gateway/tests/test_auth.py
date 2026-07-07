# JWT verification tests — in-test RS256 keypair (PEM path) + a local JWKS server.
import asyncio
import base64
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytest.importorskip("jwt")
import jwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from gateway.auth import ANON, Identity, JwtVerifier, identity_from_claims  # noqa: E402


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return key, priv, pub


def _mint(priv, claims, kid=None, alg="RS256", exp_in=3600):
    payload = {"sub": "alice", "exp": int(time.time()) + exp_in, **claims}
    headers = {"kid": kid} if kid else None
    return jwt.encode(payload, priv, algorithm=alg, headers=headers)


def _b64u(n: int) -> str:
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _jwks(pubkey, kid):
    nums = pubkey.public_numbers()
    return {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
                      "n": _b64u(nums.n), "e": _b64u(nums.e)}]}


# ---- PEM path -------------------------------------------------------------

def test_valid_token_roles_and_scopes():
    _, priv, pub = _keypair()
    v = JwtVerifier(public_key_pem=pub)
    tok = _mint(priv, {"roles": ["user", "ml"], "scope": "read write"})
    ident = asyncio.run(v.verify(tok))
    assert ident is not None
    assert ident.sub == "alice"
    assert ident.has_role("ml") and ident.has_role("user")
    assert ident.has_scope("read") and ident.has_scope("write")
    assert not ident.anon


def test_rejects_expired_badsig_wrongalg():
    _, priv, pub = _keypair()
    _, other_priv, _ = _keypair()
    v = JwtVerifier(public_key_pem=pub)
    assert asyncio.run(v.verify(_mint(priv, {}, exp_in=-10))) is None          # expired
    assert asyncio.run(v.verify(_mint(other_priv, {}))) is None                # bad signature
    assert asyncio.run(v.verify(jwt.encode({"sub": "x"}, "secret", algorithm="HS256"))) is None  # alg
    assert asyncio.run(v.verify("")) is None
    assert asyncio.run(v.verify("not.a.jwt")) is None


def test_disabled_verifier_returns_none():
    v = JwtVerifier()  # no key, no jwks
    assert not v.enabled
    assert asyncio.run(v.verify("anything")) is None


def test_role_claim_shapes():
    # single string, and a nested (Keycloak-style) path
    assert identity_from_claims({"sub": "a", "roles": "admin"}).roles == ("admin",)
    nested = identity_from_claims({"sub": "a", "realm_access": {"roles": ["x", "y"]}},
                                  roles_claim="realm_access.roles")
    assert nested.roles == ("x", "y")
    assert ANON.anon and ANON.label == "anon"
    assert isinstance(Identity().roles, tuple)


# ---- JWKS path ------------------------------------------------------------

def _serve_jwks(doc):
    payload = json.dumps(doc).encode()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/jwks.json"


def test_jwks_path():
    key, priv, _ = _keypair()
    kid = "key-1"
    srv, url = _serve_jwks(_jwks(key.public_key(), kid))
    try:
        v = JwtVerifier(jwks_url=url)
        asyncio.run(v.start())
        ident = asyncio.run(v.verify(_mint(priv, {"roles": ["admin"]}, kid=kid)))
        assert ident is not None and ident.has_role("admin")
        # a token whose kid isn't in the set → rejected
        assert asyncio.run(v.verify(_mint(priv, {}, kid="unknown"))) is None
    finally:
        srv.shutdown()
