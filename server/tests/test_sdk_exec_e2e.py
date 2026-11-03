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
