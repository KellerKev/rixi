"""The box's Caddyfile — the endpoint key is only as good as the config that enforces it.

Rendering it is shell, so this drives the real script with the serve variables set and checks
the result. Where a `caddy` binary is available it also asks Caddy to adapt the config, which
is what catches the class of bug the matcher had: a file that reads correctly but, because
Caddy sorts directives inside a `handle` block, answers 401 to every request.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

BOOTSTRAP = Path(__file__).resolve().parent.parent.parent / "box" / "bootstrap-direct.sh"
KEY = "test-key-AbC123_-xyz"


def render(tmp_path, serve=True) -> str:
    """Run just the Caddyfile section of the bootstrap."""
    src = BOOTSTRAP.read_text()
    start = src.index("mkdir -p /var/lib/caddy")
    end = src.index("mkdir -p /var/log/rixi")
    body = src[start:end].replace("/etc/rixi/Caddyfile", str(tmp_path / "Caddyfile"))
    env = {"RIXI_HOSTNAME": "b-abc.run.example.com", "RIXI_ACME_EMAIL": "ops@example.com",
           "PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    if serve:
        env.update(RIXI_SERVE_KIND="ollama", RIXI_ENDPOINT_KEY=KEY)
    body = body.replace("mkdir -p /var/lib/caddy", "mkdir -p %s/caddy" % tmp_path)
    subprocess.run(["bash", "-c", body], env=env, check=True, capture_output=True)
    return (tmp_path / "Caddyfile").read_text()


def test_the_key_guards_v1_and_nothing_else(tmp_path):
    conf = render(tmp_path)
    assert 'header Authorization "Bearer %s"' % KEY in conf
    # the authorised branch and the 401 must be separate handles: inside one block Caddy
    # would run `respond` before `reverse_proxy` and refuse every request.
    assert conf.index("handle @v1ok {") < conf.index("handle /v1/* {")
    assert "11434" in conf.split("handle @v1ok {")[1].split("}")[0] + \
        conf.split("handle @v1ok {")[1][:200]
    assert "respond" not in conf.split("handle @v1ok {")[1].split("handle /v1/*")[0]
    assert "header_up Host {upstream_hostport}" in conf      # ollama rejects a foreign Host
    assert "127.0.0.1:9000" in conf                          # the box API is still served


def test_a_plain_box_exposes_no_endpoint(tmp_path):
    conf = render(tmp_path, serve=False)
    assert "11434" not in conf and "Bearer" not in conf
    assert "127.0.0.1:9000" in conf


@pytest.mark.skipif(not shutil.which("caddy"), reason="needs the caddy binary")
def test_caddy_accepts_the_generated_config(tmp_path):
    conf_path = tmp_path / "Caddyfile"
    render(tmp_path)
    out = subprocess.run(["caddy", "adapt", "--config", str(conf_path)],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "11434" in out.stdout
