"""The SDK's exec and files methods against a real server process — plain and AES-sealed."""
import base64
import os
import socket
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


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(extra=()):
    port, ws = _port(), tempfile.mkdtemp()
    proc = subprocess.Popen([sys.executable, str(HERE / "rixi_server.py"), "--port", str(port),
                             "--workspace", ws, "--log-dir", tempfile.mkdtemp(), *extra],
                            cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if requests.get(f"{url}/health", timeout=1).ok:
                return proc, url, ws
        except requests.RequestException:
            time.sleep(0.1)
    proc.kill()
    pytest.skip("server did not start")


@pytest.fixture(scope="module")
def plain():
    proc, url, ws = _start()
    yield Client(url), ws
    proc.terminate()


@pytest.fixture(scope="module")
def sealed():
    key = os.urandom(32)
    kf = Path(tempfile.mkdtemp()) / "aes.key"
    kf.write_text(base64.b64encode(key).decode())
    proc, url, ws = _start(["--aes-key", str(kf), "--require-encryption"])
    yield Client(url, aes_key=base64.b64encode(key).decode()), Client(url), ws
    proc.terminate()


def test_an_agent_loop_writes_runs_and_reads(plain):
    c, _ = plain
    c.write_file("job/main.py", "import json\nprint(json.dumps({'answer': 6 * 7}))\n")
    r = c.exec("python3 main.py > out.json", cwd="job")
    assert r.ok, r
    assert c.read_file("job/out.json").strip() == b'{"answer": 42}'
    assert {e["name"] for e in c.list_files("job")} == {"main.py", "out.json"}
    c.delete_file("job")
    assert c.list_files(".") == []


def test_exec_streams_and_reports_failure(plain):
    c, _ = plain
    events = list(c.exec_stream("echo one; echo two >&2; exit 4"))
    kinds = [k for k, _ in events]
    assert kinds[-1] == "exit" and events[-1][1]["exit_code"] == 4
    r = c.exec(["python3", "-c", "import sys; print('x'); sys.exit(2)"])
    assert r.exit_code == 2 and r.stdout == "x\n" and not r.ok


def test_errors_surface_as_rixi_errors(plain):
    from rixi import RixiError
    c, _ = plain
    with pytest.raises(RixiError, match="outside the workspace"):
        c.read_file("../../etc/passwd")
    with pytest.raises(RixiError, match="no such file"):
        c.read_file("missing.txt")


def test_everything_is_sealed_with_a_key(sealed):
    keyed, unkeyed, _ = sealed
    keyed.write_file("s.txt", b"confidential")
    assert keyed.read_file("s.txt") == b"confidential"
    assert keyed.exec("cat s.txt").stdout == "confidential"
    from rixi import RixiError
    with pytest.raises(RixiError):                     # a client without the key is refused
        unkeyed.exec("cat s.txt")


def _collect(events):
    out, err, end = "", "", None
    for kind, value in events:
        if kind == "stdout":
            out += value
        elif kind == "stderr":
            err += value
        else:
            end = value
    return out, err, end


def _lifecycle(c):
    tid = c.spawn("read who; echo hi $who; sleep 0.3; echo done; echo warn >&2")
    assert c.tasks()[tid]["status"] == "running"
    c.send_input(tid, "ada\n")
    out, err, end = _collect(c.attach(tid))
    assert out == "hi ada\ndone\n" and err == "warn\n" and end["exit_code"] == 0
    assert "hi ada" in c.output(tid)
    out, _, _ = _collect(c.attach(tid, history=False))      # finished: nothing new
    assert out == ""
    c.restart(tid)
    c.send_input(tid, "bob\n")
    out, _, _ = _collect(c.attach(tid))
    assert out == "hi bob\ndone\n"
    c.kill(tid)
    assert tid not in c.tasks()


def test_background_commands_attach_feed_restart_and_kill(plain):
    _lifecycle(plain[0])


def test_the_task_lifecycle_works_sealed_too(sealed):
    _lifecycle(sealed[0])


@pytest.mark.skipif(not __import__("shutil").which("pixi"), reason="needs pixi on PATH")
def test_a_pixi_task_started_in_the_background_can_be_attached(plain):
    c, _ = plain
    tid = c.start(str(HERE.parent / "examples" / "hello"), task="hello")
    assert c.tasks()[tid]["status"] in ("queued", "extracting", "starting", "running", "completed")
    out, _, end = _collect(c.attach(tid))
    assert "Hello from RIXI!" in out and end["exit_code"] == 0
    assert c.tasks()[tid]["status"] == "completed"           # kept for its output
    c.kill(tid)


def _shell(client, script: bytes, cwd=None):
    """Drive `rixi.shell.interactive` with piped (non-tty) input; collect what it prints."""
    import subprocess as sp
    code = (
        "import sys, os; sys.path.insert(0, %r)\n"
        "from rixi import Client\n"
        "from rixi.shell import interactive\n"
        "c = Client(%r, aes_key=%r)\n"
        "sys.exit(interactive(c, cwd=%r))\n"
    ) % (str(HERE.parent / "src"), client.server_url,
         __import__("base64").b64encode(client.aes_key).decode() if client.aes_key else None, cwd)
    r = sp.run([sys.executable, "-c", code], input=script, capture_output=True, timeout=60)
    return r.returncode, r.stdout


def test_an_interactive_shell_over_the_websocket(plain):
    c, ws = plain
    code, out = _shell(c, b"echo SHELL-$((40+2))\nexit 7\n", cwd="term")
    assert b"SHELL-42" in out and code == 7
    assert os.path.isdir(os.path.join(ws, "term"))


def test_the_shell_is_sealed_with_a_key(sealed):
    c, _, _ = sealed
    code, out = _shell(c, b"echo SEALED-$((1+1))\nexit\n")
    assert b"SEALED-2" in out and code == 0
