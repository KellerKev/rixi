"""Tests for the metaflow-rixi extension that don't need a live box."""
import os

import pytest

pytest.importorskip("metaflow")


def test_rixi_step_decorator_registered():
    from metaflow.plugins import STEP_DECORATORS
    names = [getattr(d, "name", None) for d in STEP_DECORATORS]
    assert "rixi" in names


def test_rixi_importable_from_metaflow():
    import metaflow
    assert hasattr(metaflow, "rixi")


def test_runtime_step_cli_reroutes_to_rixi_step():
    from metaflow_extensions.rixi.plugins.rixi_decorator import RixiDecorator

    class _CliArgs:
        def __init__(self):
            self.commands = ["step"]
            self.command_args = ["mystep"]
            self.command_options = {}
            self.entrypoint = ["python", "flow.py"]

    d = RixiDecorator(attributes={"server": "http://box:9000"}, statically_defined=True)
    args = _CliArgs()
    d.runtime_step_cli(args, retry_count=0, max_user_code_retries=1, ubf_context=None)
    assert args.commands == ["rixi", "step"]
    assert args.command_options.get("server") == "http://box:9000"
    # teardown default is forwarded; a None attribute (resource) is not
    assert "resource" not in args.command_options


def test_staged_project_bundles_and_injects(tmp_path, monkeypatch):
    from metaflow_extensions.rixi.plugins import rixi_cli

    flow = tmp_path / "flow.py"
    flow.write_text("# flow\n")
    (tmp_path / "pixi.toml").write_text('[workspace]\nname="x"\n[tasks]\n')
    monkeypatch.setattr(rixi_cli.sys, "argv", [str(flow)])
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("METAFLOW_S3_ENDPOINT_URL", "http://minio:9000")

    tmp, proj = rixi_cli._staged_project("flow.py", "python -u flow.py step mystep", "rixi-step")
    try:
        script = open(os.path.join(proj, "rixi_step.sh")).read()
        assert "python -u flow.py step mystep" in script
        assert "AWS_ACCESS_KEY_ID=AKIATEST" in script          # S3 creds passed through
        assert "METAFLOW_S3_ENDPOINT_URL" in script
        assert 'rixi-step = "bash rixi_step.sh"' in open(os.path.join(proj, "pixi.toml")).read()
        # metaflow-rixi source bundled for the box to install without PyPI
        assert os.path.exists(os.path.join(proj, "_deps", "metaflow-rixi", "pyproject.toml"))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
