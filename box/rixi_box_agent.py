"""Box agent for direct mode — tells the gateway this box is alive, and nothing about its work.

Every 30 seconds it reports the number of running tasks (from the local server's /health) and
whether the box's public HTTPS name answers yet, and applies the token revocation list the gateway
sends back. Stdlib only; runs with the rixi server's Python.

Environment (from /etc/rixi/box.env): RIXI_BOX_ID, RIXI_BOX_SECRET, RIXI_HEARTBEAT_URL,
RIXI_HOSTNAME. Optional: RIXI_SERVER_URL (default http://127.0.0.1:9000),
RIXI_REVOKED_FILE (default /etc/rixi/revoked_jti), RIXI_HEARTBEAT_INTERVAL (default 30).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

VERSION = "1"


def _get_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def active_tasks(server_url: str) -> int:
    try:
        return int(_get_json(f"{server_url}/health").get("active_tasks", 0))
    except Exception:
        return 0


def tls_ready(hostname: str) -> bool:
    """True once the public name serves a certificate that verifies (so clients can connect)."""
    try:
        with urllib.request.urlopen(f"https://{hostname}/health", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def write_revoked(path: str, jtis) -> None:
    body = "".join(f"{j}\n" for j in sorted(set(jtis)))
    try:
        with open(path) as f:
            if f.read() == body:
                return
    except FileNotFoundError:
        pass
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".revoked.")
    with os.fdopen(fd, "w") as f:
        f.write(body)
    os.replace(tmp, path)       # atomic: the server never reads a half-written list


def beat(env: dict) -> dict:
    payload = json.dumps({
        "box_id": env["RIXI_BOX_ID"],
        "active_tasks": active_tasks(env.get("RIXI_SERVER_URL", "http://127.0.0.1:9000")),
        "tls_ready": tls_ready(env["RIXI_HOSTNAME"]),
        "version": VERSION,
    }).encode()
    req = urllib.request.Request(env["RIXI_HEARTBEAT_URL"], data=payload, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {env['RIXI_BOX_SECRET']}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def main() -> None:
    env = dict(os.environ)
    for key in ("RIXI_BOX_ID", "RIXI_BOX_SECRET", "RIXI_HEARTBEAT_URL", "RIXI_HOSTNAME"):
        if not env.get(key):
            sys.exit(f"rixi-box-agent: {key} is not set")
    revoked_path = env.get("RIXI_REVOKED_FILE", "/etc/rixi/revoked_jti")
    interval = float(env.get("RIXI_HEARTBEAT_INTERVAL", "30"))
    while True:
        try:
            resp = beat(env)
            write_revoked(revoked_path, resp.get("revoked_jti", []))
        except urllib.error.HTTPError as exc:
            print(f"rixi-box-agent: heartbeat refused: {exc.code}", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"rixi-box-agent: heartbeat failed: {exc}", file=sys.stderr, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
