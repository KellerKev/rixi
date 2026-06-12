#!/usr/bin/env python3
"""pixi Remote Runner Client — AES-aware & Ctrl-C menu - FIXED duplicate output"""

from __future__ import annotations
import argparse, base64, json, os, pathlib, signal, sys, tarfile, tempfile
from typing import Dict, Optional
import shutil
import lz4.frame, requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ───────────────────────── Globals ────────────────────────────────────────
NONCE_LEN = 12
current_task_id: Optional[str] = None
server_url: str = "http://localhost:9000"
aes_key: Optional[bytes] = None
_auth_headers: Dict[str, str] = {}          # set in main

# HTTP defaults: (connect timeout, read timeout); streaming reads may block indefinitely
CONNECT_TIMEOUT = 10
DEFAULT_TIMEOUT = (CONNECT_TIMEOUT, 30)
STREAM_TIMEOUT = (CONNECT_TIMEOUT, None)

_http_session = requests.Session()

def _http(method: str, url: str, *, timeout=DEFAULT_TIMEOUT, **kwargs):
    """Issue an HTTP request through the shared session with a default timeout."""
    return _http_session.request(method, url, timeout=timeout, **kwargs)

# ───────────────────────── AES helpers ────────────────────────────────────
def _dec(chunk: bytes) -> str:
    if aes_key is None:
        return chunk.decode("utf-8", "ignore")
    return AESGCM(aes_key).decrypt(chunk[:NONCE_LEN], chunk[NONCE_LEN:], None).decode(
        "utf-8", "ignore"
    )

# ───────────────────────── JSON / line helper ────────────────────────────
_decoder = json.JSONDecoder()

def _push_buffer(text: str):
    """
    Pull one JSON object from the beginning of *text* (if present),
    pass it to _handle_obj(), and return the remaining string.
    """
    text = text.lstrip()
    if not text:
        return ""
    try:
        obj, idx = _decoder.raw_decode(text)
    except json.JSONDecodeError:
        return text            # need more bytes
    _handle_obj(obj)
    return text[idx:]          # remainder (may be empty)

def _handle_obj(obj: dict):
    """Existing logic, extracted from old _handle_json_line."""
    global current_task_id
    if obj.get("task_id") and obj["task_id"] != current_task_id:
        current_task_id = obj["task_id"]
        print(f"\nTask ID: {current_task_id}\n")
    if "status" in obj:  print("Status:", obj["status"])
    if "output" in obj:  print(obj["output"], end="")
    if "stderr" in obj:  print(obj["stderr"], end="")
    if "error"  in obj:  print("Error:", obj["error"])

# ───────────────────────── Ctrl-C menu ────────────────────────────────────
def _signal_handler(sig, frame):
    global current_task_id
    if current_task_id is None:
        print("\nExiting…"); sys.exit(0)

    print("\nInterrupted! Choose:")
    print("1) Terminate remote task and exit")
    print("2) Let task continue and exit")
    print("3) Restart remote task and exit")
    print("4) Redeploy current code to task")
    print("5) Continue monitoring")
    print("6) Restart task and keep monitoring")   # NEW
    try:
        choice = input("Enter 1-6: ").strip()
    except KeyboardInterrupt:
        print("\nExiting."); sys.exit(0)

    hdr = _auth_headers
    if choice == "1":
        _http("DELETE", f"{server_url}/task/{current_task_id}", headers=hdr)
        print("Task terminated."); sys.exit(0)

    if choice == "2":
        print(f"Task {current_task_id} left running."); sys.exit(0)

    if choice == "3":
        _http("POST", f"{server_url}/task/{current_task_id}/restart", headers=hdr)
        print("Task restarted."); sys.exit(0)

    if choice == "4":
        pkg = _package_dir()
        try:
            with open(pkg, "rb") as fh:
                files = {"file": (os.path.basename(pkg), fh, "application/octet-stream")}
                _http("POST", f"{server_url}/task/{current_task_id}/redeploy", headers=hdr, files=files)
        finally:
            os.unlink(pkg)
        print("Task redeployed."); sys.exit(0)

    if choice == "6":                               # NEW
        _http("POST", f"{server_url}/task/{current_task_id}/restart", headers=hdr)
        print("Task restarting…")
        _attach_stream_only(current_task_id,_auth_headers)             # fall through to live attach
        return                                      # continue monitoring

    print("Continuing…")                            # choice == "5"


signal.signal(signal.SIGINT, _signal_handler)

# ───────────────────────── Config / headers ───────────────────────────────
def _read_cfg() -> Dict[str, str]:
    if not os.path.exists("pixi_remote_config.toml"):
        return {}
    import tomli
    with open("pixi_remote_config.toml", "rb") as fh:
        return tomli.load(fh).get("config", {})

def _hdrs(bearer: str|None, snowflake: str|None) -> Dict[str, str]:
    h: Dict[str, str] = {}
    if snowflake: h["Authorization"] = f'Snowflake Token="{snowflake}"'
    elif bearer:  h["Authorization"] = f"Bearer {bearer}"
    return h

