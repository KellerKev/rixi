"""Security regression: the server must pin JWT algorithms to RS256/ES256.

Guards against the classic algorithm-confusion attack — forging an HS256 token whose
HMAC secret is the server's RSA *public* key (which an attacker knows). A server that
derives the verification algorithm from the token header would accept it. `validate_token`
must reject it, and must reject `alg: none` too.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import rixi_server


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _forge_hs256(payload: dict, secret: str) -> str:
    """Hand-craft an HS256 JWT (PyJWT blocks encoding one with a PEM key, but an
    attacker crafting bytes by hand is not so constrained)."""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload).encode())
    signing_input = f"{header}.{body}".encode()
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(sig)}"


@pytest.fixture()
def rsa_public_pem(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    pub_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setattr(rixi_server.auth_settings, "public_key", pub_pem, raising=False)
    monkeypatch.setattr(rixi_server.auth_settings, "jwks_keys", {}, raising=False)
    monkeypatch.setattr(rixi_server.auth_settings, "enabled", True, raising=False)
    return priv_pem, pub_pem


def _valid(token):
    return asyncio.run(rixi_server.validate_token(token))[0]


def test_accepts_legitimate_rs256(rsa_public_pem):
    priv_pem, _ = rsa_public_pem
    token = jwt.encode({"sub": "u", "exp": int(time.time()) + 300}, priv_pem, algorithm="RS256")
    assert _valid(token) is True


def test_rejects_hs256_alg_confusion(rsa_public_pem):
    # Forge an HS256 token using the PUBLIC key (which an attacker knows) as the HMAC secret.
    _, pub_pem = rsa_public_pem
    forged = _forge_hs256({"sub": "attacker", "exp": int(time.time()) + 300}, pub_pem)
    assert _valid(forged) is False


def test_rejects_alg_none(rsa_public_pem):
    unsigned = jwt.encode({"sub": "attacker"}, key=None, algorithm="none")
    assert _valid(unsigned) is False
