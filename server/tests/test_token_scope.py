"""Security regression: servers that trust the same issuer must not accept each other's tokens.

A hosted fleet signs every box's tokens with one key. Without an audience check, a token
minted for box A (or for another tenant) would execute code on box B. `--audience`,
`--required-claim`, `--require-exp` and `--revoked-jti-file` close that.
"""
import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import rixi_server


@pytest.fixture()
def signer(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    pub_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    s = rixi_server.auth_settings
    monkeypatch.setattr(s, "public_key", pub_pem)
    monkeypatch.setattr(s, "jwks_keys", {})
    monkeypatch.setattr(s, "enabled", True)
    for attr, value in (("audience", None), ("required_claims", {}), ("require_exp", False),
                        ("revoked_jti_path", None), ("_revoked_jti", set()),
                        ("_revoked_mtime", None)):
        monkeypatch.setattr(s, attr, value)

    def mint(**claims):
        claims.setdefault("sub", "u")
        return jwt.encode(claims, priv_pem, algorithm="RS256")
    return mint


def _valid(token):
    return asyncio.run(rixi_server.validate_token(token))[0]


def _exp():
    return int(time.time()) + 300


def test_no_scope_configured_keeps_old_behaviour(signer):
    assert _valid(signer(aud="box-a", exp=_exp()))
    assert _valid(signer())


def test_audience_accepts_own_box(signer):
    rixi_server.setup_token_scope("box-a", [], False, None)
    assert _valid(signer(aud="box-a", exp=_exp()))


def test_audience_refuses_other_box(signer):
    rixi_server.setup_token_scope("box-a", [], False, None)
    assert not _valid(signer(aud="box-b", exp=_exp()))


def test_audience_refuses_token_without_aud(signer):
    rixi_server.setup_token_scope("box-a", [], False, None)
    assert not _valid(signer(exp=_exp()))


def test_audience_implies_exp_required(signer):
    rixi_server.setup_token_scope("box-a", [], False, None)
    assert not _valid(signer(aud="box-a"))


def test_expired_token_refused(signer):
    rixi_server.setup_token_scope("box-a", [], False, None)
    assert not _valid(signer(aud="box-a", exp=int(time.time()) - 10))


def test_required_claim_refuses_other_tenant(signer):
    rixi_server.setup_token_scope("box-a", ["tenant=7"], False, None)
    assert _valid(signer(aud="box-a", tenant="7", exp=_exp()))
    assert _valid(signer(aud="box-a", tenant=7, exp=_exp()))  # numeric claim compares as text
    assert not _valid(signer(aud="box-a", tenant="8", exp=_exp()))
    assert not _valid(signer(aud="box-a", exp=_exp()))


def test_revoked_jti_refused_and_list_reloads(signer, tmp_path):
    revoked = tmp_path / "revoked"
    rixi_server.setup_token_scope("box-a", [], False, str(revoked))
    tok = signer(aud="box-a", exp=_exp(), jti="t1")
    assert _valid(tok)                      # missing file = nothing revoked
    revoked.write_text("t9\nt1\n")
    assert not _valid(tok)
    assert _valid(signer(aud="box-a", exp=_exp(), jti="t2"))


def test_revocation_requires_jti(signer, tmp_path):
    rixi_server.setup_token_scope("box-a", [], False, str(tmp_path / "revoked"))
    assert not _valid(signer(aud="box-a", exp=_exp()))


def test_scope_without_auth_is_a_config_error(monkeypatch):
    monkeypatch.setattr(rixi_server.auth_settings, "enabled", False)
    with pytest.raises(ValueError):
        rixi_server.setup_token_scope("box-a", [], False, None)


def test_malformed_required_claim_is_a_config_error(signer):
    with pytest.raises(ValueError):
        rixi_server.setup_token_scope(None, ["tenant"], False, None)


def test_status_endpoint_requires_auth(signer):
    """/status returns recent task output, so it must sit behind the same auth as /tasks."""
    from fastapi.testclient import TestClient
    client = TestClient(rixi_server.app)
    assert client.get("/status").status_code == 401
    assert client.get("/health").status_code == 200
    tok = signer(exp=_exp())
    assert client.get("/status", headers={"Authorization": f"Bearer {tok}"}).status_code == 200


def test_jwks_recovers_after_failed_first_fetch(monkeypatch):
    """A JWKS endpoint that is down at startup must not leave the server rejecting every token."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()).decode()
    nums = key.public_key().public_numbers()

    def b64(n):
        import base64
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    jwk = {"kty": "RSA", "kid": "k1", "n": b64(nums.n), "e": b64(nums.e)}

    s = rixi_server.auth_settings
    monkeypatch.setattr(s, "enabled", True)
    monkeypatch.setattr(s, "public_key", None)
    monkeypatch.setattr(s, "jwks_url", "https://issuer.invalid/jwks")
    monkeypatch.setattr(s, "jwks_keys", {})             # first fetch failed
    for attr, value in (("audience", None), ("required_claims", {}), ("require_exp", False),
                        ("revoked_jti_path", None)):
        monkeypatch.setattr(s, attr, value)

    async def fake_refresh():
        s.jwks_keys = {"k1": jwk}
    monkeypatch.setattr(rixi_server, "refresh_jwks_keys", fake_refresh)
    tok = jwt.encode({"sub": "u", "exp": _exp()}, priv_pem, algorithm="RS256",
                     headers={"kid": "k1"})
    assert _valid(tok)


def test_health_reports_task_count_without_auth():
    from fastapi.testclient import TestClient
    body = TestClient(rixi_server.app).get("/health").json()
    assert body["active_tasks"] == len(rixi_server.running_tasks)