# ───────────────────────── Packaging helper ───────────────────────────────
def _package_dir(path=".") -> str:
    tar_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar"); tar_tmp.close()
    with tarfile.open(tar_tmp.name, "w") as tar:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for f in files:
                if f.startswith(".") or f.endswith((".pyc", ".pyo")):
                    continue
                tar.add(os.path.join(root, f), f"{os.path.relpath(root, path)}/{f}".lstrip("./"))
    lz4_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".lz4"); lz4_tmp.close()
    with open(tar_tmp.name, "rb") as src, lz4.frame.open(lz4_tmp.name, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.unlink(tar_tmp.name)
    return lz4_tmp.name

# ───────────────────────── Stream consumer ────────────────────────────────
def _consume_stream(resp):
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
                plain = _dec(enc)
            except Exception as exc:
                print("Decrypt error:", exc)
                continue

            # push through JSON peeler
            plain_buf = plain
            while plain_buf:
                plain_buf = _push_buffer(plain_buf)

# ───────────────────────── Upload / attach wrappers ───────────────────────
def _upload_and_run(pkg, task, headers):
    try:
        with open(pkg, "rb") as fh:
            files = {"file": (os.path.basename(pkg), fh, "application/octet-stream")}
            data  = {"task_name": task, "keep_alive": "true"}
            with _http("POST", f"{server_url}/upload", files=files, data=data, headers=headers,
                       stream=True, timeout=STREAM_TIMEOUT) as r:
                if r.status_code != 200: print("Error:", r.status_code, r.text); return
                _consume_stream(r)
    finally:
        os.unlink(pkg)

def _attach_stream_only(tid: str, headers):
    """FIXED: Only get the live stream - no duplicate recent_output"""
    print(f"Attaching to task {tid}...")

    # Skip the recent_output step and go straight to live stream
    with _http(
        "GET", f"{server_url}/task/{tid}/stream", headers=headers, stream=True, timeout=STREAM_TIMEOUT
    ) as resp:
        if resp.status_code != 200:
            print("Error:", resp.status_code, resp.text)
            return
        _consume_stream(resp)

def _attach(tid: str, headers):
    """FIXED: Use stream-only approach to avoid duplicates"""
    _attach_stream_only(tid, headers)

def _attach_history(tid: str, headers):
    """Get task history from recent_output, then start live stream"""
    # ➊ backlog from recent_output (formatted properly)
    r = _http("GET", f"{server_url}/task/{tid}", headers=headers)
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
    _attach_stream_only(tid, headers)

def _write_secret_file(path: str, data: str):
    """Write a secret file with owner-only permissions (0600)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)

def perform_handshake(secret: str, rotate: bool):
    data = {"secret": secret, "rotate": rotate}
    r = _http("POST", f"{server_url}/handshake", json=data, headers=_auth_headers)
    if r.status_code != 200:
        print("Handshake step-1 failed:", r.text); sys.exit(1)
    pub_pem = r.json()["public_key"]

    # generate AES key + new rotation secret
    new_aes = os.urandom(32)
    new_rot = os.urandom(32)

    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    pub_key = serialization.load_pem_public_key(pub_pem.encode())
    blob = new_aes + new_rot
    import base64
    cipher = base64.b64encode(
        pub_key.encrypt(
            blob,
            padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                         algorithm=hashes.SHA256(), label=None)
        )
    ).decode()

    r2 = _http("POST", f"{server_url}/handshake/finish", json={"cipher": cipher},
               headers=_auth_headers)
    if r2.status_code != 200:
        print("Handshake step-2 failed:", r2.text); sys.exit(1)

    # persist to local file (owner-only permissions)
    _write_secret_file("aes.key", base64.b64encode(new_aes).decode())
    _write_secret_file("rotation.secret", base64.b64encode(new_rot).decode())
    print("Handshake successful – AES key saved (base64)")
    return new_aes, new_rot

# ───────────────────────── Main CLI ───────────────────────────────────────
def main():
    global server_url, aes_key, _auth_headers
    cfg = _read_cfg()

    ap = argparse.ArgumentParser(description="pixi remote client")
    ap.add_argument("--server", default=cfg.get("server_url", server_url))
    ap.add_argument("--task",   default="foobar")
    ap.add_argument("--attach")
    ap.add_argument("--attach-history")
    ap.add_argument("--bearer-token",    default=cfg.get("bearer_token"))
    ap.add_argument("--snowflake-token", default=cfg.get("snowflake_token"))
    ap.add_argument("--aes-key", help="Path to base64 AES key file")
    # NEW flags
    ap.add_argument("--handshake-secret", help="Do handshake then run/attach")
    ap.add_argument("--rotate-secret",    help="Rotate AES key via handshake")
    args = ap.parse_args()

    server_url = args.server
    _auth_headers = _hdrs(args.bearer_token, args.snowflake_token)
    # ➊ optional handshake first
    if args.handshake_secret:
        aes_key, _ = perform_handshake(args.handshake_secret, rotate=False)
    elif args.rotate_secret:
        aes_key, _ = perform_handshake(args.rotate_secret,  rotate=True)

    # ➋ otherwise load key from file if requested
    elif args.aes_key:
        raw = pathlib.Path(args.aes_key).read_bytes()
        try:
            cand = base64.b64decode(raw.strip(), validate=True)
            aes_key = cand if len(cand) == 32 else raw
        except Exception:
            aes_key = raw
        if len(aes_key) != 32:
            print("Invalid AES key length"); sys.exit(1)
        print("AES encryption enabled")

    # ─── run / attach as normal ─────────────────────────────────────────
    if args.attach_history:
        _attach_history(args.attach_history, _auth_headers)
    elif args.attach:
        _attach(args.attach, _auth_headers)
    else:
        pkg = _package_dir()
        _upload_and_run(pkg, args.task, _auth_headers)

if __name__ == "__main__":
    main()
