# The back-channel: a task asks the client to do something (read a local file, call a local
# service) by printing a request line. Anything a task prints could look like one, so a request
# counts only when it carries the token the client made for this run.
import asyncio
import io
import os
import tarfile

import lz4.frame

import rixi_client
from rixi_client import BACKCHANNEL_FILE, OneStepPixiClient, _package_dir, _resolve_within_root


def _client():
    c = OneStepPixiClient("http://127.0.0.1:9000")
    handled = []

    async def handle(request):
        handled.append(request)
        return {"ok": True}

    async def respond(kind, response):
        pass
    c._handle_mcp_request = handle
    c._send_backchannel_response = respond
    return c, handled


def test_a_request_needs_this_runs_token():
    c, handled = _client()
    run = asyncio.run
    # what a task echoing somebody else's text would print
    run(c._process_enhanced_output_line('MCP_REQUEST:{"action": "write_file", "request_id": "x"}'))
    run(c._process_enhanced_output_line('MCP_REQUEST:guess:{"action": "write_file"}'))
    run(c._process_enhanced_output_line('HTTP_PROXY:{"method": "GET", "path": "/admin"}'))
    assert handled == []
    run(c._process_enhanced_output_line(
        f'MCP_REQUEST:{c.backchannel_token}:{{"action": "read_file", "request_id": "r1"}}'))
    assert [r["action"] for r in handled] == ["read_file"]
    # the operator can ask for the old behaviour
    c.allow_untagged_backchannel = True
    run(c._process_enhanced_output_line('MCP_REQUEST:{"action": "list", "request_id": "r2"}'))
    assert [r["action"] for r in handled] == ["read_file", "list"]


def test_the_token_travels_inside_the_package_and_the_clients_keys_do_not(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("pixi.toml", "main.py", "aes.key", "rotation.secret", "pixi_remote_config.toml",
                 "rixi_headers.json", "server.pem", BACKCHANNEL_FILE):
        (tmp_path / name).write_text("x")
    monkeypatch.setattr(rixi_client, "_show_package_stats", lambda *a, **kw: None, raising=False)
    pkg = _package_dir(backchannel_token="tok-123")
    try:
        with lz4.frame.open(pkg, "rb") as fh, tarfile.open(fileobj=io.BytesIO(fh.read())) as tar:
            names = sorted(tar.getnames())
            token = tar.extractfile(BACKCHANNEL_FILE).read().decode().strip()
    finally:
        os.unlink(pkg)
    assert names == sorted([BACKCHANNEL_FILE, "main.py", "pixi.toml"]) and token == "tok-123"


def test_a_task_cannot_write_where_it_would_run_or_read_credentials(tmp_path):
    for rel in ("notes.txt", "src/app.py", "out/report.json"):
        assert _resolve_within_root(str(tmp_path), rel, write=True) is not None, rel
    for rel in (".git/hooks/pre-commit", ".envrc", ".ssh/authorized_keys", "sub/.git/config",
                "aes.key", "../outside", ".bashrc", "deploy/server.pem"):
        assert _resolve_within_root(str(tmp_path), rel, write=True) is None, rel
    for rel in ("aes.key", ".env", ".ssh/id_rsa", "rotation.secret", ".aws/credentials",
                "pixi_remote_config.toml"):
        assert _resolve_within_root(str(tmp_path), rel) is None, rel
    assert _resolve_within_root(str(tmp_path), "README.md") is not None
