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
import fnmatch
import hashlib
import hmac
import json
import logging
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

log = logging.getLogger("rixi")

DEFAULT_IGNORES = {
    ".git", ".pixi", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".ruff_cache", ".pytest_cache", "node_modules", ".DS_Store",
}

# Files and directories that hold credentials are not shipped unless asked for: a project
# directory often has them lying around, and a box is somebody else's machine. A `.rixiignore`
# file in the project adds patterns (one per line), and a line starting with `!` ships a path
# these would leave out (`!.env` to send it after all).
SECRET_PATTERNS = (
    ".env", ".env.*", ".envrc", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa", "id_rsa.*", "id_dsa", "id_ecdsa", "id_ecdsa.*", "id_ed25519", "id_ed25519.*",
    ".npmrc", ".pypirc", ".netrc", ".git-credentials", ".htpasswd", ".dockercfg",
    ".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker",
    "aes.key", "rotation.secret", "rixi_headers.json", "pixi_remote_config.toml",
    "credentials.json", "terraform.tfstate", "terraform.tfstate.*", "*.tfvars",
)
_NOT_SECRET = (".env.example", ".env.sample", ".env.template", ".env.dist")


def _matches(name: str, rel: str, patterns) -> bool:
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) for p in patterns)


def _rixiignore(root: Path):
    """(extra patterns to leave out, patterns to ship after all) from the project's .rixiignore."""
    out, keep = [], []
    try:
        lines = (root / ".rixiignore").read_text().splitlines()
    except OSError:
        return out, keep
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        (keep if line.startswith("!") else out).append(line.lstrip("!").strip().rstrip("/"))
    return out, keep


def cert_fingerprint(pem: str) -> str:
    """SHA-256 of a PEM certificate's DER form, in hex."""
    import ssl
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem.strip() + "\n")).hexdigest()


