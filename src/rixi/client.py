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

from .crypto import decrypt, encrypt, frame, iter_frames

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


@dataclass
class ExecResult:
    """Result of one command run with ``Client.exec``."""
    exit_code: Optional[int] = None
    status: str = ""                       # completed | failed | timeout
    stdout: str = ""
    stderr: str = ""
    duration_s: Optional[float] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.error is None


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

    # ── long-running work: tasks you start, leave and come back to ─────────
    def start(self, project_dir: str = ".", *, task: str = "default",
              ignore: Optional[set] = None) -> str:
        """Ship a project and start ``task`` in the background; returns its task id at once.

        The task keeps running after this returns. Follow it with ``attach``, feed it with
        ``send_input``, and ``restart``/``redeploy``/``kill`` it by id — from any process.
        """
        with self._post_package(project_dir, task, True, ignore,
                                extra={"detach": "true"}) as r:
            return json.loads(self._unseal(r))["task_id"]

    def spawn(self, cmd: Union[str, List[str]], *, cwd: Optional[str] = None,
              env: Optional[Dict[str, str]] = None, timeout: Optional[float] = None,
              stdin: Optional[str] = None) -> str:
        """Start a command in the workspace in the background; returns its task id.

        Like ``exec``, but the command outlives the call: a dev server, a watcher, a long job.
        Its output is kept, so ``attach`` can replay and follow it later.
        """
        body = {"cmd": cmd, "background": True}
        for k, v in (("cwd", cwd), ("env", env), ("timeout", timeout), ("stdin", stdin)):
            if v is not None:
                body[k] = v
        data, headers = self._seal_json(body)
        r = requests.post(f"{self.server_url}/exec", data=data, headers=headers,
                          verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"spawn failed: {r.status_code} {_detail(r)}")
        return json.loads(self._unseal(r))["task_id"]

    def tasks(self) -> Dict[str, dict]:
        """Every task on the box — running, or finished and kept — by id."""
        return self._get_json("/tasks", "list tasks")

    def task(self, task_id: str) -> dict:
        """One task: status, exit code, recent output."""
        return self._get_json(f"/task/{task_id}", f"task {task_id}")

    def output(self, task_id: str) -> str:
        """Everything a task has printed so far (kept output, no following)."""
        lines = self._get_json(f"/task/{task_id}/output", f"output of {task_id}")
        return "".join(e["content"] + "\n" for e in lines.get("output_history", []))

    def attach(self, task_id: str, *, history: bool = True) -> Iterator[tuple]:
        """Follow a task's output until it ends: yields ``("stdout", text)`` /
        ``("stderr", text)``, then ``("exit", {...})``. ``history=False`` skips what it printed
        before you attached. Stop iterating to detach; the task keeps running."""
        with requests.get(f"{self.server_url}/task/{task_id}/stream",
                          params={"history": str(history).lower()}, headers=self._headers(),
                          stream=True, verify=self.verify_ssl, timeout=None) as r:
            if r.status_code != 200:
                raise RixiError(f"attach failed: {r.status_code} {_detail(r)}")
            for payload in iter_frames(self.aes_key, r.iter_content(chunk_size=4096)):
                for obj in _objects(payload):
                    if "output" in obj:
                        yield "stdout", obj["output"]
                    elif "stderr" in obj:
                        yield "stderr", obj["stderr"]
                    elif "exit_code" in obj:
                        yield "exit", obj

    def send_input(self, task_id: str, data: Union[str, dict], *, raw: bool = True) -> dict:
        """Write to a running task's stdin. A string goes as-is (``raw``) — add the newline a
        line-reading program expects; a dict goes as one JSON line (the task protocol)."""
        params = {"raw": "1"} if raw and isinstance(data, str) else {}
        body = data.encode() if isinstance(data, str) else json.dumps(data).encode()
        headers = self._headers()
        if self.aes_key:
            body = encrypt(self.aes_key, body)
            headers["Content-Type"] = "application/octet-stream"
        else:
            headers["Content-Type"] = "text/plain" if params else "application/json"
        r = requests.post(f"{self.server_url}/task/{task_id}/input", params=params, data=body,
                          headers=headers, verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"input failed: {r.status_code} {_detail(r)}")
        return r.json()

    def restart(self, task_id: str) -> dict:
        """Run a task again from the start (same code, or the same command)."""
        return self._post_json(f"/task/{task_id}/restart", "restart")

    def redeploy(self, task_id: str, project_dir: str = ".", *,
                 ignore: Optional[set] = None) -> dict:
        """Replace a running task's code with this project and start it again, same id."""
        with self._post_package(project_dir, "", True, ignore,
                                path=f"/task/{task_id}/redeploy") as r:
            return r.json()

    def kill(self, task_id: str) -> None:
        """Stop a task and forget it (its output goes with it)."""
        r = requests.delete(f"{self.server_url}/task/{task_id}", headers=self._headers(),
                            verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"kill failed: {r.status_code} {_detail(r)}")

    # ── preview URLs ───────────────────────────────────────────────────────
    def open_preview(self, port: int, *, public: bool = False) -> dict:
        """Serve a port of the box as ``https://<port>.<box name>/``.

        Private by default: ``share_url`` carries a token, opening it once sets a cookie for
        that host; ``public=True`` lets anyone with the URL in. Hosted (named) boxes only."""
        data, headers = self._seal_json({"port": int(port), "public": bool(public)})
        r = requests.post(f"{self.server_url}/previews", data=data, headers=headers,
                          verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"preview failed: {r.status_code} {_detail(r)}")
        return json.loads(self._unseal(r))

    def previews(self) -> List[dict]:
        r = requests.get(f"{self.server_url}/previews", headers=self._headers(),
                         verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"list previews failed: {r.status_code} {_detail(r)}")
        return json.loads(self._unseal(r))

    def close_preview(self, port: int) -> None:
        r = requests.delete(f"{self.server_url}/previews/{int(port)}", headers=self._headers(),
                            verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"close preview failed: {r.status_code} {_detail(r)}")

    def _get_json(self, path: str, what: str):
        r = requests.get(f"{self.server_url}{path}", headers=self._headers(),
                         verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"{what} failed: {r.status_code} {_detail(r)}")
        return r.json()

    def _post_json(self, path: str, what: str):
        r = requests.post(f"{self.server_url}{path}", headers=self._headers(),
                          verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"{what} failed: {r.status_code} {_detail(r)}")
        return r.json()

    # ── sandboxed environments: commands and files ─────────────────────────
    def exec(self, cmd: Union[str, List[str]], *, cwd: Optional[str] = None,
             env: Optional[Dict[str, str]] = None, timeout: Optional[float] = None,
             stdin: Optional[str] = None) -> ExecResult:
        """Run one command in the box's workspace and wait for it.

        A string runs through ``bash -lc``; a list is executed as-is (no shell). Returns the
        exit code with stdout and stderr collected separately.
        """
        result = ExecResult()
        for kind, value in self.exec_stream(cmd, cwd=cwd, env=env, timeout=timeout,
                                            stdin=stdin):
            if kind == "stdout":
                result.stdout += value
            elif kind == "stderr":
                result.stderr += value
            else:
                result.exit_code = value.get("exit_code")
                result.status = value.get("status", "")
                result.duration_s = value.get("duration_s")
                result.error = value.get("error")
        return result

    def exec_stream(self, cmd: Union[str, List[str]], *, cwd: Optional[str] = None,
                    env: Optional[Dict[str, str]] = None, timeout: Optional[float] = None,
                    stdin: Optional[str] = None) -> Iterator[tuple]:
        """Run a command and yield ``("stdout", text)`` / ``("stderr", text)`` as output
        arrives, then ``("exit", {...})`` with the exit code and status."""
        body = {"cmd": cmd}
        if cwd is not None:
            body["cwd"] = cwd
        if env:
            body["env"] = env
        if timeout is not None:
            body["timeout"] = timeout
        if stdin is not None:
            body["stdin"] = stdin
        data, headers = self._seal_json(body)
        with requests.post(f"{self.server_url}/exec", data=data, headers=headers, stream=True,
                           verify=self.verify_ssl, timeout=None) as r:
            if r.status_code != 200:
                raise RixiError(f"exec failed: {r.status_code} {_detail(r)}")
            for payload in iter_frames(self.aes_key, r.iter_content(chunk_size=4096)):
                for obj in _objects(payload):
                    if "stdout" in obj:
                        yield "stdout", obj["stdout"]
                    elif "stderr" in obj:
                        yield "stderr", obj["stderr"]
                    elif "exit_code" in obj:
                        yield "exit", obj

    def write_file(self, path: str, data: Union[bytes, str]) -> dict:
        """Write a file in the workspace (parent directories are created)."""
        raw = data.encode() if isinstance(data, str) else data
        headers = self._headers()
        if self.aes_key:
            raw, headers["X-Rixi-Encrypted"] = encrypt(self.aes_key, raw), "1"
        headers["Content-Type"] = "application/octet-stream"
        r = requests.put(f"{self.server_url}/files", params={"path": path}, data=raw,
                         headers=headers, verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"write {path} failed: {r.status_code} {_detail(r)}")
        return r.json()

    def read_file(self, path: str) -> bytes:
        r = requests.get(f"{self.server_url}/files", params={"path": path},
                         headers=self._headers(), verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"read {path} failed: {r.status_code} {_detail(r)}")
        return self._unseal(r)

    def list_files(self, path: str = ".") -> List[dict]:
        r = requests.get(f"{self.server_url}/files/list", params={"path": path},
                         headers=self._headers(), verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"list {path} failed: {r.status_code} {_detail(r)}")
        return json.loads(self._unseal(r))["entries"]

    def delete_file(self, path: str) -> None:
        r = requests.delete(f"{self.server_url}/files", params={"path": path},
                            headers=self._headers(), verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"delete {path} failed: {r.status_code} {_detail(r)}")

    def _seal_json(self, body: dict):
        raw = json.dumps(body).encode()
        headers = self._headers()
        if self.aes_key:
            headers.update({"X-Rixi-Encrypted": "1", "Content-Type": "application/octet-stream"})
            return encrypt(self.aes_key, raw), headers
        headers["Content-Type"] = "application/json"
        return raw, headers

    def _unseal(self, r: requests.Response) -> bytes:
        if r.headers.get("X-Rixi-Encrypted") == "1":
            if not self.aes_key:
                raise RixiError("the server sealed its answer but this client has no key")
            return decrypt(self.aes_key, r.content)
        return r.content

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
                      ignore: Optional[set], *, path: str = "/upload",
                      extra: Optional[Dict[str, str]] = None) -> Iterator[requests.Response]:
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
                data = {"task_name": task, "keep_alive": str(keep_alive).lower(),
                        **(extra or {})}
                with requests.post(f"{self.server_url}{path}", files=files, data=data,
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


def _detail(r: requests.Response) -> str:
    try:
        body = r.json()
        return body.get("detail") or body.get("error") or r.text
    except ValueError:
        return r.text[:300]


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
