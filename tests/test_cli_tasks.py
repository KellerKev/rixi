"""`rixi run --detach`, `exec --background`, `tasks`, `attach`, `logs`, `input`, `restart`,
`kill`, `redeploy` — against a fake client."""
import pytest

from rixi import _cli


class FakeClient:
    def __init__(self):
        self.calls = []

    def start(self, project_dir, task="default"):
        self.calls.append(("start", project_dir, task))
        return "t-run"

    def spawn(self, cmd, cwd=None, env=None, timeout=None):
        self.calls.append(("spawn", cmd, cwd, env, timeout))
        return "t-exec"

    def tasks(self):
        return {"t-exec": {"status": "completed", "exit_code": 3, "kind": "exec",
                           "task_name": "make", "started_at": 2},
                "t-run": {"status": "running", "kind": "task", "task_name": "train",
                          "started_at": 1}}

    def attach(self, task_id, history=True):
        self.calls.append(("attach", task_id, history))
        yield "stdout", "epoch 1\n"
        yield "stderr", "warn\n"
        yield "exit", {"exit_code": 3}

    def output(self, task_id):
        return "so far\n"

    def send_input(self, task_id, data, raw=True):
        self.calls.append(("input", task_id, data, raw))
        return {}

    def restart(self, task_id):
        self.calls.append(("restart", task_id))

    def kill(self, task_id):
        self.calls.append(("kill", task_id))

    def redeploy(self, task_id, project_dir="."):
        self.calls.append(("redeploy", task_id, project_dir))


@pytest.fixture()
def fake(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(_cli, "_client", lambda args: c)
    return c


def test_background_starts_print_the_task_id(fake, capsys):
    assert _cli.main(["run", "--detach", "--task", "train", "proj"]) == 0
    assert _cli.main(["exec", "--background", "--cwd", "app", "npm run dev"]) == 0
    assert fake.calls == [("start", "proj", "train"), ("spawn", "npm run dev", "app", None, None)]
    assert capsys.readouterr().out.split() == ["t-run", "t-exec"]


def test_tasks_lists_oldest_first_with_exit_codes(fake, capsys):
    assert _cli.main(["tasks"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("t-run") and "running" in lines[0]
    assert lines[1].startswith("t-exec") and "(exit 3)" in lines[1] and "make" in lines[1]


def test_attach_streams_and_returns_the_tasks_exit_code(fake, capsys):
    assert _cli.main(["attach", "t-run", "--no-history"]) == 3
    out = capsys.readouterr()
    assert out.out == "epoch 1\n" and out.err == "warn\n"
    assert fake.calls[-1] == ("attach", "t-run", False)


def test_logs_input_restart_kill_redeploy(fake, capsys):
    assert _cli.main(["logs", "t-run"]) == 0 and capsys.readouterr().out == "so far\n"
    _cli.main(["input", "t-run", "yes"])
    _cli.main(["input", "t-run", "--json", '{"command": "generate"}'])
    _cli.main(["restart", "t-run"])
    _cli.main(["kill", "t-run"])
    _cli.main(["redeploy", "t-run", "proj"])
    assert fake.calls == [("input", "t-run", "yes\n", True),
                          ("input", "t-run", {"command": "generate"}, False),
                          ("restart", "t-run"), ("kill", "t-run"),
                          ("redeploy", "t-run", "proj")]


class PreviewClient(FakeClient):
    def open_preview(self, port, public=False):
        self.calls.append(("open", port, public))
        url = f"https://{port}.b-x.run.test/"
        return {"port": port, "public": public, "url": url,
                "share_url": url if public else url + "?rixi_preview=tok"}

    def previews(self):
        return [self.open_preview(8000)]

    def close_preview(self, port):
        self.calls.append(("close", port))


def test_preview_open_list_close(monkeypatch, capsys):
    c = PreviewClient()
    monkeypatch.setattr(_cli, "_client", lambda args: c)
    assert _cli.main(["preview", "8000"]) == 0
    assert capsys.readouterr().out.strip() == "https://8000.b-x.run.test/?rixi_preview=tok"
    _cli.main(["preview", "3000", "--public"])
    assert capsys.readouterr().out.strip() == "https://3000.b-x.run.test/"
    _cli.main(["preview", "--list"])
    assert "8000  private" in capsys.readouterr().out
    _cli.main(["preview", "8000", "--close"])
    assert ("close", 8000) in c.calls
