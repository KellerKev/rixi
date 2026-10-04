"""A plain `rixi run` must stream the task's output and its exit code.

The streaming loop reads the output buffer from the task record. Registering that record
only for keep-alive runs meant an ordinary run returned the opening statuses and nothing
else: no output, no exit code — while the task itself ran fine on the box.
"""
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import requests

HERE = Path(__file__).resolve().parent.parent


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    """A server whose PATH has a stub `pixi`, so the test needs no real pixi env.

    The stub must exist before the server starts: the task wrapper runs `pixi run
    --verbose <task>` with the server process's own PATH.
    """
    import os
    port = _free_port()
    stub = Path(tempfile.mkdtemp()) / "bin"
    stub.mkdir()
    (stub / "pixi").write_text("#!/bin/sh\nshift 2\nexec sh run.sh\n")
    (stub / "pixi").chmod(0o755)
    env = dict(os.environ, PATH=f"{stub}:{os.environ['PATH']}")
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "rixi_server.py"), "--host", "127.0.0.1",
         "--port", str(port), "--log-dir", tempfile.mkdtemp()],
        cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if requests.get(f"{url}/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.skip("server did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


def _package(tmp_path, script="echo 'hello from the task'", exit_code=0):
    """A project whose task runs with plain `sh`, so the test needs no pixi."""
    import tarfile
    import lz4.frame
    proj = tmp_path / ("proj-%d" % abs(hash((script, exit_code))))
    proj.mkdir(exist_ok=True)
    (proj / "run.sh").write_text(f"#!/bin/sh\n{script}\nexit {exit_code}\n")
    tar = proj.parent / (proj.name + ".tar")
    with tarfile.open(tar, "w") as t:
        t.add(proj / "run.sh", "run.sh")
    out = proj.parent / (proj.name + ".tar.lz4")
    with open(tar, "rb") as src, lz4.frame.open(out, "wb") as dst:
        dst.write(src.read())
    return out


def _run(url, pkg, task="run", keep=False):
    sys.path.insert(0, str(HERE.parent / "src"))
    from rixi.crypto import iter_frames
    with open(pkg, "rb") as fh:
        r = requests.post(f"{url}/upload",
                          files={"file": (pkg.name, fh, "application/octet-stream")},
                          data={"task_name": task, "keep_alive": str(keep).lower()},
                          stream=True, timeout=120)
        assert r.status_code == 200, r.text
        objs = []
        for payload in iter_frames(None, r.iter_content(chunk_size=4096)):
            text = payload.decode()
            for line in text.splitlines():
                if line.strip():
                    objs.append(json.loads(line))
    return objs


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs a shell")
def test_plain_run_streams_output_and_exit_code(server, tmp_path):
    objs = _run(server, _package(tmp_path))
    text = " ".join(json.dumps(o) for o in objs)
    assert "hello from the task" in text, f"no task output in stream: {text[:400]}"
    assert any(o.get("exit_code") == 0 for o in objs), f"no exit code in stream: {text[:400]}"
    assert all(o.get("task_id") for o in objs if "status" in o), "frames carry no task id"


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs a shell")
def test_failing_task_reports_its_exit_code(server, tmp_path):
    objs = _run(server, _package(tmp_path, script="echo nope >&2", exit_code=3))
    assert any(o.get("exit_code") == 3 for o in objs), objs[-2:]


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs a shell")
def test_a_run_that_is_not_kept_leaves_no_task_behind(server, tmp_path):
    objs = _run(server, _package(tmp_path))
    tid = next(o["task_id"] for o in objs if o.get("task_id"))
    assert requests.get(f"{server}/task/{tid}", timeout=10).status_code == 404
    kept = _run(server, _package(tmp_path), keep=True)
    ktid = next(o["task_id"] for o in kept if o.get("task_id"))
    assert requests.get(f"{server}/task/{ktid}", timeout=10).status_code == 200
