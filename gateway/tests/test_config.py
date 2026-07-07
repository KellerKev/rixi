# Unit tests for the TOML resource catalog (config.py) — parsing, substitution, lifecycle defaults.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gateway.config import ResourceDef, _duration, load_catalog, parse_resource  # noqa: E402


def test_parse_basic_and_aliases():
    rd = parse_resource("gpu-box", {
        "provider": "scaleway", "type": "L4-1-24G", "region": "fr-par-2",
        "image": "ubuntu_focal_gpu_os_12", "reuse": True,
        "teardown": "idle", "idle_timeout": "30m",
        "credentials": {"api_key": "AK", "secret": "SK"},
    })
    assert rd.provider == "scaleway"
    assert rd.vars["instance_type"] == "L4-1-24G"   # type → instance_type alias
    assert rd.vars["region"] == "fr-par-2"
    assert rd.vars["image"] == "ubuntu_focal_gpu_os_12"
    assert rd.reuse is True and rd.teardown == "idle"
    assert rd.idle_timeout == 1800.0
    assert rd.node_id == "res-gpu-box"
    # creds mapped to the provider's env vars
    assert rd.cred_env() == {"SCW_ACCESS_KEY": "AK", "SCW_SECRET_KEY": "SK"}


def test_teardown_defaults():
    assert parse_resource("a", {"reuse": True}).teardown == "manual"
    assert parse_resource("b", {"reuse": False}).teardown == "on_release"


def test_env_and_file_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "from-env")
    secret_file = tmp_path / "sk"
    secret_file.write_text("from-file\n")
    rd = parse_resource("x", {"provider": "scaleway", "credentials": {
        "api_key": "${env:MY_KEY}", "secret": f"${{file:{secret_file}}}"}})
    assert rd.credentials["api_key"] == "from-env"
    assert rd.credentials["secret"] == "from-file"          # trailing newline stripped
    assert rd.cred_env()["SCW_SECRET_KEY"] == "from-file"


def test_duration_forms():
    assert _duration("90s") == 90
    assert _duration("30m") == 1800
    assert _duration("2h") == 7200
    assert _duration("1d") == 86400
    assert _duration(45) == 45.0
    assert _duration(None) is None


def test_custom_module_and_passthrough_vars():
    rd = parse_resource("byo", {"module": "/abs/tofu", "vars": {"foo": "bar"}, "reuse": True})
    assert rd.module == "/abs/tofu"
    assert rd.vars["foo"] == "bar"


def test_key_secret_for_end_to_end(monkeypatch):
    monkeypatch.setenv("HS", "shared-handshake")
    rd = parse_resource("sec", {"provider": "scaleway", "type": "DEV1-S",
                                "key_secret": "${env:HS}", "key_secret_uses": 5})
    assert rd.key_secret == "shared-handshake"     # resolved via ${env:}
    assert rd.key_secret_uses == 5
    # key_secret is meta, not a passthrough tofu var
    assert "key_secret" not in rd.vars
    # default: no key_secret → open mode
    assert parse_resource("plain", {"provider": "scaleway"}).key_secret is None


def test_load_catalog_and_relative_module(tmp_path):
    (tmp_path / "rixi.toml").write_text(
        '[resource.gpu]\nprovider="scaleway"\ntype="L4-1-24G"\nreuse=true\n'
        '[resource.gpu.credentials]\napi_key="AK"\n'
        '[resource.byo]\nmodule="./my-tofu"\nreuse=false\n')
    cat = load_catalog(tmp_path / "rixi.toml")
    assert set(cat) == {"gpu", "byo"}
    assert cat["gpu"].vars["instance_type"] == "L4-1-24G"
    assert cat["byo"].module == str((tmp_path / "my-tofu").resolve())   # relative → resolved
    assert cat["byo"].teardown == "on_release"                           # reuse=false default


def test_missing_file_is_empty():
    assert load_catalog("/no/such/rixi.toml") == {}


def test_resourcedef_defaults():
    rd = ResourceDef(name="d")
    assert rd.provider == "dummy" and rd.reuse is True and rd.node_id == "res-d"
