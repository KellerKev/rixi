"""Security regressions for the inference proxy: who may call it, and what a passthrough path
may reach. The backend is never contacted here."""
import os
import sys
import time
import types

import jwt
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import aiohttp  # noqa: F401
except ImportError:                      # only used when a request is really forwarded
    sys.modules["aiohttp"] = types.ModuleType("aiohttp")

import proxy as proxy_mod  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

BACKEND = {"backends": [{"id": "b", "url": "http://backend.invalid:9000/task/t1", "weight": 1}]}


def _client(**config):
    return TestClient(proxy_mod.create_enhanced_app({**BACKEND, **config}))


def test_the_settings_of_the_example_config_take_effect(monkeypatch):
    import yaml
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    example = yaml.safe_load(open(os.path.join(here, "proxy_config.example.yaml")))
    example.pop("aes_key_file", None)
    monkeypatch.delenv("PROXY_API_KEY", raising=False)
    monkeypatch.setenv("BACKEND_API_KEY", "backend-token")
    with pytest.raises(proxy_mod.ConfigError, match="PROXY_API_KEY"):
        proxy_mod.create_enhanced_app(example)          # never the literal "${PROXY_API_KEY}"
    monkeypatch.setenv("PROXY_API_KEY", "k-123")
    c = TestClient(proxy_mod.create_enhanced_app(example))
    assert c.get("/v1/models").status_code == 401
    assert c.get("/v1/models", headers={"x-api-key": "${PROXY_API_KEY}"}).status_code == 401
    assert c.get("/v1/models", headers={"x-api-key": "k-123"}).status_code == 200
    cfg = proxy_mod.normalize_config(example)
    assert cfg["auth_headers"]["Authorization"] == "Bearer backend-token"


def test_keys_written_at_the_top_level_are_honoured_too():
    c = _client(require_api_key=True, api_key="k-123")
    assert c.get("/status").status_code == 401
    assert c.get("/status", headers={"Authorization": "Bearer k-123"}).status_code == 200
    with pytest.raises(proxy_mod.ConfigError, match="api_key"):
        _client(require_api_key=True)


def test_every_route_but_health_needs_the_key():
    c = _client(authentication={"require_api_key": True, "api_key": "k-123"})
    for method, path in (("GET", "/status"), ("GET", "/metrics"), ("GET", "/admin/models"),
                         ("GET", "/admin/apis"), ("POST", "/admin/cache/clear"),
                         ("GET", "/v1/models"), ("GET", "/api/tags"),
                         ("POST", "/v1/chat/completions"), ("GET", "/proxy/x"),
                         ("GET", "/t1/proxy/x")):
        assert c.request(method, path).status_code == 401, path
    assert c.get("/health").status_code == 200
    assert c.post("/admin/cache/clear", headers={"x-api-key": "k-123"}).status_code == 200


def test_a_jwt_setting_is_enforced():
    secret = "s" * 40
    c = _client(authentication={"jwt": {"enabled": True, "secret_key": secret}})

    def bearer(**claims):
        return {"Authorization": "Bearer " + jwt.encode(claims, secret, algorithm="HS256")}
    assert c.get("/v1/models").status_code == 401
    assert c.get("/v1/models", headers=bearer(sub="u", exp=int(time.time()) + 60)).status_code == 200
    assert c.get("/v1/models", headers=bearer(sub="u")).status_code == 401              # no exp
    assert c.get("/v1/models", headers=bearer(sub="u", exp=int(time.time()) - 5)).status_code == 401
    forged = {"Authorization": "Bearer " + jwt.encode(
        {"sub": "u", "exp": int(time.time()) + 60}, "x" * 40, algorithm="HS256")}
    assert c.get("/v1/models", headers=forged).status_code == 401
    with pytest.raises(proxy_mod.ConfigError, match="secret_key"):
        _client(authentication={"jwt": {"enabled": True}})


@pytest.mark.parametrize("path", [
    "../../../exec", "a/../../../../exec", "%2e%2e/%2e%2e/%2e%2e/exec", "..%2f..%2f..%2fexec",
    "%252e%252e/%252e%252e/exec", "..\\..\\exec", "./x", "a/%2e/b", "a/..", "x%00y"])
def test_a_passthrough_path_cannot_leave_its_task(path):
    assert proxy_mod.unsafe_path(path)
    forwarded = []

    class Request:                       # never reaches the network: refused before any call
        method, query_params, headers = "POST", {}, {}
    channel = proxy_mod.ProxyServer(proxy_mod.normalize_config(dict(BACKEND))).channels[0]

    class Session:
        def __init__(self, *a, **kw):
            forwarded.append(1)
    proxy_mod.aiohttp.ClientSession = Session
    import asyncio
    for task in (None, "t1"):
        result = asyncio.run(channel.forward_http_request(Request(), path, task))
        assert result["success"] is False and result["status_code"] == 400
    assert forwarded == []


def test_ordinary_paths_pass():
    for path in ("v1/items", "a/b.c/d", "files/report..final.txt", "x/...", ""):
        assert not proxy_mod.unsafe_path(path), path
