#!/usr/bin/env python3
"""Shared transport/encryption logic for the pixi Remote Runner clients."""

from __future__ import annotations
import base64, json, os, re, sys
from pathlib import Path
from typing import Dict, List, Optional

import requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_LEN = 12

# HTTP defaults: (connect timeout, read timeout); streaming reads may block indefinitely
CONNECT_TIMEOUT = 10
DEFAULT_TIMEOUT = (CONNECT_TIMEOUT, 30)
STREAM_TIMEOUT = (CONNECT_TIMEOUT, None)

_http_session = requests.Session()

# JSON peeler used by _push_buffer
_decoder = json.JSONDecoder()


def _http(method: str, url: str, *, timeout=DEFAULT_TIMEOUT, **kwargs):
    """Issue an HTTP request through the shared session with a default timeout."""
    return _http_session.request(method, url, timeout=timeout, **kwargs)


def _write_secret_file(path: str, data: str):
    """Write a secret file with owner-only permissions (0600)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)


def _hdrs(bearer: str | None, snowflake: str | None) -> Dict[str, str]:
    """Create authentication headers."""
    h: Dict[str, str] = {}
    if snowflake: h["Authorization"] = f'Snowflake Token="{snowflake}"'
    elif bearer:  h["Authorization"] = f"Bearer {bearer}"
    return h


# ───────────────────── Custom request headers ─────────────────────────────
# Arbitrary HTTP headers that the client sends to the server on every request.
# Populated from (low→high precedence) a [config.headers] table, a JSON template
# file, and repeatable --header CLI flags. Values support ${env:VAR} / ${file:path}
# expansion so external programs can drop in secrets via the environment or a file.

DEFAULT_HEADERS_FILE = "rixi_headers.json"


def resolve_placeholders(value: str) -> str:
    """Expand ${env:NAME} and ${file:path} placeholders in a header value."""
    if not isinstance(value, str):
        return value

    def _env(m):
        return os.getenv(m.group(1), "")

    def _file(m):
        try:
            return Path(m.group(1)).read_text().strip()
        except Exception:
            return ""

    value = re.sub(r"\$\{env:([^}]+)\}", _env, value)
    value = re.sub(r"\$\{file:([^}]+)\}", _file, value)
    return value


def _parse_cli_header(item: str) -> Optional[tuple]:
    """Parse a curl-style 'Key: Value' string into (key, value)."""
    if not item or ":" not in item:
        return None
    key, val = item.split(":", 1)
    key = key.strip()
    return (key, val.strip()) if key else None


def build_custom_headers(cli_headers: Optional[List[str]],
                         headers_file: Optional[str],
                         config_headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Merge custom headers from config < file < CLI, resolving ${env:}/${file:} values.

    Headers whose value resolves to empty are dropped. Returns {} when nothing is set,
    so callers can apply it unconditionally without changing default behavior.
    """
    merged: Dict[str, str] = {}

    if config_headers:
        for k, v in config_headers.items():
            if str(k).strip():
                merged[str(k).strip()] = str(v)

    path = headers_file or (DEFAULT_HEADERS_FILE if os.path.exists(DEFAULT_HEADERS_FILE) else None)
    if path:
        try:
            with open(path) as f:
                data = json.load(f)
            for k, v in data.items():
                k = str(k).strip()
                # allow comment keys like "//" or "_comment" in the template
                if k and not k.startswith(("//", "_")):
                    merged[k] = str(v)
        except FileNotFoundError:
            if headers_file:  # explicit path that doesn't exist is worth flagging
                print(f"⚠️ headers file not found: {path}")
        except Exception as e:
            print(f"⚠️ Could not load headers file {path}: {e}")

    for item in (cli_headers or []):
        parsed = _parse_cli_header(item)
        if parsed:
            merged[parsed[0]] = parsed[1]
        else:
            print(f"⚠️ Ignoring malformed --header (expected 'Key: Value'): {item!r}")

    resolved: Dict[str, str] = {}
    for k, v in merged.items():
        rv = resolve_placeholders(v)
        if rv != "":
            resolved[k] = rv
    return resolved


def set_default_headers(headers: Optional[Dict[str, str]]):
    """Apply default headers to every request made through the shared session.

    requests merges Session.headers into each request, with per-request headers
    overriding — so existing Authorization/Content-Type headers still take precedence.
    """
    if headers:
        _http_session.headers.update(headers)


def mask_header_value(key: str, value: str) -> str:
    """Mask values for sensitive-looking header keys (for --show-headers previews)."""
    if re.search(r"authorization|token|secret|key|cookie", key, re.IGNORECASE):
        return "***"
    return value


