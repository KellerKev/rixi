"""Preview URLs: opening ports, and the two gates Caddy asks (certificate, and every request)."""
import pytest
from fastapi.testclient import TestClient

import rixi_server

GATE = "gate-secret-123"
DOMAIN = "b-abc.run.example.test"


@pytest.fixture()
def srv(monkeypatch):
    monkeypatch.setattr(rixi_server.auth_settings, "enabled", False)
    monkeypatch.setattr(rixi_server, "aes_key", None)
    monkeypatch.setattr(rixi_server, "require_encryption", False)
    monkeypatch.setattr(rixi_server, "PREVIEW_DOMAIN", DOMAIN)
    monkeypatch.setattr(rixi_server, "PREVIEW_GATE", GATE)
    rixi_server.previews.clear()
    yield TestClient(rixi_server.app)
    rixi_server.previews.clear()


def ask(c, domain, gate=GATE):
    return c.get("/preview/ask", params={"domain": domain, "gate": gate}).status_code


def auth(c, host, uri="/", cookie=None, gate=GATE):
    h = {"X-Rixi-Host": host, "X-Forwarded-Uri": uri, "X-Rixi-Preview-Gate": gate}
    if cookie:
        h["Cookie"] = f"rixi_preview={cookie}"
    return c.get("/preview/auth", headers=h, follow_redirects=False)


def test_opening_a_port_gives_a_url_and_a_share_link(srv):
    r = srv.post("/previews", json={"port": 8000})
    assert r.status_code == 200
    pv = r.json()
    assert pv["url"] == f"https://8000.{DOMAIN}/" and not pv["public"]
    assert pv["share_url"].startswith(pv["url"] + "?rixi_preview=")
    assert [p["port"] for p in srv.get("/previews").json()] == [8000]
    again = srv.post("/previews", json={"port": 8000, "public": True}).json()
    assert again["public"] and again["share_url"] == again["url"]


@pytest.mark.parametrize("port", [9000, 22, 2019, 11434, 0, 70000, "8000", True])
def test_the_server_its_neighbours_and_nonsense_cannot_be_opened(srv, port):
    assert srv.post("/previews", json={"port": port}).status_code == 400


def test_certificates_only_for_opened_ports_and_only_for_caddy(srv):
    srv.post("/previews", json={"port": 8000})
    assert ask(srv, f"8000.{DOMAIN}") == 200
    assert ask(srv, f"8001.{DOMAIN}") == 404                  # not opened
    assert ask(srv, f"9000.{DOMAIN}") == 404
    assert ask(srv, "8000.b-other.run.example.test") == 404
    assert ask(srv, f"8000.{DOMAIN}", gate="wrong") == 404


def test_a_private_preview_needs_its_share_link_once_then_the_cookie(srv):
    token = srv.post("/previews", json={"port": 8000}).json()["share_url"].split("=", 1)[1]
    host = f"8000.{DOMAIN}"
    assert auth(srv, host, "/app?x=1").status_code == 401
    r = auth(srv, host, f"/app?x=1&rixi_preview={token}")
    assert r.status_code == 302 and r.headers["location"] == "/app?x=1"   # token leaves the URL
    cookie = r.headers["set-cookie"]
    assert f"rixi_preview={token}" in cookie and "HttpOnly" in cookie and "Secure" in cookie
    assert "Domain" not in cookie                                # this host only
    assert auth(srv, host, "/app", cookie=token).status_code == 200
    assert auth(srv, host, "/app", cookie="forged").status_code == 401
    assert auth(srv, host, "/?rixi_preview=forged").status_code == 401


def test_public_previews_closing_and_the_gate(srv):
    srv.post("/previews", json={"port": 3000, "public": True})
    host = f"3000.{DOMAIN}"
    assert auth(srv, host).status_code == 200
    assert auth(srv, host, gate="wrong").status_code == 404      # only Caddy may ask
    assert auth(srv, f"3001.{DOMAIN}").status_code == 404
    assert srv.delete("/previews/3000").status_code == 200
    assert auth(srv, host).status_code == 404 and ask(srv, host) == 404
    assert srv.delete("/previews/3000").status_code == 404


def test_previews_need_a_named_box(srv, monkeypatch):
    monkeypatch.setattr(rixi_server, "PREVIEW_DOMAIN", "")
    assert srv.post("/previews", json={"port": 8000}).status_code == 404


def test_opening_a_preview_needs_a_token_when_auth_is_on(srv, monkeypatch):
    monkeypatch.setattr(rixi_server.auth_settings, "enabled", True)
    assert srv.post("/previews", json={"port": 8000}).status_code in (401, 403)
