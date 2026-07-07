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
