"""Importable rixi client SDK.

Package a local project directory, ship it to a rixi server, run a task, and stream the
output back — from Python, no CLI or shell alias required:

    from rixi import Client

    rixi = Client("http://127.0.0.1:9000", token="…")     # token optional on loopback
    result = rixi.run(".", task="train")                    # blocks, returns RunResult
    print(result.output)

    for line in rixi.stream(".", task="train"):             # or stream incrementally
        print(line, end="")

The SDK speaks the same wire format as ``clients/rixi_client.py`` (tar → LZ4 → multipart
POST /upload, length-prefixed AES-GCM frames back) but exposes it as a library so it works
in notebooks and other Python code. Transport encryption: pass ``aes_key`` (base64 of a
32-byte key) to match a server started with ``--aes-key``; otherwise the channel is plain
HTTP (use it over loopback, an SSH tunnel, the rixi tunnel, or behind TLS).
"""
from __future__ import annotations

import base64
import json
import os
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import requests

from .crypto import iter_frames

DEFAULT_IGNORES = {
    ".git", ".pixi", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".ruff_cache", ".pytest_cache", "node_modules", ".DS_Store",
}


@dataclass
class RunResult:
    """Collected result of a completed run."""
    task_id: Optional[str] = None
    output: str = ""
    statuses: List[str] = field(default_factory=list)
    error: Optional[str] = None
    exit_code: Optional[int] = None        # the task process's exit code (0 = success)

    @property
    def ok(self) -> bool:
        return self.error is None and self.exit_code in (None, 0)


class Client:
    """A rixi server client.

    Parameters
    ----------
    server_url: base URL of the rixi server (e.g. ``http://127.0.0.1:9000``).
    token:      optional JWT bearer token (required when the server has auth enabled).
    aes_key:    optional base64 of a 32-byte AES key matching the server's ``--aes-key``.
    verify_ssl: verify TLS certs (default True); set False only for self-signed dev certs.
    timeout:    per-request timeout in seconds for non-streaming calls.
    """

    def __init__(self, server_url: str = "http://127.0.0.1:9000", *,
                 token: Optional[str] = None, aes_key: Optional[str] = None,
                 verify_ssl: bool = True, timeout: float = 30.0) -> None:
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.aes_key = base64.b64decode(aes_key) if aes_key else None
        self.verify_ssl = verify_ssl
        self.timeout = timeout

    # ── public API ─────────────────────────────────────────────────────────
    def health(self) -> dict:
        """GET /health — raises for a non-2xx response."""
        r = requests.get(f"{self.server_url}/health", headers=self._headers(),
                         verify=self.verify_ssl, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def stream(self, project_dir: str = ".", *, task: str = "default",
               keep_alive: bool = False,
               ignore: Optional[set] = None) -> Iterator[str]:
        """Package ``project_dir``, run ``task``, and yield output text as it streams."""
        pkg = self.package(project_dir, ignore=ignore)
        try:
            with open(pkg, "rb") as fh:
                files = {"file": (os.path.basename(pkg), fh, "application/octet-stream")}
                data = {"task_name": task, "keep_alive": str(keep_alive).lower()}
                with requests.post(f"{self.server_url}/upload", files=files, data=data,
                                   headers=self._headers(), stream=True,
                                   verify=self.verify_ssl, timeout=None) as r:
                    if r.status_code != 200:
                        raise RixiError(f"upload failed: {r.status_code} {r.text}")
                    for payload in iter_frames(self.aes_key, r.iter_content(chunk_size=4096)):
                        yield from _texts(payload)
        finally:
            try:
                os.unlink(pkg)
            except OSError:
                pass

    def run(self, project_dir: str = ".", *, task: str = "default",
            keep_alive: bool = False, ignore: Optional[set] = None) -> RunResult:
        """Package + run ``task`` and block until it completes, returning a RunResult."""
        result = RunResult()
        for obj in self._stream_objs(project_dir, task=task, keep_alive=keep_alive,
                                     ignore=ignore):
            tid = obj.get("task_id")
            if tid:
                result.task_id = tid
            if "status" in obj:
                result.statuses.append(obj["status"])
            if "output" in obj:
                result.output += obj["output"]
            if "stderr" in obj:
                result.output += obj["stderr"]
            if "error" in obj:
                result.error = obj["error"]
            if "exit_code" in obj:
                result.exit_code = obj["exit_code"]
        return result

    def package(self, project_dir: str = ".", *,
                ignore: Optional[set] = None) -> str:
        """Tar + LZ4 a project directory into a temp file; returns its path.

        The caller owns the returned file (``run``/``stream`` delete it automatically).
        """
        try:
            import lz4.frame
        except ImportError as exc:  # pragma: no cover
            raise RixiError("the 'lz4' package is required to package projects "
                            "(pip install lz4)") from exc

        root = Path(project_dir)
        if not root.is_dir():
            raise RixiError(f"not a directory: {project_dir}")
        skip = DEFAULT_IGNORES if ignore is None else ignore

        tar_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar")
        tar_tmp.close()
        try:
            with tarfile.open(tar_tmp.name, "w") as tar:
                for dirpath, dirs, files in os.walk(root):
                    dirs[:] = [d for d in dirs if d not in skip]
                    for name in files:
                        fp = Path(dirpath) / name
                        arcname = str(fp.relative_to(root))
                        tar.add(fp, arcname)
            lz4_path = tar_tmp.name + ".lz4"
            with open(tar_tmp.name, "rb") as src, lz4.frame.open(lz4_path, "wb") as dst:
                dst.write(src.read())
            return lz4_path
        finally:
            try:
                os.unlink(tar_tmp.name)
            except OSError:
                pass

    # ── internals ──────────────────────────────────────────────────────────
    def _stream_objs(self, project_dir: str, *, task: str, keep_alive: bool,
                     ignore: Optional[set]) -> Iterator[dict]:
        pkg = self.package(project_dir, ignore=ignore)
        try:
            with open(pkg, "rb") as fh:
                files = {"file": (os.path.basename(pkg), fh, "application/octet-stream")}
                data = {"task_name": task, "keep_alive": str(keep_alive).lower()}
                with requests.post(f"{self.server_url}/upload", files=files, data=data,
                                   headers=self._headers(), stream=True,
                                   verify=self.verify_ssl, timeout=None) as r:
                    if r.status_code != 200:
                        raise RixiError(f"upload failed: {r.status_code} {r.text}")
                    for payload in iter_frames(self.aes_key, r.iter_content(chunk_size=4096)):
                        yield from _objects(payload)
        finally:
            try:
                os.unlink(pkg)
            except OSError:
                pass

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}


class RixiError(RuntimeError):
    """Raised for client-side and server-side rixi errors."""


_decoder = json.JSONDecoder()


def _objects(payload: bytes) -> Iterator[dict]:
    """Peel concatenated JSON objects out of one decrypted payload."""
    text = payload.decode("utf-8", "ignore").lstrip()
    while text:
        try:
            obj, idx = _decoder.raw_decode(text)
        except json.JSONDecodeError:
            break
        yield obj
        text = text[idx:].lstrip()


def _texts(payload: bytes) -> Iterator[str]:
    for obj in _objects(payload):
        if "output" in obj:
            yield obj["output"]
        if "stderr" in obj:
            yield obj["stderr"]
        if "error" in obj:
            yield f"\nError: {obj['error']}\n"
