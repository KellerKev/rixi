"""Tests for the rixi client SDK packaging and stream parsing."""
import tarfile
from pathlib import Path

import pytest

from rixi.client import Client, RixiError, _objects, _texts


def test_package_creates_lz4_tar(tmp_path):
    (tmp_path / "hello.py").write_text("print('hi')\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "junk").write_text("nope")
    (tmp_path / "pkg.egg-info").mkdir()  # not in default ignore, should be included

    pkg = Client().package(str(tmp_path))
    try:
        assert pkg.endswith(".lz4")
        import lz4.frame
        raw = lz4.frame.open(pkg, "rb").read()
        tar_path = Path(pkg + ".tar")
        tar_path.write_bytes(raw)
        with tarfile.open(tar_path) as t:
            names = t.getnames()
        # code is packaged; the .git dir is skipped by DEFAULT_IGNORES
        assert "hello.py" in names
        assert not any(n.startswith(".git") for n in names)
    finally:
        Path(pkg).unlink(missing_ok=True)


def test_package_rejects_missing_dir():
    with pytest.raises(RixiError):
        Client().package("/nonexistent/dir/xyz")


def test_objects_peels_concatenated_json():
    payload = b'{"a":1}{"b":2}  {"c":3}'
    assert list(_objects(payload)) == [{"a": 1}, {"b": 2}, {"c": 3}]


def test_texts_extracts_output_and_errors():
    payload = b'{"output":"hello "}{"stderr":"warn"}{"error":"boom"}'
    assert list(_texts(payload)) == ["hello ", "warn", "\nError: boom\n"]


def test_headers_include_bearer_when_token_set():
    assert Client(token="abc")._headers() == {"Authorization": "Bearer abc"}
    assert Client()._headers() == {}


# ── what a package leaves out ────────────────────────────────────────────────

def _names(pkg_path):
    import io
    import tarfile

    import lz4.frame
    with lz4.frame.open(pkg_path, "rb") as fh:
        with tarfile.open(fileobj=io.BytesIO(fh.read())) as tar:
            return sorted(tar.getnames())


def _project(tmp_path, files):
    for rel in files:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    return tmp_path


SECRETS = [".env", ".env.production", "deploy/server.pem", "id_ed25519", ".npmrc",
           ".aws/credentials", ".ssh/id_rsa", "aes.key", "conf/prod.tfvars", ".DS_Store"]
CODE = ["pixi.toml", "main.py", ".env.example", "src/app.py", ".gitignore"]


def test_credentials_in_the_project_are_not_shipped_by_default(tmp_path, caplog):
    import os

    from rixi import Client
    root = _project(tmp_path, SECRETS + CODE)
    with caplog.at_level("WARNING", logger="rixi"):
        pkg = Client().package(str(root))
    try:
        assert _names(pkg) == sorted(CODE)
    finally:
        os.unlink(pkg)
    assert "not shipped" in caplog.text and ".env" in caplog.text        # and it says so


def test_rixiignore_adds_and_takes_back(tmp_path):
    import os

    from rixi import Client
    root = _project(tmp_path, SECRETS + CODE + ["data/big.bin", "notes.md"])
    (root / ".rixiignore").write_text("# local only\ndata\n*.md\n!.env\n")
    pkg = Client().package(str(root))
    try:
        assert _names(pkg) == sorted(CODE + [".env", ".rixiignore"])
    finally:
        os.unlink(pkg)
    # an explicit ignore set is the caller's whole say, as before
    pkg = Client().package(str(root), ignore={".ssh", ".aws", "data"})
    try:
        assert ".env.production" in _names(pkg) and "data/big.bin" not in _names(pkg)
    finally:
        os.unlink(pkg)
