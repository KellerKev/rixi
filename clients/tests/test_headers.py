# Tests for the custom request-header logic in rixi_transport.
import json

from rixi_transport import (
    build_custom_headers,
    resolve_placeholders,
    _parse_cli_header,
    mask_header_value,
)


def test_parse_cli_header():
    assert _parse_cli_header("X-Trace-Id: abc123") == ("X-Trace-Id", "abc123")
    # value may itself contain colons (e.g. a URL)
    assert _parse_cli_header("X-Url: http://x:9000") == ("X-Url", "http://x:9000")
    assert _parse_cli_header("malformed") is None
    assert _parse_cli_header(": no-key") is None


def test_resolve_env_placeholder(monkeypatch):
    monkeypatch.setenv("RIXI_TOKEN", "s3cr3t")
    assert resolve_placeholders("Bearer ${env:RIXI_TOKEN}") == "Bearer s3cr3t"
    # unset env resolves to empty
    monkeypatch.delenv("MISSING", raising=False)
    assert resolve_placeholders("${env:MISSING}") == ""


def test_resolve_file_placeholder(tmp_path):
    f = tmp_path / "tok.txt"
    f.write_text("  file-token\n")
    assert resolve_placeholders(f"Bearer ${{file:{f}}}") == "Bearer file-token"


def test_precedence_config_file_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    hf = tmp_path / "rixi_headers.json"
    hf.write_text(json.dumps({"X-Tenant": "from-file", "X-Only-File": "f"}))
    config = {"X-Tenant": "from-config", "X-Only-Config": "c"}
    cli = ["X-Tenant: from-cli", "X-Only-Cli: x"]

    out = build_custom_headers(cli, str(hf), config)
    # CLI wins over file wins over config
    assert out["X-Tenant"] == "from-cli"
    assert out["X-Only-Config"] == "c"
    assert out["X-Only-File"] == "f"
    assert out["X-Only-Cli"] == "x"


def test_empty_resolved_headers_dropped(monkeypatch):
    monkeypatch.delenv("UNSET_HDR", raising=False)
    out = build_custom_headers(["X-Empty: ${env:UNSET_HDR}", "X-Keep: ok"], None, {})
    assert "X-Empty" not in out
    assert out["X-Keep"] == "ok"


def test_default_when_nothing_set():
    assert build_custom_headers(None, None, {}) == {}
    assert build_custom_headers([], None, None) == {}


def test_comment_keys_ignored(tmp_path):
    hf = tmp_path / "h.json"
    hf.write_text(json.dumps({"//": "note", "_comment": "x", "X-Real": "1"}))
    out = build_custom_headers(None, str(hf), {})
    assert out == {"X-Real": "1"}


def test_mask_sensitive():
    assert mask_header_value("Authorization", "Bearer abc") == "***"
    assert mask_header_value("X-Api-Key", "abc") == "***"
    assert mask_header_value("X-Tenant", "acme") == "acme"
