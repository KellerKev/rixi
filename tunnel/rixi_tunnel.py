#!/usr/bin/env python3
"""rixi reverse tunnel — reach a firewalled rixi server through an outbound tunnel.

A firewalled host that cannot accept inbound connections runs `connect`, which dials OUT over a
WebSocket to a reachable host running `listen`. The listener exposes a local TCP port that is
forwarded — over an AES-256-GCM-encrypted, multiplexed tunnel — to a target on the firewalled
side (the rixi server). Point a normal rixi client at the local port and everything works, because
the tunnel forwards raw TCP and is protocol-agnostic.

    firewalled host:   rixi_server :9000
                       rixi-tunnel connect --to ws://CLIENT:7000 --target 127.0.0.1:9000 --secret S
    reachable host:    rixi-tunnel listen  --bind 127.0.0.1:9100 --ws-bind 0.0.0.0:7000 --secret S
                       rixi_client.py --server http://127.0.0.1:9100   # reaches the firewalled server

Security: every frame is AES-256-GCM with a key derived from the shared --secret (PBKDF2); a wrong
secret cannot decrypt the auth challenge and is rejected. The --ws-bind side is the exposed
surface; keep --bind on loopback.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import uuid

import websockets
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logging.basicConfig(level=os.getenv("RIXI_TUNNEL_LOG", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rixi.tunnel")

NONCE_LEN = 12
_SALT = b"rixi_tunnel_2026"
_READ = 65536


# ───────────────────────── crypto / framing ───────────────────────────────
def derive_key(secret: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", secret.encode(), _SALT, 100_000, dklen=32)


def enc(obj: dict, key: bytes) -> bytes:
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, json.dumps(obj).encode(), None)
    return nonce + ct


def dec(raw: bytes, key: bytes) -> dict:
    return json.loads(AESGCM(key).decrypt(raw[:NONCE_LEN], raw[NONCE_LEN:], None).decode())


def _proof(secret: str, nonce_hex: str) -> str:
    return hmac.new(secret.encode(), nonce_hex.encode(), hashlib.sha256).hexdigest()


# ───────────────────────── connection wrapper ─────────────────────────────
class Conn:
    """One tunnel WebSocket: serialized encrypted sends + a session→writer map."""

    def __init__(self, ws, key: bytes):
        self.ws = ws
        self.key = key
        self._lock = asyncio.Lock()
        self.sessions: dict[str, asyncio.StreamWriter] = {}

    async def send(self, obj: dict):
        async with self._lock:
            await self.ws.send(enc(obj, self.key))

    async def pump_tcp_to_ws(self, sid: str, reader: asyncio.StreamReader):
        """Read bytes from a local/target socket and forward them as session_data frames."""
        try:
            while True:
                data = await reader.read(_READ)
                if not data:
                    break
                await self.send({"type": "session_data", "sid": sid,
                                 "data": base64.b64encode(data).decode()})
        except Exception:
            pass
        finally:
            await self._close_session(sid, notify=True)

    async def on_session_data(self, sid: str, data_b64: str):
        w = self.sessions.get(sid)
        if w is not None:
            try:
                w.write(base64.b64decode(data_b64))
                await w.drain()
            except Exception:
                await self._close_session(sid, notify=True)

    async def _close_session(self, sid: str, notify: bool):
        w = self.sessions.pop(sid, None)
        if w is not None:
            try:
                w.close()
            except Exception:
                pass
        if notify:
            try:
                await self.send({"type": "session_close", "sid": sid})
            except Exception:
                pass

    async def close_all(self):
        for sid in list(self.sessions.keys()):
            await self._close_session(sid, notify=False)


# ───────────────────────────── listen role ────────────────────────────────
async def run_listen(bind: str, ws_bind: str, secret: str):
    key = derive_key(secret)
    bind_host, bind_port = _split_hostport(bind, 9100)
    ws_host, ws_port = _split_hostport(ws_bind, 7000)
    state: dict = {"conn": None}  # current authenticated agent connection

    async def on_agent(ws):
        # Auth: send an encrypted challenge; require a valid HMAC proof back.
        nonce = os.urandom(16).hex()
        await ws.send(enc({"type": "challenge", "nonce": nonce}, key))
        try:
            reply = dec(await asyncio.wait_for(ws.recv(), timeout=10), key)
        except Exception:
            log.warning("agent failed auth (bad secret or no reply)")
            return
        if reply.get("type") != "auth" or not hmac.compare_digest(
                reply.get("proof", ""), _proof(secret, nonce)):
            log.warning("agent rejected: bad auth proof")
            return

        conn = Conn(ws, key)
        state["conn"] = conn
        log.info("agent connected (node=%s) — local port %s:%d is live",
                 reply.get("node_id", "?"), bind_host, bind_port)
        try:
            async for raw in ws:
                obj = dec(raw, key)
                t = obj.get("type")
                if t == "session_data":
                    await conn.on_session_data(obj["sid"], obj["data"])
                elif t == "session_close":
                    await conn._close_session(obj["sid"], notify=False)
                # heartbeat / others: ignore
        except Exception:
            pass
        finally:
            await conn.close_all()
            if state["conn"] is conn:
                state["conn"] = None
            log.info("agent disconnected — local port paused until reconnect")

    async def on_local(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        conn: Conn = state["conn"]
        if conn is None:
            log.warning("local connection refused: no tunnel agent connected")
            writer.close()
            return
        sid = uuid.uuid4().hex
        conn.sessions[sid] = writer
        try:
            await conn.send({"type": "session_open", "sid": sid})
        except Exception:
            writer.close()
            conn.sessions.pop(sid, None)
            return
        await conn.pump_tcp_to_ws(sid, reader)

    ws_server = await websockets.serve(on_agent, ws_host, ws_port, ping_interval=20, ping_timeout=20)
    tcp_server = await asyncio.start_server(on_local, bind_host, bind_port)
    log.info("listen: ws-bind ws://%s:%d  |  local TCP %s:%d", ws_host, ws_port, bind_host, bind_port)
    async with ws_server, tcp_server:
        await asyncio.gather(ws_server.wait_closed(), tcp_server.serve_forever())


# ──────────────────────────── connect role ────────────────────────────────
async def run_connect(to_url: str, target: str, secret: str, node_id: str = "rixi"):
    key = derive_key(secret)
    t_host, t_port = _split_hostport(target, 9000)
    delay = 2
    while True:
        try:
            await _connect_once(to_url, t_host, t_port, key, secret, node_id)
            delay = 2
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("tunnel down (%s); reconnecting in %ds", e, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


async def _connect_once(to_url, t_host, t_port, key, secret, node_id):
    async with websockets.connect(to_url, ping_interval=20, ping_timeout=20, max_size=None) as ws:
        # Auth: decrypt the challenge (proves we hold the key), return the proof.
        ch = dec(await asyncio.wait_for(ws.recv(), timeout=10), key)
        if ch.get("type") != "challenge":
            raise RuntimeError("expected challenge")
        await ws.send(enc({"type": "auth", "proof": _proof(secret, ch["nonce"]), "node_id": node_id}, key))
        conn = Conn(ws, key)
        log.info("connected to %s — forwarding to %s:%d", to_url, t_host, t_port)
        try:
            async for raw in ws:
                obj = dec(raw, key)
                t = obj.get("type")
                if t == "session_open":
                    await _open_target(conn, obj["sid"], t_host, t_port)
                elif t == "session_data":
                    await conn.on_session_data(obj["sid"], obj["data"])
                elif t == "session_close":
                    await conn._close_session(obj["sid"], notify=False)
        finally:
            await conn.close_all()


async def _open_target(conn: Conn, sid: str, host: str, port: int):
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10)
    except Exception as e:
        log.warning("session %s: cannot reach target %s:%d (%s)", sid[:8], host, port, e)
        await conn._close_session(sid, notify=True)
        return
    conn.sessions[sid] = writer
    asyncio.create_task(conn.pump_tcp_to_ws(sid, reader))


# ───────────────────────────────── cli ────────────────────────────────────
def _split_hostport(hp: str, default_port: int) -> tuple[str, int]:
    if ":" in hp:
        host, _, port = hp.rpartition(":")
        return host or "127.0.0.1", int(port)
    return hp, default_port


def main():
    ap = argparse.ArgumentParser(description="rixi reverse tunnel (encrypted, multiplexed)")
    sub = ap.add_subparsers(dest="role", required=True)

    pl = sub.add_parser("listen", help="reachable side: accept the outbound tunnel, expose a local port")
    pl.add_argument("--bind", default="127.0.0.1:9100", help="local TCP port to expose (loopback by default)")
    pl.add_argument("--ws-bind", default="0.0.0.0:7000", help="address the firewalled server dials into")
    pl.add_argument("--secret", default=os.getenv("RIXI_TUNNEL_SECRET"), help="shared secret (or RIXI_TUNNEL_SECRET)")

    pc = sub.add_parser("connect", help="firewalled side: dial out and forward to a local target")
    pc.add_argument("--to", required=True, help="listener ws URL, e.g. ws://client-host:7000")
    pc.add_argument("--target", default="127.0.0.1:9000", help="local service to expose (the rixi server)")
    pc.add_argument("--secret", default=os.getenv("RIXI_TUNNEL_SECRET"), help="shared secret (or RIXI_TUNNEL_SECRET)")
    pc.add_argument("--node-id", default="rixi", help="identifier sent at auth (for logs)")

    args = ap.parse_args()
    if not args.secret:
        ap.error("a --secret (or RIXI_TUNNEL_SECRET) is required")

    try:
        if args.role == "listen":
            asyncio.run(run_listen(args.bind, args.ws_bind, args.secret))
        else:
            asyncio.run(run_connect(args.to, args.target, args.secret, args.node_id))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
