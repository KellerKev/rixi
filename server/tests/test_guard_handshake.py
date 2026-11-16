"""Security regressions for what stands in front of every route, and for the key handshake.

- With no tokens (the loopback default) a web page open in the operator's browser must not be
  able to drive the server: a request naming another site in Origin, or reaching the server
  under a name that is not localhost, is refused — over HTTP and over the terminal's WebSocket.
- With tokens, a caller without a valid one is turned away before any body is read.
- The key handshake never carries the secret; the server proves the key it offers is its own;
  only whoever began a handshake can finish it, once.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import rixi_server

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

SECRET = "s" * 43


@pytest.fixture()
def server(monkeypatch, tmp_path):
    s = rixi_server.auth_settings
    for attr, value in (("enabled", False), ("public_key", None), ("jwks_keys", {}),
                        ("jwks_url", None), ("audience", None), ("required_claims", {}),
                        ("require_exp", False), ("revoked_jti_path", None)):
        monkeypatch.setattr(s, attr, value)
    for name, value in (("aes_key", None), ("require_encryption", False), ("bind_host", None),
                        ("handshake_secret", None), ("secret_uses_remaining", None),
                        ("pending_handshake", None), ("WORKSPACE_ROOT", str(tmp_path))):
        monkeypatch.setattr(rixi_server, name, value, raising=False)
    return TestClient(rixi_server.app, base_url="http://127.0.0.1:9000")


@pytest.fixture()
def token(server, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    monkeypatch.setattr(rixi_server.auth_settings, "public_key", key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode())
    monkeypatch.setattr(rixi_server.auth_settings, "enabled", True)
    return {"Authorization": "Bearer " + jwt.encode(
        {"sub": "u", "exp": int(time.time()) + 300}, priv, algorithm="RS256")}


# ── a browser must not be a way in ───────────────────────────────────────────

def test_a_page_of_another_site_cannot_call_an_open_server(server, monkeypatch):
    monkeypatch.setattr(rixi_server, "bind_host", "127.0.0.1")
    assert server.get("/tasks").status_code == 200
    # what a page does with fetch(..., {mode: "no-cors"}): no preflight, text/plain body
    r = server.post("/exec", content='{"cmd": "id"}',
                    headers={"Origin": "https://evil.example", "Content-Type": "text/plain"})
    assert r.status_code == 403 and "other sites" in r.json()["detail"]
    assert server.post("/exec", content='{"cmd": "id"}',
                       headers={"Origin": "null"}).status_code == 403
    assert server.post("/upload", files={"file": ("p", b"x")},
                       headers={"Origin": "http://localhost:3000"}).status_code == 403
    # a page served by this very server is the one origin that may
    assert server.get("/tasks", headers={"Origin": "http://127.0.0.1:9000"}).status_code == 200
    with pytest.raises(WebSocketDisconnect) as closed:
        with server.websocket_connect("/pty", headers={"Origin": "https://evil.example"}):
            pass
    assert closed.value.code == 4403


def test_a_name_rebound_to_localhost_is_not_localhost(server, monkeypatch):
    monkeypatch.setattr(rixi_server, "bind_host", "127.0.0.1")
    for host in ("evil.example:9000", "evil.example", "127.0.0.1.evil.example:9000"):
        r = server.get("/tasks", headers={"Host": host})
        assert r.status_code == 403 and "localhost only" in r.json()["detail"], host
    for host in ("localhost:9000", "127.0.0.1:9000", "[::1]:9000", "localhost"):
        assert server.get("/tasks", headers={"Host": host}).status_code == 200, host
    # a server bound to an address on purpose (--insecure) answers under any name
    monkeypatch.setattr(rixi_server, "bind_host", "0.0.0.0")
    assert server.get("/tasks", headers={"Host": "box.example:9000"}).status_code == 200
    assert server.get("/tasks", headers={"Host": "box.example:9000",
                                         "Origin": "https://evil.example"}).status_code == 403


def test_with_tokens_the_token_is_the_check(server, token):
    # tokens are not something a browser sends by itself, so Origin does not matter
    assert server.get("/tasks", headers={**token, "Origin": "https://app.example"},
                      ).status_code not in (401, 403)   # past the token check


# ── nothing of a request is read before its token ────────────────────────────

class _Counting:
    """An ASGI client that counts how much of the body the app asked for."""

    def __init__(self, body_chunks):
        self.chunks, self.read, self.status = list(body_chunks), 0, None

    async def receive(self):
        if self.chunks:
            self.read += 1
            return {"type": "http.request", "body": self.chunks.pop(0),
                    "more_body": bool(self.chunks)}
        return {"type": "http.disconnect"}

    async def send(self, msg):
        if msg["type"] == "http.response.start":
            self.status = msg["status"]


def _scope(path, headers):
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
            "root_path": "", "server": ("127.0.0.1", 9000), "client": ("127.0.0.1", 1),
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}


@pytest.mark.parametrize("path", ["/upload", "/handshake", "/exec", "/task/x/redeploy", "/files"])
def test_no_body_is_read_from_a_caller_without_a_token(server, token, path):
    import asyncio
    for auth in ({}, {"Authorization": "Bearer junk"}):
        conn = _Counting([b"x" * 65536] * 50)
        asyncio.run(rixi_server.app(_scope(path, {
            "host": "127.0.0.1:9000", "content-type": "multipart/form-data; boundary=b", **auth,
        }), conn.receive, conn.send))
        assert conn.status == 401 and conn.read == 0, (path, auth)
    assert server.get("/health").status_code == 200            # needs none
    assert server.get("/docs").status_code == 401


def test_handshake_bodies_are_small(server, monkeypatch):
    monkeypatch.setattr(rixi_server, "handshake_secret", SECRET)
    big = json.dumps({"nonce": "A" * 100_000})
    assert server.post("/handshake", content=big,
                       headers={"Content-Type": "application/json"}).status_code == 413


# ── the key handshake ────────────────────────────────────────────────────────

def _proof(secret, step, *parts):
    mac = hmac.new(secret.encode(), step, hashlib.sha256)
    for part in parts:
        mac.update(len(part).to_bytes(4, "big") + part)
    return mac.digest()


def _b64(raw):
    return base64.b64encode(raw).decode()


def _begin(server, secret=SECRET, **extra):
    nonce = os.urandom(32)
    r = server.post("/handshake", json={"nonce": _b64(nonce),
                                        "proof": _b64(_proof(secret, b"rixi-hs1", nonce)), **extra})
    return nonce, r


def _wrap(answer, key=None):
    pub = serialization.load_pem_public_key(answer["public_key"].encode())
    return pub.encrypt(key or os.urandom(64), padding.OAEP(
        mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))


def test_the_sdk_negotiates_a_key_without_sending_the_secret(server, monkeypatch):
    from rixi import Client
    monkeypatch.setattr(rixi_server, "handshake_secret", SECRET)
    sent = []
    client = Client("http://127.0.0.1:9000")
    client._http = server                       # the SDK's session, pointed at the app
    real_post = server.post

    def post(url, **kw):
        sent.append(json.dumps(kw.get("json")))
        return real_post(url, **{k: v for k, v in kw.items() if k not in ("verify", "timeout")})
    monkeypatch.setattr(server, "post", post)
    key = client.handshake(SECRET)
    assert base64.b64decode(key) == rixi_server.aes_key
    assert len(sent) == 2 and not any(SECRET in body for body in sent)
    assert rixi_server.pending_handshake is None


def test_the_old_handshake_that_sent_the_secret_is_refused(server, monkeypatch):
    monkeypatch.setattr(rixi_server, "handshake_secret", SECRET)
    r = server.post("/handshake", json={"secret": SECRET})
    assert r.status_code == 400 and "update the client" in r.json()["error"]
    assert rixi_server.pending_handshake is None


def test_a_handshake_needs_the_secret_at_both_steps(server, monkeypatch):
    monkeypatch.setattr(rixi_server, "handshake_secret", SECRET)
    assert _begin(server, secret="wrong" * 9)[1].status_code == 401
    nonce, r = _begin(server)
    assert r.status_code == 200
    answer = r.json()
    server_nonce = base64.b64decode(answer["nonce"])
    # the server proved the key is its own
    assert base64.b64decode(answer["proof"]) == _proof(
        SECRET, b"rixi-hs2", nonce, server_nonce, answer["public_key"].encode())
    # somebody who saw the exchange (token, nonces, public key) but does not know the secret
    stolen = _wrap(answer)
    for proof in (None, _b64(os.urandom(32)),
                  _b64(_proof("guess" * 9, b"rixi-hs3", nonce, server_nonce, stolen))):
        body = {"cipher": _b64(stolen), **({"proof": proof} if proof else {})}
        assert server.post("/handshake/finish", json=body).status_code == 401
    assert rixi_server.aes_key is None
    # the one who began it finishes it
    mine = os.urandom(64)
    cipher = _wrap(answer, mine)
    good = {"cipher": _b64(cipher),
            "proof": _b64(_proof(SECRET, b"rixi-hs3", nonce, server_nonce, cipher))}
    assert server.post("/handshake/finish", json=good).status_code == 200
    assert rixi_server.aes_key == mine[:32]
    # … once: the same finish again, or another key under the same handshake, goes nowhere
    assert server.post("/handshake/finish", json=good).status_code == 400
    assert rixi_server.aes_key == mine[:32]


def test_a_handshake_left_open_expires(server, monkeypatch):
    monkeypatch.setattr(rixi_server, "handshake_secret", SECRET)
    nonce, r = _begin(server)
    answer = r.json()
    cipher = _wrap(answer)
    rixi_server.pending_handshake["at"] -= rixi_server.HANDSHAKE_TTL + 1
    r = server.post("/handshake/finish", json={"cipher": _b64(cipher), "proof": _b64(_proof(
        SECRET, b"rixi-hs3", nonce, base64.b64decode(answer["nonce"]), cipher))})
    assert r.status_code == 400 and rixi_server.aes_key is None


def test_a_one_use_secret_is_spent_by_the_first_handshake(server, monkeypatch):
    monkeypatch.setattr(rixi_server, "handshake_secret", SECRET)
    monkeypatch.setattr(rixi_server, "secret_uses_remaining", 1)
    assert _begin(server, secret="wrong" * 9)[1].status_code == 401      # a miss costs nothing
    assert _begin(server)[1].status_code == 200
    assert _begin(server)[1].status_code == 403


# ── with a key, answers are sealed ───────────────────────────────────────────

def test_plain_json_answers_are_sealed_when_encryption_is_required(server, monkeypatch):
    key = os.urandom(32)
    monkeypatch.setattr(rixi_server, "aes_key", key)
    r = server.get("/tasks")
    assert r.headers.get("x-rixi-encrypted") is None and r.json() is not None   # as before
    r = server.get("/tasks", headers={"X-Rixi-Accept-Sealed": "1"})             # the SDK asks
    assert r.headers["x-rixi-encrypted"] == "1"
    assert json.loads(rixi_server.decrypt(r.content)) is not None
    monkeypatch.setattr(rixi_server, "require_encryption", True)
    for path in ("/tasks", "/status"):
        r = server.get(path)
        assert r.headers["x-rixi-encrypted"] == "1", path
        json.loads(rixi_server.decrypt(r.content))
    assert server.get("/health").headers.get("x-rixi-encrypted") is None        # before a key


def test_an_sdk_with_a_key_refuses_an_answer_in_the_clear(server, monkeypatch):
    from rixi import Client
    from rixi.client import RixiError
    client = Client("http://127.0.0.1:9000", aes_key=_b64(os.urandom(32)))
    client._http = server
    real_get = server.get
    monkeypatch.setattr(server, "get", lambda url, **kw: real_get(
        url, **{k: v for k, v in kw.items() if k not in ("verify", "timeout")}))
    with pytest.raises(RixiError, match="in the clear"):
        client.tasks()                           # this server has no key: it answers plainly


# ── a token seen on the wire ─────────────────────────────────────────────────

def test_single_use_tokens_cannot_be_sent_again(server, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    s = rixi_server.auth_settings
    monkeypatch.setattr(s, "public_key", key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode())
    monkeypatch.setattr(s, "enabled", True)
    monkeypatch.setattr(s, "single_use", True)
    monkeypatch.setattr(s, "_used_jti", {})

    def bearer(**claims):
        claims.setdefault("exp", int(time.time()) + 120)
        return {"Authorization": "Bearer " + jwt.encode({"sub": "u", **claims}, priv, "RS256")}
    fresh = bearer(jti="a1")
    assert server.get("/tasks", headers=fresh).status_code == 200
    # somebody who saw that request go by: a body-less call needs nothing but the token
    assert server.delete("/task/whatever", headers=fresh).status_code == 401
    assert server.get("/tasks", headers=fresh).status_code == 401
    assert server.get("/tasks", headers=bearer(jti="a2")).status_code == 200       # a new one
    assert server.get("/tasks", headers=bearer()).status_code == 401               # no jti
    # routes with a check of their own count the token once, not twice
    assert server.get("/logs/tasks", headers=bearer(jti="a3", logs_retrieval="enabled")
                      ).status_code not in (401, 403)   # past the token check


# ── JWKS ─────────────────────────────────────────────────────────────────────

def test_unknown_key_ids_do_not_make_the_server_hammer_the_issuer(server, monkeypatch):
    import asyncio
    calls = []
    monkeypatch.setattr(rixi_server.auth_settings, "jwks_url", "https://issuer.example/jwks")
    monkeypatch.setattr(rixi_server.auth_settings, "enabled", True)
    monkeypatch.setattr(rixi_server, "_jwks_refreshed_at", 0.0)

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"keys": []}
    monkeypatch.setattr(rixi_server.requests, "get", lambda *a, **kw: calls.append(1) or Resp())
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    tok = jwt.encode({"sub": "x"}, key, algorithm="RS256", headers={"kid": "nope"})
    for _ in range(20):
        assert asyncio.run(rixi_server.validate_token(tok))[0] is False
    assert len(calls) == 1


# ── the older command-line client speaks the same handshake ──────────────────

def test_the_command_line_client_negotiates_and_seals_its_uploads(server, monkeypatch, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients"))
    import rixi_transport
    from rixi.crypto import iter_frames
    monkeypatch.setattr(rixi_server, "handshake_secret", SECRET)
    sent = []

    def http(method, url, **kw):
        sent.append(json.dumps(kw.get("json")))
        return server.request(method, url, **{k: v for k, v in kw.items()
                                              if k in ("json", "headers")})
    monkeypatch.setattr(rixi_transport, "_http", http)
    monkeypatch.chdir(tmp_path)                       # it writes aes.key beside the project
    t = rixi_transport.Transport("http://127.0.0.1:9000")
    key, _ = t.perform_handshake(SECRET, rotate=False)
    assert key == rixi_server.aes_key and not any(SECRET in body for body in sent)
    # what it uploads with that key is sealed frame by frame, the way the server reads it
    pkg = tmp_path / "pkg.lz4"
    pkg.write_bytes(os.urandom(3 * 1024 * 1024 + 17))
    sealed = Path(t._seal_package(str(pkg)))
    try:
        assert pkg.read_bytes() not in sealed.read_bytes()
        assert b"".join(iter_frames(key, [sealed.read_bytes()])) == pkg.read_bytes()
    finally:
        sealed.unlink()
    # and its own key files are never part of a package
    assert all(rixi_transport.is_local_secret(n) for n in (
        "aes.key", "rotation.secret", "pixi_remote_config.toml", "rixi_headers.json", "tls.pem"))
    assert not rixi_transport.is_local_secret("main.py")


# ── a box with a network policy stays shut until the policy is in force ──────

def test_a_held_server_answers_health_and_nothing_else_until_it_is_opened(server, token, tmp_path,
                                                                          monkeypatch):
    ready = tmp_path / "ready"
    monkeypatch.setattr(rixi_server, "hold_path", str(ready))
    monkeypatch.setattr(rixi_server, "_hold_lifted", False)
    assert server.get("/health").status_code == 200              # the box's own TLS self-check
    for method, path in (("GET", "/tasks"), ("POST", "/exec"), ("PUT", "/files?path=x"),
                         ("POST", "/upload")):
        r = server.request(method, path, headers=token, content=b"{}")
        assert r.status_code == 503 and "network policy" in r.json()["detail"], path
    with pytest.raises(WebSocketDisconnect) as closed:
        with server.websocket_connect("/pty", headers=token):
            pass
    assert closed.value.code == 4503
    ready.write_text("")                                         # the agent opens the box
    assert server.get("/tasks", headers=token).status_code == 200
    ready.unlink()                                               # opened once, open for good
    assert server.get("/tasks", headers=token).status_code == 200
