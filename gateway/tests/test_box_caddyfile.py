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
GATE = "0a1b2c3d4e5f"


def render(tmp_path, serve=True, **extra) -> str:
    """Run just the Caddyfile section of the bootstrap."""
    src = BOOTSTRAP.read_text()
    start = src.index("mkdir -p /var/lib/caddy")
    end = src.index("mkdir -p /var/log/rixi")
    body = src[start:end].replace("/etc/rixi/Caddyfile", str(tmp_path / "Caddyfile"))
    env = {"RIXI_HOSTNAME": "b-abc.run.example.com", "RIXI_ACME_EMAIL": "ops@example.com",
           "RIXI_PREVIEW_GATE": GATE, "PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    if serve:
        env.update(RIXI_SERVE_KIND="ollama", RIXI_ENDPOINT_KEY=KEY)
    env.update(extra)
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


def test_previews_get_certificates_on_demand_and_every_request_is_checked(tmp_path):
    conf = render(tmp_path, serve=False)
    assert f"ask http://127.0.0.1:9000/preview/ask?gate={GATE}" in conf
    site = conf.split("*.b-abc.run.example.com {")[1].split("\nb-abc.run.example.com {")[0]
    assert "on_demand" in site
    assert "forward_auth 127.0.0.1:9000" in site and f"X-Rixi-Preview-Gate {GATE}" in site
    # b-abc.run.example.com has 4 labels, so <port>.b-abc… has the port at label 4
    assert "reverse_proxy 127.0.0.1:{http.request.host.labels.4}" in site
    main = conf.split("\nb-abc.run.example.com {")[1]
    assert main.index("handle @previewgate") < main.index("reverse_proxy 127.0.0.1:9000")


@pytest.mark.skipif(not shutil.which("caddy"), reason="needs the caddy binary")
def test_caddy_accepts_the_preview_config(tmp_path):
    render(tmp_path, serve=False)
    out = subprocess.run(["caddy", "adapt", "--config", str(tmp_path / "Caddyfile")],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert '"on_demand":true' in out.stdout.replace(" ", "") or "on_demand" in out.stdout


def test_lets_encrypt_alone_unless_another_ca_is_asked_for(tmp_path):
    assert "cert_issuer" not in render(tmp_path, serve=False)


def test_zerossl_first_and_lets_encrypt_as_fallback(tmp_path):
    conf = render(tmp_path, serve=False, RIXI_ACME_CA="zerossl")
    glob = conf.split("*.b-abc.run.example.com {")[0]
    assert glob.index("dir https://acme.zerossl.com/v2/DV90") < glob.index("  cert_issuer acme\n")
    assert "eab" not in conf.lower()                  # nothing secret for the CA on the box


@pytest.mark.skipif(not shutil.which("caddy"), reason="needs the caddy binary")
def test_caddy_accepts_the_zerossl_config(tmp_path):
    render(tmp_path, serve=False, RIXI_ACME_CA="zerossl")
    out = subprocess.run(["caddy", "adapt", "--config", str(tmp_path / "Caddyfile")],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "acme.zerossl.com" in out.stdout and "acme-v02.api.letsencrypt.org" not in out.stdout


def _workload_env(tmp_path, **env) -> str:
    """Run just the workload-environment section of the bootstrap."""
    src = BOOTSTRAP.read_text()
    start = src.index(": > /etc/rixi/workload.env")
    end = src.index("cat > /etc/systemd/system/rixi-server.service")
    body = src[start:end].replace("/etc/rixi/workload.env", str(tmp_path / "workload.env")) \
        .replace("/etc/profile.d/rixi-llm.sh", str(tmp_path / "profile.sh"))
    subprocess.run(["bash", "-c", body], env={"PATH": os.environ["PATH"], **env}, check=True,
                   capture_output=True)
    return (tmp_path / "workload.env").read_text()


def test_storage_and_model_access_both_reach_the_workload_environment(tmp_path):
    out = _workload_env(tmp_path, RIXI_LLM_TOKEN="tok-123456", RIXI_LLM_OPENAI_URL="https://m/v1",
                        RIXI_S3_ENDPOINT="https://s3.example.com", RIXI_S3_ACCESS_KEY="RXABC",
                        RIXI_S3_SECRET_KEY="sec", RIXI_S3_REGION="fr-par")
    assert "OPENAI_API_KEY=tok-123456" in out
    assert "AWS_ENDPOINT_URL_S3=https://s3.example.com" in out
    assert "AWS_ACCESS_KEY_ID=RXABC" in out and "AWS_SECRET_ACCESS_KEY=sec" in out
    assert "METAFLOW_S3_ENDPOINT_URL=https://s3.example.com" in out
    assert (tmp_path / "profile.sh").exists()


def test_a_box_without_extras_has_an_empty_workload_environment(tmp_path):
    assert _workload_env(tmp_path) == ""
    assert not (tmp_path / "profile.sh").exists()