class Transport:
    """Holds connection state and the shared transport/encryption logic."""

    def __init__(self, server_url: str = "http://localhost:9000",
                 aes_key: Optional[bytes] = None,
                 auth_headers: Optional[Dict[str, str]] = None,
                 task_id: Optional[str] = None):
        self.server_url = server_url
        self.aes_key = aes_key
        self.auth_headers = auth_headers if auth_headers is not None else {}
        self.task_id = task_id

    # ───────────────────────── AES helpers ────────────────────────────────
    def _dec(self, chunk: bytes) -> str:
        if self.aes_key is None:
            return chunk.decode("utf-8", "ignore")
        return AESGCM(self.aes_key).decrypt(chunk[:NONCE_LEN], chunk[NONCE_LEN:], None).decode(
            "utf-8", "ignore"
        )

    # ───────────────────────── JSON / line helpers ────────────────────────
    def _handle_obj(self, obj: dict):
        if obj.get("task_id") and obj["task_id"] != self.task_id:
            self.task_id = obj["task_id"]
            print(f"\nTask ID: {self.task_id}\n")
        if "status" in obj:  print("Status:", obj["status"])
        if "output" in obj:  print(obj["output"], end="")
        if "stderr" in obj:  print(obj["stderr"], end="")
        if "error"  in obj:  print("Error:", obj["error"])

    def _push_buffer(self, text: str, on_obj=None):
        """Pull one JSON object from the beginning of text and handle it."""
        text = text.lstrip()
        if not text:
            return ""
        try:
            obj, idx = _decoder.raw_decode(text)
        except json.JSONDecodeError:
            return text            # need more bytes
        (on_obj or self._handle_obj)(obj)
        return text[idx:]          # remainder (may be empty)

    # ───────────────────────── Stream consumer ────────────────────────────
    def _consume_stream(self, resp, on_obj=None):
        """Parse encrypted binary frames containing JSON."""
        buf = b""
        need = None         # bytes still required for current frame

        for chunk in resp.iter_content(chunk_size=4096):
            buf += chunk
            while True:
                if need is None:
                    if len(buf) < 4:
                        break
                    need = int.from_bytes(buf[:4], "big")
                    buf = buf[4:]
                if len(buf) < need:
                    break

                enc, buf = buf[:need], buf[need:]
                need = None
                try:
                    plain = self._dec(enc)
                except Exception as exc:
                    print("Decrypt error:", exc)
                    continue

                # push through JSON peeler
                plain_buf = plain
                while plain_buf:
                    plain_buf = self._push_buffer(plain_buf, on_obj)

    # ───────────────────────── Upload / attach wrappers ───────────────────
    def _upload_and_run(self, pkg, task, headers, on_obj=None):
        try:
            with open(pkg, "rb") as fh:
                files = {"file": (os.path.basename(pkg), fh, "application/octet-stream")}
                data  = {"task_name": task, "keep_alive": "true"}
                with _http("POST", f"{self.server_url}/upload", files=files, data=data, headers=headers,
                           stream=True, timeout=STREAM_TIMEOUT) as r:
                    if r.status_code != 200:
                        print("Error:", r.status_code, r.text)
                        return
                    self._consume_stream(r, on_obj)
        finally:
            os.unlink(pkg)

    def _attach_stream_only(self, tid: str, headers):
        """Attach to running task - live stream only (no duplicate recent_output)."""
        with _http(
            "GET", f"{self.server_url}/task/{tid}/stream", headers=headers, stream=True, timeout=STREAM_TIMEOUT
        ) as resp:
            if resp.status_code != 200:
                print("Error:", resp.status_code, resp.text)
                return
            self._consume_stream(resp)

    def _attach(self, tid: str, headers):
        """Attach to running task - stream only to avoid duplicated backlog output."""
        self._attach_stream_only(tid, headers)

    def _attach_history(self, tid: str, headers):
        """Attach with full history - print backlog once, then live-stream."""
        # ➊ backlog from recent_output (formatted properly)
        r = _http("GET", f"{self.server_url}/task/{tid}", headers=headers)
        if r.status_code == 200:
            info = r.json()
            for entry in info.get("recent_output", []):
                if entry["type"] in {"output", "stderr"}:
                    print(entry["content"], end="")
                elif entry["type"] == "error":
                    print("Error:", entry["content"])
                elif entry["type"] == "status":
                    print("Status:", entry["content"])

        # ➋ live follow (stream only, no duplicates)
        self._attach_stream_only(tid, headers)

    # ───────────────────────── Handshake ──────────────────────────────────
    def perform_handshake(self, secret: str, rotate: bool):
        data = {"secret": secret, "rotate": rotate}
        r = _http("POST", f"{self.server_url}/handshake", json=data, headers=self.auth_headers)
        if r.status_code != 200:
            # Better error handling for handshake limits
            if r.status_code == 403:
                error_msg = r.json().get("error", r.text) if r.text else "Forbidden"
                if "limit" in error_msg.lower():
                    print(f"❌ Handshake failed: {error_msg}")
                    print("💡 Try again later or use existing aes.key file")
                    sys.exit(1)
            print("Handshake step-1 failed:", r.text)
            sys.exit(1)
        pub_pem = r.json()["public_key"]

        # generate AES key + new rotation secret
        new_aes = os.urandom(32)
        new_rot = os.urandom(32)

        pub_key = serialization.load_pem_public_key(pub_pem.encode())
        blob = new_aes + new_rot
        cipher = base64.b64encode(
            pub_key.encrypt(
                blob,
                padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                             algorithm=hashes.SHA256(), label=None)
            )
        ).decode()

        r2 = _http("POST", f"{self.server_url}/handshake/finish", json={"cipher": cipher},
                   headers=self.auth_headers)
        if r2.status_code != 200:
            print("Handshake step-2 failed:", r2.text)
            sys.exit(1)

        # persist to local file (owner-only permissions)
        _write_secret_file("aes.key", base64.b64encode(new_aes).decode())
        _write_secret_file("rotation.secret", base64.b64encode(new_rot).decode())
        self.aes_key = new_aes
        print("Handshake successful – AES key saved (base64)")
        return new_aes, new_rot
