"""`rixi shell`: an interactive terminal on the box (the server's /pty WebSocket).

Raw mode on your terminal, keystrokes to the box, the screen back; window resizes follow. With
an AES key (boxes from `rixi up`) every message is sealed; hosted boxes run it over TLS.
"""
from __future__ import annotations

import json
import os
import select
import signal
import sys
import threading
from typing import Optional
from urllib.parse import urlencode

from .crypto import decrypt, encrypt

DATA, RESIZE, EXIT = 0, 1, 2


def pty_url(server_url: str, cols: int, rows: int, cwd: Optional[str] = None) -> str:
    base = server_url.rstrip("/")
    base = "wss://" + base[8:] if base.startswith("https://") else "ws://" + base.split("://", 1)[-1]
    q = {"cols": cols, "rows": rows}
    if cwd:
        q["cwd"] = cwd
    return f"{base}/pty?{urlencode(q)}"


def _pack(key, kind: int, payload: bytes) -> bytes:
    return bytes([kind]) + (encrypt(key, payload) if key else payload)


def interactive(client, cwd: Optional[str] = None) -> int:
    """Run a terminal session until the remote shell exits; returns its exit code."""
    import termios
    import tty

    from websockets.sync.client import connect

    size = os.get_terminal_size() if sys.stdin.isatty() else os.terminal_size((120, 32))
    headers = client._headers()
    key = client.aes_key
    code = {"exit": 0}
    with connect(pty_url(client.server_url, size.columns, size.lines, cwd),
                 additional_headers=headers, max_size=None,
                 open_timeout=30) as ws:
        def reader():
            try:
                for msg in ws:
                    if isinstance(msg, str):
                        msg = msg.encode()
                    kind, payload = msg[0], msg[1:]
                    if key:
                        payload = decrypt(key, payload)
                    if kind == DATA:
                        os.write(sys.stdout.fileno(), payload)
                    elif kind == EXIT:
                        code["exit"] = json.loads(payload or b"{}").get("exit_code", 0)
                        return
            except Exception:
                return

        def on_resize(*_):
            s = os.get_terminal_size()
            try:
                ws.send(_pack(key, RESIZE, json.dumps({"cols": s.columns,
                                                       "rows": s.lines}).encode()))
            except Exception:
                pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        tty_state = None
        if sys.stdin.isatty():
            tty_state = termios.tcgetattr(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno())
            signal.signal(signal.SIGWINCH, on_resize)
        try:
            fd = sys.stdin.fileno()
            while t.is_alive():
                ready, _, _ = select.select([fd], [], [], 0.2)
                if not ready:
                    continue
                data = os.read(fd, 4096)
                if not data:                      # stdin closed (piped input ran out)
                    break
                ws.send(_pack(key, DATA, data))
            t.join(timeout=5)
        finally:
            if tty_state is not None:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, tty_state)
                signal.signal(signal.SIGWINCH, signal.SIG_DFL)
    return code["exit"]
