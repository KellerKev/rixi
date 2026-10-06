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
32-byte key) to match a server started with ``--aes-key``, or call ``handshake(secret)`` to
negotiate one with a server started with ``--key-secret``. With a key set, the uploaded package
and the streamed output are both AES-256-GCM sealed; without one the channel is plain HTTP (use
it over loopback, an SSH tunnel, the rixi tunnel, or behind TLS).
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Union

import requests

from .crypto import frame, iter_frames

# Packages are sealed in chunks of this size, each as one length-prefixed AES-GCM frame.
_SEAL_CHUNK = 1024 * 1024

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
    token:      optional JWT bearer token (required when the server has auth enabled), or a
                zero-argument callable returning one — called per request, so short-lived
                tokens can be minted fresh each time.
    aes_key:    optional base64 of a 32-byte AES key matching the server's ``--aes-key``.
    verify_ssl: verify TLS certs (default True); set False only for self-signed dev certs.
    timeout:    per-request timeout in seconds for non-streaming calls.
    """

    def __init__(self, server_url: str = "http://127.0.0.1:9000", *,
                 token: Union[str, Callable[[], str], None] = None,
                 aes_key: Optional[str] = None,
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

    def handshake(self, secret: str, *, rotate: bool = False) -> str:
        """Negotiate a fresh AES-256 key with a server started with ``--key-secret``.

        The server returns an ephemeral RSA public key; the client generates the AES key and
        sends it back RSA-OAEP-wrapped, so the key never crosses the wire in the clear. Sets
        this client's key and returns it base64-encoded for the caller to store.
        """
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        r = requests.post(f"{self.server_url}/handshake",
                          json={"secret": secret, "rotate": rotate}, headers=self._headers(),
                          verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"handshake failed: {r.status_code} {r.text}")
        pub = serialization.load_pem_public_key(r.json()["public_key"].encode())
        new_key, rotation_secret = os.urandom(32), os.urandom(32)
        cipher = pub.encrypt(new_key + rotation_secret,
                             padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                          algorithm=hashes.SHA256(), label=None))
        r2 = requests.post(f"{self.server_url}/handshake/finish",
                           json={"cipher": base64.b64encode(cipher).decode()},
                           headers=self._headers(), verify=self.verify_ssl,
                           timeout=self.timeout)
        if r2.status_code != 200:
            raise RixiError(f"handshake failed: {r2.status_code} {r2.text}")
        self.aes_key = new_key
        return base64.b64encode(new_key).decode()

    def stream(self, project_dir: str = ".", *, task: str = "default",
               keep_alive: bool = False,
               ignore: Optional[set] = None) -> Iterator[str]:
        """Package ``project_dir``, run ``task``, and yield output text as it streams."""
        with self._post_package(project_dir, task, keep_alive, ignore) as r:
            for payload in iter_frames(self.aes_key, r.iter_content(chunk_size=4096)):
                yield from _texts(payload)

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
        with self._post_package(project_dir, task, keep_alive, ignore) as r:
            for payload in iter_frames(self.aes_key, r.iter_content(chunk_size=4096)):
                yield from _objects(payload)

    @contextlib.contextmanager
    def _post_package(self, project_dir: str, task: str, keep_alive: bool,
                      ignore: Optional[set]) -> Iterator[requests.Response]:
        """Package, upload, and yield the streaming response; temp files are always removed.

        With an AES key the package is sealed (one AES-GCM frame per 1 MiB chunk) and marked
        ``X-Rixi-Encrypted: 1`` so the server decrypts it; the code never crosses the wire in
        the clear.
        """
        pkg = self.package(project_dir, ignore=ignore)
        paths = [pkg]
        try:
            headers = self._headers()
            if self.aes_key:
                sealed = pkg + ".sealed"
                paths.append(sealed)
                _seal_file(self.aes_key, pkg, sealed)
                pkg = sealed
                headers["X-Rixi-Encrypted"] = "1"
            with open(pkg, "rb") as fh:
                files = {"file": (os.path.basename(pkg), fh, "application/octet-stream")}
                data = {"task_name": task, "keep_alive": str(keep_alive).lower()}
                with requests.post(f"{self.server_url}/upload", files=files, data=data,
                                   headers=headers, stream=True,
                                   verify=self.verify_ssl, timeout=None) as r:
                    if r.status_code != 200:
                        raise RixiError(f"upload failed: {r.status_code} {r.text}")
                    yield r
        finally:
            for path in paths:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _headers(self) -> Dict[str, str]:
        token = self.token() if callable(self.token) else self.token
        return {"Authorization": f"Bearer {token}"} if token else {}


class RixiError(RuntimeError):
    """Raised for client-side and server-side rixi errors."""


def _seal_file(key: bytes, src: str, dst: str) -> None:
    """Write `src` to `dst` as consecutive length-prefixed AES-GCM frames (server: _receive_package)."""
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            chunk = fin.read(_SEAL_CHUNK)
            if not chunk:
                break
            fout.write(frame(key, chunk))


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
