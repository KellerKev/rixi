"""The version the client reports must be the version of the release it came from.

`rixi --version` said 0.2.1 for three tagged releases, which is exactly the sort of thing that
wastes an hour when a box and a laptop disagree.
"""
import pathlib
import re

import rixi

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_package_version_matches_pyproject():
    declared = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(),
                         re.M).group(1)
    assert rixi.__version__ == declared, (
        "pyproject says %s, the package reports %s" % (declared, rixi.__version__))


def test_cli_reports_the_same_version(capsys):
    from rixi._cli import main
    try:
        main(["--version"])
    except SystemExit:
        pass
    assert rixi.__version__ in capsys.readouterr().out


def test_connection_flags_work_before_and_after_the_subcommand(monkeypatch):
    """`rixi run --server …` is what the docs show; `rixi --server … run` also has to work."""
    from rixi import _cli
    seen = {}

    class FakeResult:
        output = ""
        ok = True
        error = None

    class FakeClient:
        def __init__(self, server, token=None, aes_key=None, verify_ssl=True):
            seen.update(server=server, token=token, verify=verify_ssl)

        def run(self, project_dir, task="default", keep_alive=False):
            seen.update(task=task, dir=project_dir)
            return FakeResult()

    monkeypatch.setattr(_cli, "Client", FakeClient)
    monkeypatch.delenv("RIXI_SERVER", raising=False)
    monkeypatch.delenv("RIXI_TOKEN", raising=False)

    assert _cli.main(["run", "--server", "https://box.test", "--token", "t1",
                      "--task", "train", "./p"]) == 0
    assert seen["server"] == "https://box.test" and seen["token"] == "t1"
    assert seen["task"] == "train" and seen["dir"] == "./p"

    assert _cli.main(["--server", "https://other.test", "--token", "t2", "run",
                      "--task", "x", "./q"]) == 0
    assert seen["server"] == "https://other.test" and seen["token"] == "t2"

    monkeypatch.setenv("RIXI_SERVER", "https://from-env.test")
    monkeypatch.setenv("RIXI_TOKEN", "t3")
    assert _cli.main(["run"]) == 0
    assert seen["server"] == "https://from-env.test" and seen["token"] == "t3"
