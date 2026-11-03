"""The interactive terminal: a login shell on a PTY over a WebSocket, plain and sealed."""
import base64
import json
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

import rixi_server

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))
from rixi.crypto import decrypt as unseal, encrypt as seal  # noqa: E402


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(rixi_server, "WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setattr(rixi_server.auth_settings, "enabled", False)
    monkeypatch.setattr(rixi_server, "aes_key", None)
    monkeypatch.setattr(rixi_server, "require_encryption", False)
    return tmp_path / "workspace"


def _read_until(conn, needle, key=None, timeout=10):
    seen, end = b"", time.time() + timeout
    while time.time() < end:
        msg = conn.receive_bytes()
        kind, payload = msg[0], msg[1:]
        if key:
            payload = unseal(key, payload)
        if kind == 2:
            return seen, json.loads(payload)
        seen += payload
        if needle and needle in seen:
            return seen, None
    raise AssertionError(f"never saw {needle!r}: {seen[-300:]!r}")


def _send(conn, kind, payload, key=None):
    conn.send_bytes(bytes([kind]) + (seal(key, payload) if key else payload))


def test_a_shell_runs_in_the_workspace_and_its_exit_ends_the_session(ws):
    with TestClient(rixi_server.app).websocket_connect("/pty?cols=100&rows=30") as conn:
        _send(conn, 0, b"pwd; tput cols; echo $TERM; echo MARK-$((6*7))\n")
        seen, _ = _read_until(conn, b"MARK-42")
        assert str(ws).encode() in seen and b"100" in seen and b"xterm-256color" in seen
        _send(conn, 1, json.dumps({"cols": 61, "rows": 20}).encode())
        _send(conn, 0, b"tput cols; echo SIZE-$((1+1))-DONE\n")
        seen, _ = _read_until(conn, b"SIZE-2-DONE")
        assert b"61" in seen
        _send(conn, 0, b"exit 3\n")
        _, end = _read_until(conn, None)
        assert end == {"exit_code": 3}


def test_a_token_is_required_when_auth_is_on(ws, monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    monkeypatch.setattr(rixi_server.auth_settings, "enabled", True)
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(rixi_server.app).websocket_connect("/pty") as conn:
            conn.receive_bytes()
    assert exc.value.code == 4401


def test_sealed_sessions_with_a_key(ws, monkeypatch):
    key = os.urandom(32)
    monkeypatch.setattr(rixi_server, "aes_key", key)
    with TestClient(rixi_server.app).websocket_connect("/pty") as conn:
        _send(conn, 0, b"echo SEALED-$((2+3))\n", key)
        seen, _ = _read_until(conn, b"SEALED-5", key)
        _send(conn, 0, b"exit\n", key)
        _, end = _read_until(conn, None, key)
        assert end == {"exit_code": 0}


def test_a_session_counts_as_busy_while_open(ws):
    with TestClient(rixi_server.app).websocket_connect("/pty") as conn:
        _send(conn, 0, b"echo READY-$((1+1))\n")
        _read_until(conn, b"READY-2")
        assert TestClient(rixi_server.app).get("/health").json()["active_tasks"] >= 1
        _send(conn, 0, b"exit\n")
        _read_until(conn, None)
