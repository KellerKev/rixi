"""Long-running work: background commands as tasks — attach (with or without history), stdin,
restart, kill — and what counts as busy."""
import json
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

import rixi_server

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))
from rixi.crypto import iter_frames  # noqa: E402


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    monkeypatch.setattr(rixi_server, "WORKSPACE_ROOT", str(root))
    monkeypatch.setattr(rixi_server.auth_settings, "enabled", False)
    monkeypatch.setattr(rixi_server, "aes_key", None)
    monkeypatch.setattr(rixi_server, "require_encryption", False)
    yield root
    for tid in list(rixi_server.running_tasks):
        rixi_server.cleanup_task(tid)


def client():
    return TestClient(rixi_server.app)


def start(cmd, **kw):
    r = client().post("/exec", json={"cmd": cmd, "background": True, **kw})
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def frames(resp):
    out = []
    for payload in iter_frames(None, iter([resp.content])):
        out += [json.loads(line) for line in payload.decode().splitlines() if line.strip()]
    return out


def wait(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def status(tid):
    return client().get("/tasks").json()[tid]


def test_a_background_command_returns_at_once_and_can_be_attached_with_history(ws):
    tid = start("echo one; echo err >&2; sleep 0.3; echo two")
    t = status(tid)
    assert t["kind"] == "exec" and t["status"] == "running"
    fs = frames(client().get(f"/task/{tid}/stream"))         # replays, follows, ends
    out = "".join(f.get("output", "") for f in fs)
    assert out == "one\ntwo\n" and "".join(f.get("stderr", "") for f in fs) == "err\n"
    assert fs[-1]["exit_code"] == 0
    assert wait(lambda: status(tid)["status"] == "completed")
    assert status(tid)["exit_code"] == 0


def test_attach_without_history_follows_only_new_output(ws):
    tid = start("echo early; sleep 0.6; echo late")
    assert wait(lambda: any("early" in c for _, c in rixi_server.running_tasks[tid]["output_lines"]))
    fs = frames(client().get(f"/task/{tid}/stream", params={"history": "false"}))
    out = "".join(f.get("output", "") for f in fs)
    assert out == "late\n"


def test_several_run_side_by_side(ws):
    a, b = start("sleep 0.4; echo a"), start("sleep 0.4; echo b")
    assert a != b
    assert {status(a)["status"], status(b)["status"]} == {"running"}
    for tid, word in ((a, "a"), (b, "b")):
        fs = frames(client().get(f"/task/{tid}/stream"))
        assert "".join(f.get("output", "") for f in fs) == f"{word}\n"


def test_raw_stdin_reaches_an_interactive_program(ws):
    tid = start("read name; echo hello $name; read again; echo bye $again")
    r = client().post(f"/task/{tid}/input", params={"raw": "1"}, content=b"ada\n",
                      headers={"Content-Type": "text/plain"})
    assert r.status_code == 200
    client().post(f"/task/{tid}/input", params={"raw": "1"}, content=b"bob\n",
                  headers={"Content-Type": "text/plain"})
    fs = frames(client().get(f"/task/{tid}/stream"))
    assert "".join(f.get("output", "") for f in fs) == "hello ada\nbye bob\n"


def test_restart_runs_the_command_again_and_kill_removes_it(ws):
    (ws / "count").parent.mkdir(parents=True, exist_ok=True)
    tid = start("echo run >> count; sleep 30")
    assert wait(lambda: (ws / "count").exists())
    assert client().post(f"/task/{tid}/restart").json()["status"] == "restarted"
    assert wait(lambda: (ws / "count").read_text().count("run") == 2)
    assert client().post(f"/task/{tid}/redeploy",
                         files={"file": ("x", b"")}).status_code == 400
    proc = rixi_server.running_tasks[tid]["process"]
    assert client().delete(f"/task/{tid}").json()["status"] == "terminated"
    assert wait(lambda: proc.poll() is not None)
    assert tid not in client().get("/tasks").json()


def test_a_timeout_stops_a_background_command(ws):
    tid = start("sleep 30", timeout=0.5)
    assert wait(lambda: status(tid)["status"] in ("failed", "completed"), timeout=5)


def test_only_running_processes_count_as_busy(ws):
    tid = start("sleep 0.5")
    assert client().get("/health").json()["active_tasks"] == 1
    assert wait(lambda: rixi_server.running_tasks[tid]["process"].poll() is not None)
    # finished but kept for its output: not work, so an idle box can still be released
    assert client().get("/health").json()["active_tasks"] == 0
    assert tid in client().get("/tasks").json()


def test_background_commands_stay_inside_the_workspace(ws):
    r = client().post("/exec", json={"cmd": "pwd", "background": True, "cwd": "../.."})
    assert r.status_code == 400


def test_a_detached_upload_answers_at_once_and_keeps_running(ws, tmp_path, monkeypatch):
    calls = []

    async def fake_run(pkg, task, tid, keep=True):     # no pixi here: stand in for the runner
        calls.append((task, tid, keep))
        rixi_server.running_tasks[tid] = {"status": "running", "task_name": task,
                                          "output_lines": [], "process": None}
        yield b""
    monkeypatch.setattr(rixi_server, "extract_and_run", fake_run)
    r = client().post("/upload", data={"task_name": "work", "keep_alive": "true",
                                       "detach": "true"},
                      files={"file": ("p.tar.lz4", b"\x04\x22\x4d\x18", "application/octet-stream")})
    assert r.status_code == 200 and r.json()["status"] == "starting"
    tid = r.json()["task_id"]
    assert tid in rixi_server.running_tasks          # attachable before the run gets going
    assert wait(lambda: calls == [("work", tid, True)])