class _PinnedAdapter(requests.adapters.HTTPAdapter):
    """TLS to a box that has a certificate of its own making: the one certificate whose SHA-256
    fingerprint the profile holds is trusted, whatever address the box is reached at, and no
    other — not even one a certificate authority signed."""

    def __init__(self, fingerprint: str):
        self._fingerprint = fingerprint.replace(":", "").lower()
        if len(self._fingerprint) != 64 or any(c not in "0123456789abcdef"
                                               for c in self._fingerprint):
            raise ValueError("tls_fingerprint must be the SHA-256 of the certificate, in hex")
        super().__init__()

    def init_poolmanager(self, connections, maxsize, block=False, **kw):
        kw["assert_fingerprint"] = self._fingerprint
        super().init_poolmanager(connections, maxsize, block, **kw)

    def cert_verify(self, conn, url, verify, cert):
        # the fingerprint is the check (urllib3 makes it on every connection); the chain and the
        # name have nothing to say about a certificate nobody signed
        conn.cert_reqs = "CERT_NONE"
        conn.ca_certs = None
        conn.ca_cert_dir = None
        conn.assert_fingerprint = self._fingerprint

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host, pool = super().build_connection_pool_key_attributes(request, False, cert)
        pool["assert_fingerprint"] = self._fingerprint
        return host, pool


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
    tls_cert:   PEM of the one certificate to trust, for a box that was given a certificate of
                its own (``rixi up``): the connection is refused for any other certificate.
    timeout:    per-request timeout in seconds for non-streaming calls.
    """

    def __init__(self, server_url: str = "http://127.0.0.1:9000", *,
                 token: Union[str, Callable[[], str], None] = None,
                 aes_key: Optional[str] = None,
                 verify_ssl: bool = True, tls_cert: Optional[str] = None,
                 timeout: float = 30.0) -> None:
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.aes_key = base64.b64decode(aes_key) if aes_key else None
        self.verify_ssl = verify_ssl
        self.tls_cert = tls_cert
        self.timeout = timeout
        self._http = requests.Session()
        if tls_cert:
            if not self.server_url.startswith("https://"):
                raise ValueError("tls_cert needs an https:// server_url")
            self._http.mount("https://", _PinnedAdapter(cert_fingerprint(tls_cert)))

    def ssl_context(self):
        """For connections made outside this class (the terminal's WebSocket): trusts what
        this client trusts. None for plain http."""
        import ssl
        if not self.server_url.startswith("https://"):
            return None
        if self.tls_cert:
            ctx = ssl.create_default_context(cadata=self.tls_cert)   # that certificate only
            ctx.check_hostname = False              # it names no address; the key is the check
            return ctx
        ctx = ssl.create_default_context()
        if not self.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    # ── public API ─────────────────────────────────────────────────────────
    def health(self) -> dict:
        """GET /health — raises for a non-2xx response."""
        r = self._http.get(f"{self.server_url}/health", headers=self._headers(),
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

        def proof(step: bytes, *parts: bytes) -> bytes:
            mac = hmac.new(secret.encode(), step, hashlib.sha256)
            for part in parts:
                mac.update(len(part).to_bytes(4, "big") + part)
            return mac.digest()

        def b64(raw: bytes) -> str:
            return base64.b64encode(raw).decode()

        # The secret is never sent: this side proves it knows it, and so must the server —
        # over the public key it answers with, so the key cannot be swapped on the way.
        nonce = os.urandom(32)
        r = self._http.post(f"{self.server_url}/handshake",
                            json={"nonce": b64(nonce), "proof": b64(proof(b"rixi-hs1", nonce)),
                                  "rotate": rotate}, headers=self._headers(),
                            verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"handshake failed: {r.status_code} {r.text}")
        try:
            answer = r.json()
            pem = answer["public_key"].encode()
            server_nonce = base64.b64decode(answer["nonce"])
            server_proof = base64.b64decode(answer["proof"])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RixiError("handshake failed: the server did not prove it knows the secret "
                            "(an older rixi server? update it to match this client)")
        if not hmac.compare_digest(server_proof,
                                   proof(b"rixi-hs2", nonce, server_nonce, pem)):
            raise RixiError("handshake failed: the key offered is not from a server that knows "
                            "the secret — something sits between this client and the box")
        pub = serialization.load_pem_public_key(pem)
        new_key, rotation_secret = os.urandom(32), os.urandom(32)
        cipher = pub.encrypt(new_key + rotation_secret,
                             padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                          algorithm=hashes.SHA256(), label=None))
        r2 = self._http.post(f"{self.server_url}/handshake/finish",
                             json={"cipher": b64(cipher),
                                   "proof": b64(proof(b"rixi-hs3", nonce, server_nonce, cipher))},
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
        r = self._http.post(f"{self.server_url}/exec", data=data, headers=headers,
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
        with self._http.get(f"{self.server_url}/task/{task_id}/stream",
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
        r = self._http.post(f"{self.server_url}/task/{task_id}/input", params=params, data=body,
                          headers=headers, verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"input failed: {r.status_code} {_detail(r)}")
        return json.loads(self._unseal(r))

    def restart(self, task_id: str) -> dict:
        """Run a task again from the start (same code, or the same command)."""
        return self._post_json(f"/task/{task_id}/restart", "restart")

    def redeploy(self, task_id: str, project_dir: str = ".", *,
                 ignore: Optional[set] = None) -> dict:
        """Replace a running task's code with this project and start it again, same id."""
        with self._post_package(project_dir, "", True, ignore,
                                path=f"/task/{task_id}/redeploy") as r:
            return json.loads(self._unseal(r))

    def kill(self, task_id: str) -> None:
        """Stop a task and forget it (its output goes with it)."""
        r = self._http.delete(f"{self.server_url}/task/{task_id}", headers=self._headers(),
                            verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"kill failed: {r.status_code} {_detail(r)}")

    # ── preview URLs ───────────────────────────────────────────────────────
    def open_preview(self, port: int, *, public: bool = False) -> dict:
        """Serve a port of the box as ``https://<port>.<box name>/``.

        Private by default: ``share_url`` carries a token, opening it once sets a cookie for
        that host; ``public=True`` lets anyone with the URL in. Hosted (named) boxes only."""
        data, headers = self._seal_json({"port": int(port), "public": bool(public)})
        r = self._http.post(f"{self.server_url}/previews", data=data, headers=headers,
                          verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"preview failed: {r.status_code} {_detail(r)}")
        return json.loads(self._unseal(r))

    def previews(self) -> List[dict]:
        r = self._http.get(f"{self.server_url}/previews", headers=self._headers(),
                         verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"list previews failed: {r.status_code} {_detail(r)}")
        return json.loads(self._unseal(r))

    def close_preview(self, port: int) -> None:
        r = self._http.delete(f"{self.server_url}/previews/{int(port)}", headers=self._headers(),
                            verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"close preview failed: {r.status_code} {_detail(r)}")

    def _get_json(self, path: str, what: str):
        r = self._http.get(f"{self.server_url}{path}", headers=self._headers(),
                         verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"{what} failed: {r.status_code} {_detail(r)}")
        return json.loads(self._unseal(r))

    def _post_json(self, path: str, what: str):
        r = self._http.post(f"{self.server_url}{path}", headers=self._headers(),
                          verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"{what} failed: {r.status_code} {_detail(r)}")
        return json.loads(self._unseal(r))

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
        with self._http.post(f"{self.server_url}/exec", data=data, headers=headers, stream=True,
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
        r = self._http.put(f"{self.server_url}/files", params={"path": path}, data=raw,
                         headers=headers, verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"write {path} failed: {r.status_code} {_detail(r)}")
        return json.loads(self._unseal(r))

    def read_file(self, path: str) -> bytes:
        r = self._http.get(f"{self.server_url}/files", params={"path": path},
                         headers=self._headers(), verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"read {path} failed: {r.status_code} {_detail(r)}")
        return self._unseal(r)

    def list_files(self, path: str = ".") -> List[dict]:
        r = self._http.get(f"{self.server_url}/files/list", params={"path": path},
                         headers=self._headers(), verify=self.verify_ssl, timeout=self.timeout)
        if r.status_code != 200:
            raise RixiError(f"list {path} failed: {r.status_code} {_detail(r)}")
        return json.loads(self._unseal(r))["entries"]

    def delete_file(self, path: str) -> None:
        r = self._http.delete(f"{self.server_url}/files", params={"path": path},
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
            try:
                return decrypt(self.aes_key, r.content)
            except Exception:
                raise RixiError("the answer was not sealed with this client's key")
        if self.aes_key:
            # with a key every answer is sealed: one in the clear did not come from the server
            # that holds the key (or the server is older than this client)
            raise RixiError("the server answered in the clear although this client has a key")
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
        # An explicit `ignore` is the caller's whole say; otherwise the defaults, what looks
        # like a credential, and the project's .rixiignore.
        skip = DEFAULT_IGNORES if ignore is None else ignore
        extra, keep = _rixiignore(root) if ignore is None else ([], [])
        secrets_out = SECRET_PATTERNS if ignore is None else ()
        held_back: List[str] = []

        def left_out(name: str, rel: str) -> bool:
            if name in skip or _matches(name, rel, extra):
                return True
            if _matches(name, rel, secrets_out) and name not in _NOT_SECRET \
                    and not _matches(name, rel, keep):
                held_back.append(rel)
                return True
            return False

        tar_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar")
        tar_tmp.close()
        try:
            with tarfile.open(tar_tmp.name, "w") as tar:
                for dirpath, dirs, files in os.walk(root):
                    here = Path(dirpath).relative_to(root)
                    dirs[:] = [d for d in dirs if not left_out(d, (here / d).as_posix())]
                    for name in files:
                        fp = Path(dirpath) / name
                        arcname = str(fp.relative_to(root))
                        if left_out(name, fp.relative_to(root).as_posix()):
                            continue
                        tar.add(fp, arcname)
            if held_back:
                shown = ", ".join(sorted(held_back)[:8]) + (" …" if len(held_back) > 8 else "")
                log.warning("not shipped (they look like credentials): %s — add a line like "
                            "`!.env` to .rixiignore to send one", shown)
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
                with self._http.post(f"{self.server_url}{path}", files=files, data=data,
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
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if self.aes_key:
            headers["X-Rixi-Accept-Sealed"] = "1"      # plain JSON answers come sealed as well
        return headers


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
