"""The sandboxed-environment primitives: run a command, move files — inside the workspace only."""
import asyncio
import base64
import json
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

import rixi_server

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))
from rixi.crypto import encrypt as seal, decrypt as unseal, iter_frames  # noqa: E402


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    monkeypatch.setattr(rixi_server, "WORKSPACE_ROOT", str(root))
    monkeypatch.setattr(rixi_server.auth_settings, "enabled", False)
    monkeypatch.setattr(rixi_server, "aes_key", None)
    monkeypatch.setattr(rixi_server, "require_encryption", False)
    return root


def client():
    return TestClient(rixi_server.app)


def frames(resp, key=None):
    out = []
    for payload in iter_frames(key, iter([resp.content])):
        for line in payload.decode().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def run(cmd, key=None, **kw):
    body = json.dumps({"cmd": cmd, **kw}).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        body, headers = seal(key, body), {"X-Rixi-Encrypted": "1",
                                          "Content-Type": "application/octet-stream"}
    r = client().post("/exec", content=body, headers=headers)
    return r, (frames(r, key) if r.status_code == 200 else None)


def text(fs, stream):
    return "".join(f.get(stream, "") for f in fs)


def test_exec_streams_output_and_ends_with_the_exit_code(ws):
    r, fs = run("echo hello; echo oops >&2")
    assert r.status_code == 200
    assert text(fs, "stdout") == "hello\n" and text(fs, "stderr") == "oops\n"
    assert fs[-1]["exit_code"] == 0 and fs[-1]["status"] == "completed"


def test_a_failing_command_reports_its_code(ws):
    _, fs = run("exit 3")
    assert fs[-1] == {**fs[-1], "exit_code": 3, "status": "failed"}


def test_argv_list_runs_without_a_shell(ws):
    _, fs = run(["printf", "%s", "a b; echo injected"])
    assert text(fs, "stdout") == "a b; echo injected"


def test_timeout_kills_the_command(ws):
    t0 = time.time()
    _, fs = run("sleep 30", timeout=0.5)
    assert fs[-1]["status"] == "timeout" and fs[-1]["exit_code"] == 124
    assert time.time() - t0 < 10


def test_cwd_env_and_stdin(ws):
    _, fs = run("pwd; echo $GREETING; cat", cwd="sub/dir", env={"GREETING": "hi"},
                stdin="from stdin")
    out = text(fs, "stdout").splitlines()
    assert out[0] == str((ws / "sub" / "dir").resolve())
    assert out[1:] == ["hi", "from stdin"]


@pytest.mark.parametrize("cwd", ["../", "/etc", "a/../../.."])
def test_cwd_outside_the_workspace_is_refused(ws, cwd):
    r, _ = run("pwd", cwd=cwd)
    assert r.status_code == 400


@pytest.mark.parametrize("body", [{"cmd": ""}, {"cmd": []}, {"cmd": 5}, {"cmd": "x", "env": {"A": 1}},
                                  {"cmd": "x", "timeout": -1}, {"cmd": "x", "timeout": 10 ** 9}])
def test_bad_exec_requests_are_refused(ws, body):
    assert client().post("/exec", json=body).status_code == 400


def test_files_round_trip(ws):
    c = client()
    blob = bytes(range(256)) * 10
    assert c.put("/files", params={"path": "data/blob.bin"}, content=blob).json() == \
        {"path": "data/blob.bin", "bytes": len(blob)}
    assert c.get("/files", params={"path": "data/blob.bin"}).content == blob
    listing = c.get("/files/list", params={"path": "data"}).json()
    assert [e["name"] for e in listing["entries"]] == ["blob.bin"]
    assert listing["entries"][0]["type"] == "file" and listing["entries"][0]["size"] == len(blob)
    assert c.delete("/files", params={"path": "data/blob.bin"}).status_code == 200
    assert c.get("/files", params={"path": "data/blob.bin"}).status_code == 404


def test_written_files_are_visible_to_exec(ws):
    client().put("/files", params={"path": "app.py"}, content=b"print(6 * 7)\n")
    _, fs = run("python3 app.py")
    assert text(fs, "stdout").strip() == "42"


@pytest.mark.parametrize("path", ["../escape", "/etc/passwd", "a/../../x"])
def test_file_paths_outside_the_workspace_are_refused(ws, path):
    c = client()
    assert c.get("/files", params={"path": path}).status_code == 400
    assert c.put("/files", params={"path": path}, content=b"x").status_code == 400


def test_a_symlink_out_of_the_workspace_is_refused(ws, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read")
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "link").symlink_to(secret)
    assert client().get("/files", params={"path": "link"}).status_code == 400


def test_the_workspace_itself_cannot_be_deleted(ws):
    assert client().delete("/files", params={"path": "."}).status_code == 400


def test_everything_needs_a_token_when_auth_is_on(ws, monkeypatch):
    monkeypatch.setattr(rixi_server.auth_settings, "enabled", True)
    monkeypatch.setattr(rixi_server.auth_settings, "public_key", None)
    monkeypatch.setattr(rixi_server.auth_settings, "jwks_keys", {})
    c = client()
    assert c.post("/exec", json={"cmd": "id"}).status_code == 401
    assert c.get("/files/list").status_code == 401
    assert c.put("/files", params={"path": "x"}, content=b"x").status_code == 401


def test_sealed_requests_and_responses_with_a_key(ws, monkeypatch):
    key = os.urandom(32)
    monkeypatch.setattr(rixi_server, "aes_key", key)
    r, fs = run("echo sealed", key=key)
    assert r.status_code == 200 and text(fs, "stdout") == "sealed\n"
    c = client()
    c.put("/files", params={"path": "s.txt"}, content=seal(key, b"secret bytes"),
          headers={"X-Rixi-Encrypted": "1"})
    got = c.get("/files", params={"path": "s.txt"})
    assert got.headers["x-rixi-encrypted"] == "1"
    assert unseal(key, got.content) == b"secret bytes"          # never in the clear on the wire
    listing = c.get("/files/list")
    assert "s.txt" in unseal(key, listing.content).decode()


def test_plaintext_is_refused_when_encryption_is_required(ws, monkeypatch):
    monkeypatch.setattr(rixi_server, "aes_key", os.urandom(32))
    monkeypatch.setattr(rixi_server, "require_encryption", True)
    c = client()
    assert c.post("/exec", json={"cmd": "id"}).status_code == 400
    assert c.put("/files", params={"path": "x"}, content=b"x").status_code == 400


def test_activity_is_counted_so_an_agent_driving_the_box_is_not_idle(ws):
    c = client()
    before = c.get("/health").json()["activity"]
    c.get("/health")                                           # the agent's own poll: not activity
    assert c.get("/health").json()["activity"] == before
    run("true")
    c.get("/files/list")
    assert c.get("/health").json()["activity"] == before + 2


def test_a_running_command_counts_as_busy_and_stops_counting_when_done(ws):
    ws.mkdir(parents=True, exist_ok=True)

    async def go():
        gen = rixi_server._exec_stream(["bash", "-c", "echo started; sleep 0.3"], ws,
                                       dict(os.environ), 5, None)
        await gen.__anext__()                                  # the first output frame
        busy = rixi_server.active_execs
        async for _ in gen:
            pass
        return busy, rixi_server.active_execs

    busy, after = asyncio.run(go())
    assert busy == 1 and after == 0
    assert TestClient(rixi_server.app).get("/health").json()["active_tasks"] == \
        len(rixi_server.running_tasks)
