#!/usr/bin/env python3
"""pixi Remote Runner Client — AES-aware & Ctrl-C menu - FIXED duplicate output"""

from __future__ import annotations
import argparse, base64, os, pathlib, signal, sys, tarfile, tempfile
from typing import Dict
import shutil
import lz4.frame
from rixi_transport import (
    Transport, _http, _hdrs, DEFAULT_HEADERS_FILE, build_custom_headers, set_default_headers,
)

# ───────────────────────── Globals ────────────────────────────────────────
transport = Transport()

# ───────────────────────── Ctrl-C menu ────────────────────────────────────
def _signal_handler(sig, frame):
    if transport.task_id is None:
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

    hdr = transport.auth_headers
    if choice == "1":
        _http("DELETE", f"{transport.server_url}/task/{transport.task_id}", headers=hdr)
        print("Task terminated."); sys.exit(0)

    if choice == "2":
        print(f"Task {transport.task_id} left running."); sys.exit(0)

    if choice == "3":
        _http("POST", f"{transport.server_url}/task/{transport.task_id}/restart", headers=hdr)
        print("Task restarted."); sys.exit(0)

    if choice == "4":
        pkg = _package_dir()
        try:
            with open(pkg, "rb") as fh:
                files = {"file": (os.path.basename(pkg), fh, "application/octet-stream")}
                _http("POST", f"{transport.server_url}/task/{transport.task_id}/redeploy", headers=hdr, files=files)
        finally:
            os.unlink(pkg)
        print("Task redeployed."); sys.exit(0)

    if choice == "6":                               # NEW
        _http("POST", f"{transport.server_url}/task/{transport.task_id}/restart", headers=hdr)
        print("Task restarting…")
        transport._attach_stream_only(transport.task_id, transport.auth_headers)  # fall through to live attach
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

# ───────────────────────── Main CLI ───────────────────────────────────────
def main():
    cfg = _read_cfg()

    ap = argparse.ArgumentParser(description="pixi remote client")
    ap.add_argument("--server", default=cfg.get("server_url", transport.server_url))
    ap.add_argument("--task",   default="foobar")
    ap.add_argument("--attach")
    ap.add_argument("--attach-history")
    ap.add_argument("--bearer-token",    default=cfg.get("bearer_token"))
    ap.add_argument("--snowflake-token", default=cfg.get("snowflake_token"))
    ap.add_argument("--header", action="append", metavar="'Key: Value'",
                    help="Extra HTTP header on every request (repeatable; supports ${env:VAR}/${file:path})")
    ap.add_argument("--headers-file", default=None,
                    help=f"JSON file of headers to send (default: {DEFAULT_HEADERS_FILE} if present)")
    ap.add_argument("--aes-key", help="Path to base64 AES key file")
    # NEW flags
    ap.add_argument("--handshake-secret", help="Do handshake then run/attach")
    ap.add_argument("--rotate-secret",    help="Rotate AES key via handshake")
    args = ap.parse_args()

    transport.server_url = args.server
    transport.auth_headers = _hdrs(args.bearer_token, args.snowflake_token)
    set_default_headers(build_custom_headers(args.header, args.headers_file, cfg.get("headers", {})))
    # ➊ optional handshake first
    if args.handshake_secret:
        transport.perform_handshake(args.handshake_secret, rotate=False)
    elif args.rotate_secret:
        transport.perform_handshake(args.rotate_secret,  rotate=True)

    # ➋ otherwise load key from file if requested
    elif args.aes_key:
        raw = pathlib.Path(args.aes_key).read_bytes()
        try:
            cand = base64.b64decode(raw.strip(), validate=True)
            transport.aes_key = cand if len(cand) == 32 else raw
        except Exception:
            transport.aes_key = raw
        if len(transport.aes_key) != 32:
            print("Invalid AES key length"); sys.exit(1)
        print("AES encryption enabled")

    # ─── run / attach as normal ─────────────────────────────────────────
    if args.attach_history:
        transport._attach_history(args.attach_history, transport.auth_headers)
    elif args.attach:
        transport._attach(args.attach, transport.auth_headers)
    else:
        pkg = _package_dir()
        transport._upload_and_run(pkg, args.task, transport.auth_headers)

if __name__ == "__main__":
    main()
