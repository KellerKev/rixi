"""`rixi exec`, `put`, `get`, `ls` — argument handling and exit codes, against a fake client."""
import pytest

from rixi import _cli


class FakeClient:
    def __init__(self):
        self.calls = []
        self.files = {}

    def exec_stream(self, cmd, cwd=None, env=None, timeout=None, stdin=None):
        self.calls.append(("exec", cmd, cwd, env, timeout))
        yield "stdout", "out\n"
        yield "stderr", "err\n"
        yield "exit", {"exit_code": 5 if cmd == "fail" else 0, "status": "completed"}

    def write_file(self, path, data):
        self.files[path] = data
        return {"path": path, "bytes": len(data)}

    def read_file(self, path):
        return self.files[path]

    def list_files(self, path="."):
        return [{"name": n, "type": "file", "size": len(d)} for n, d in self.files.items()]


@pytest.fixture()
def fake(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(_cli, "_client", lambda args: c)
    return c


def test_one_argument_is_a_shell_string_several_are_argv(fake, capsys):
    assert _cli.main(["exec", "ls -la | wc -l"]) == 0
    assert _cli.main(["exec", "--", "python3", "-V"]) == 0
    assert fake.calls[0][1] == "ls -la | wc -l"
    assert fake.calls[1][1] == ["python3", "-V"]
    out = capsys.readouterr()
    assert out.out.count("out") == 2 and out.err.count("err") == 2


def test_the_remote_exit_code_passes_through(fake):
    assert _cli.main(["exec", "fail"]) == 5


def test_exec_options(fake):
    _cli.main(["exec", "--cwd", "src", "--timeout", "30", "--env", "A=1", "--env", "B=x=y", "true"])
    assert fake.calls[-1] == ("exec", "true", "src", {"A": "1", "B": "x=y"}, 30.0)
    assert _cli.main(["exec", "--env", "broken", "true"]) == 1


def test_put_get_ls(fake, tmp_path, capsys):
    src = tmp_path / "a.txt"
    src.write_bytes(b"hello")
    assert _cli.main(["put", str(src)]) == 0
    assert fake.files == {"a.txt": b"hello"}
    dest = tmp_path / "back.txt"
    assert _cli.main(["get", "a.txt", str(dest)]) == 0
    assert dest.read_bytes() == b"hello"
    assert _cli.main(["ls"]) == 0
    assert "a.txt" in capsys.readouterr().out
