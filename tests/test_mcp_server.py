"""`rixi mcp`: the protocol (JSON-RPC over stdio) and the tools, over a fake box."""
import io
import json

import pytest

from rixi import ExecResult
from rixi.cloud.profiles import Profile, ProfileStore
from rixi.mcp_server import TOOLS, Server, Tools


@pytest.fixture(autouse=True)
def _config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RIXI_CONFIG_DIR", str(tmp_path / "rixi"))
    monkeypatch.delenv("RIXI_PROFILE", raising=False)


class Box:
    def __init__(self):
        self.files, self.tasks_ = {}, {}

    def exec(self, cmd, cwd=None, timeout=None):
        return ExecResult(exit_code=0 if "fail" not in cmd else 2, status="completed",
                          stdout=f"ran {cmd}\n", stderr="")

    def spawn(self, cmd, cwd=None):
        self.tasks_["t1"] = {"status": "running", "task_name": cmd, "started_at": 1}
        return "t1"

    def tasks(self):
        return self.tasks_

    def output(self, tid):
        return "serving on :8000\n"

    def send_input(self, tid, text):
        self.sent = text

    def kill(self, tid):
        self.tasks_.pop(tid)

    def write_file(self, path, content):
        self.files[path] = content
        return {"path": path, "bytes": len(content)}

    def read_file(self, path):
        return self.files[path].encode()

    def list_files(self, path="."):
        return [{"name": n, "type": "file", "size": len(c)} for n, c in self.files.items()]

    def preview(self, port, public=False):
        url = f"https://{port}.b-x.run.test/"
        return {"port": port, "public": public, "share_url": url + ("" if public else "?t=1")}


def _store_with(name="b1"):
    store = ProfileStore()
    store.put(Profile(name=name, provider="eutonomous", server_url="https://b", ip="",
                      region="fr-par-2", instance_type="DEV1-M", box_id="x", audience="",
                      rixi_ref="", created_at=0, status="ready"), make_default=False)
    return store


def _server(box=None, **kw):
    box = box or Box()
    store = kw.pop("store", None) or _store_with()
    return Server(Tools(store=store, sandbox_for=lambda name: box, sleep=lambda s: None,
                        **kw)), box


def rpc(server, method, params=None, mid=1):
    return server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def call(server, _tool, **args):
    r = rpc(server, "tools/call", {"name": _tool, "arguments": args})["result"]
    return r["content"][0]["text"], r["isError"]


def test_the_handshake_and_tool_list():
    s, _ = _server()
    init = rpc(s, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                 "clientInfo": {"name": "t", "version": "1"}})["result"]
    assert init["protocolVersion"] == "2025-03-26" and "tools" in init["capabilities"]
    assert init["serverInfo"]["name"] == "rixi"
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in rpc(s, "tools/list")["result"]["tools"]}
    assert {"create_box", "exec", "spawn", "task_output", "destroy_box"} <= names
    assert all(t["inputSchema"]["type"] == "object" for t in TOOLS)
    assert rpc(s, "nope")["error"]["code"] == -32601


def test_an_agent_loop_over_tools():
    s, box = _server()
    assert call(s, "write_file", box="b1", path="app.py", content="print(1)") == \
        ("wrote app.py (8 bytes)", False)
    text, err = call(s, "exec", box="b1", command="python3 app.py")
    assert not err and "exit code: 0" in text and "ran python3 app.py" in text
    assert "exit code: 2" in call(s, "exec", box="b1", command="fail")[0]
    assert call(s, "read_file", box="b1", path="app.py")[0] == "print(1)"
    assert "app.py" in call(s, "list_files", box="b1")[0]


def test_background_work_over_tools():
    s, box = _server()
    assert call(s, "spawn", box="b1", command="python3 -m http.server")[0] == \
        "started; task id: t1"
    assert "t1: running" in call(s, "list_tasks", box="b1")[0]
    out = call(s, "task_output", box="b1", task_id="t1", wait_seconds=2)[0]
    assert out.startswith("status: running") and "serving on :8000" in out
    call(s, "send_input", box="b1", task_id="t1", text="q")
    assert box.sent == "q\n"
    assert call(s, "kill_task", box="b1", task_id="t1")[0] == "killed"
    assert call(s, "task_output", box="b1", task_id="t1") == ("no task 't1' on this box", True)


def test_create_and_destroy_go_through_rixi_up(monkeypatch):
    made, gone = [], []

    def fake_up(**kw):
        made.append(kw)
        return Profile(name=kw["name"], provider=kw["provider"], server_url="https://b", ip="",
                       region="fr-par-2", instance_type="DEV1-M", box_id="x", audience="",
                       rixi_ref="", created_at=0, status="ready")
    store = _store_with("old")
    s, _ = _server(store=store, up=fake_up, down=lambda name, creds, **kw: gone.append(name))
    text, err = call(s, "create_box", egress="block", model_access="governed")
    assert not err and "shuts down after 2 h" in text
    kw = made[0]
    assert kw["provider"] == "eutonomous" and kw["size"] == "cpu-s" and kw["ttl_hours"] == 2
    assert kw["policy"] == {"egress": {"mode": "block", "allow": []}}
    assert kw["model_access"] == "governed" and kw["make_default"] is False
    call(s, "create_box", provider="hetzner")
    assert made[1]["size"] == "sample-cpu" and "ttl_hours" not in made[1]
    assert call(s, "destroy_box", box="old")[0] == "box 'old' destroyed" and gone == ["old"]
    assert call(s, "destroy_box", box="ghost")[1] is True


def test_failures_are_results_not_crashes():
    s, _ = _server()
    assert call(s, "exec", box="b1")[1] is True                       # missing command
    assert call(s, "no_such_tool")[1] is True
    s2 = Server(Tools(store=_store_with()))
    text, err = call(s2, "exec", box="ghost", command="ls")
    assert err and "no box named 'ghost'" in text


def test_stdio_carries_only_protocol_lines():
    s, _ = _server()
    stdin = io.StringIO("\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "not json",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]) + "\n")
    stdout = io.StringIO()
    s.serve(stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, 2]
    assert replies[1]["error"]["code"] == -32700


def test_previews_over_tools():
    s, _ = _server()
    assert call(s, "open_preview", box="b1", port=5173)[0] == \
        "private preview: https://5173.b-x.run.test/?t=1"
    assert call(s, "open_preview", box="b1", port="80")[1] is True


def test_persistence_over_tools():
    class Api:
        def __init__(self):
            self.vols = {}

        def volumes(self):
            return list(self.vols.values())

        def create_volume(self, name, size_gb, zone=None):
            self.vols["v1"] = {"id": "v1", "name": name, "size_gb": size_gb,
                               "zone": zone or "fr-par-2", "state": "creating", "box_id": None}
            return self.vols["v1"]

        def delete_volume(self, vid):
            self.vols.pop(vid)

    class StoppableBox(Box):
        def stop(self):
            self.stopped = True

        def resume(self):
            self.stopped = False
    box = StoppableBox()
    api = Api()
    s = Server(Tools(store=_store_with(), sandbox_for=lambda name: box, api=api))
    assert "v1 (data, 50 GB" in call(s, "create_volume", name="data", size_gb=50)[0]
    assert "v1: data 50 GB" in call(s, "list_volumes")[0]
    assert call(s, "create_volume", name="x", size_gb="big")[1] is True
    assert call(s, "stop_box", box="b1")[0].startswith("stopping") and box.stopped
    assert call(s, "resume_box", box="b1")[0] == "running again" and not box.stopped
    assert call(s, "delete_volume", volume_id="v1")[0] == "deleting" and api.vols == {}
