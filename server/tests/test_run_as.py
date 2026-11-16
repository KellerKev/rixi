"""Workloads as a user of their own (--run-as).

With the server as root and callers' code as an ordinary user, what the server guards — the list
of revoked tokens, the box's own credentials, its agent — is out of the callers' reach. Two rules
make that hold and are what these tests pin down: root never writes into a directory the workload
user controls, and the file API acts as that user, so the kernel decides what it may reach.

These run the server as the current user with --run-as naming that same user: every code path of
the mode except the switch of user itself, which needs root.
"""
import getpass
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import requests

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE.parent / "src"))
from rixi import Client  # noqa: E402
from rixi.client import RixiError  # noqa: E402

import rixi_server  # noqa: E402


@pytest.fixture(scope="module")
def box():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    ws, base = tempfile.mkdtemp(), tempfile.mkdtemp()
    stub = Path(tempfile.mkdtemp())
    (stub / "pixi").write_text('#!/bin/sh\nwhile [ $# -gt 1 ]; do shift; done\nexec sh "$1.sh"\n')
    (stub / "pixi").chmod(0o755)
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "rixi_server.py"), "--port", str(port), "--workspace", ws,
         "--log-dir", tempfile.mkdtemp(), "--run-as", getpass.getuser()],
        cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "RIXI_LOCKED_BASE": base, "PATH": f"{stub}:{os.environ['PATH']}",
             "RIXI_PREVIEW_GATE": "the-gate-secret"})
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if requests.get(f"{url}/health", timeout=1).ok:
                break
        except requests.RequestException:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.skip("server did not start")
    yield Client(url), Path(ws), Path(base)
    proc.terminate()


def test_files_commands_and_tasks_work_as_the_workload_user(box):
    c, ws, base = box
    c.write_file("job/main.py", "print(6 * 7)\n")
    assert c.read_file("job/main.py") == b"print(6 * 7)\n"
    r = c.exec("python3 main.py && id -un && echo $HOME", cwd="job")
    assert r.ok and r.stdout.split()[0] == "42" and r.stdout.split()[1] == getpass.getuser()
    assert {e["name"] for e in c.list_files("job")} == {"main.py"}
    with pytest.raises(RixiError, match="404"):
        c.read_file("job/nope")
    for outside in ("../../etc/passwd", "/etc/passwd"):
        with pytest.raises(RixiError, match="outside the workspace"):
            c.read_file(outside)
    c.delete_file("job")
    assert c.list_files(".") == []
    tid = c.spawn("echo started; sleep 0.2; echo done", cwd="bg/deep")       # cwd is made for it
    time.sleep(1.0)
    assert "done" in c.output(tid) and (ws / "bg" / "deep").is_dir()


def test_a_link_out_of_the_workspace_is_followed_by_nobody(box):
    c, ws, _ = box
    secret = Path(tempfile.mkdtemp()) / "revoked_jti"
    secret.write_text("the servers own file\n")
    c.exec(f"ln -s {secret} leak && ln -s {secret.parent} dirleak")
    for path in ("leak", "dirleak/revoked_jti"):
        with pytest.raises(RixiError, match="outside the workspace"):
            c.read_file(path)
        with pytest.raises(RixiError, match="outside the workspace"):
            c.write_file(path, "overwritten")
    assert secret.read_text() == "the servers own file\n"


def test_a_tasks_start_script_and_directory_are_not_the_tasks_to_change(box, tmp_path):
    c, ws, base = box
    (tmp_path / "pixi.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "hello.sh").write_text("echo hello from the task; pwd\n")
    out = c.run(str(tmp_path), task="hello")
    assert out.exit_code == 0 and "hello from the task" in out.output
    assert str(base / "tasks") in out.output                 # the task ran under the locked base
    wrappers = list((base / "wrappers").iterdir())
    assert wrappers and all(w.parent == base / "wrappers" for w in wrappers)
    # no start script inside any task directory (where its task could rewrite it)
    assert not list((base / "tasks").rglob("*wrapper.sh"))
    assert stat.S_IMODE((base / "tasks").stat().st_mode) == 0o711


def test_the_servers_own_secrets_are_not_in_a_workloads_environment(box):
    c, _, _ = box
    r = c.exec("env")
    assert "the-gate-secret" not in r.stdout and "RIXI_PREVIEW_GATE" not in r.stdout


def test_the_mode_refuses_what_it_cannot_do():
    with pytest.raises(SystemExit, match="no user"):
        rixi_server.Workload("no-such-user-here")
    with pytest.raises(SystemExit):
        rixi_server.Workload("root")                         # root, or not allowed to become it
    me = rixi_server.Workload(getpass.getuser())
    assert me.popen() == {} and me.env({"HOME": "/root", "RIXI_KEY_SECRET": "s", "A": "1"}) == {
        "HOME": me.home, "USER": me.name, "LOGNAME": me.name, "A": "1"}


def test_as_root_the_process_arguments_and_the_hand_over_name_the_user(tmp_path, monkeypatch):
    me = rixi_server.Workload(getpass.getuser())
    me.switch = True                                         # as it is when the server is root
    assert me.popen() == {"user": me.uid, "group": me.gid, "extra_groups": me.groups}
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "sub" / "f.py").write_text("x")
    (tmp_path / "pkg" / "link").symlink_to("/etc")           # in the package: owned, not followed
    order = []
    monkeypatch.setattr(os, "lchown", lambda path, uid, gid: order.append((path, uid, gid)))
    me.hand_over(str(tmp_path / "pkg"))
    assert order[-1][0] == str(tmp_path / "pkg")             # the directory itself goes last
    assert {p for p, _, _ in order} == {str(tmp_path / "pkg" / x)
                                        for x in ("", "sub", "sub/f.py", "link")} | {
        str(tmp_path / "pkg")} - {str(tmp_path / "pkg") + "/"}
    assert not any(p.startswith("/etc") for p, _, _ in order)
    assert all((uid, gid) == (me.uid, me.gid) for _, uid, gid in order)
