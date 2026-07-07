"""SMCP-free tunnel wire format — mirrors the OPEN rixi tunnel (rixi/tunnel/rixi_tunnel.py).

The gateway is the multi-agent generalization of the open tunnel's `listen` role, so it must speak
the exact same wire format to interoperate with the unmodified public `rixi-tunnel connect`
(v2 crypto — verified byte-compatible with rixi/tunnel/rixi_tunnel.py):

  master    = PBKDF2-HMAC-SHA256(secret, kdf_salt or b"rixi-tunnel-v2", 600_000, 32)
  AES key   = HKDF(master, info=b"rixi-tunnel-v2-aes")   (AES-256-GCM channel key)
  proof key = HKDF(master, info=b"rixi-tunnel-v2-proof") (separate from the AES key)
  frame     = nonce(12) || AES-256-GCM(json)
  auth      = listener sends challenge{nonce}; dialer returns auth{proof=HMAC(proof_key,nonce), node_id}
  sessions  = session_open{sid} / session_data{sid, data(b64)} / session_close{sid}

KEEP THE SALT/INFO STRINGS AND FRAME FORMAT IN SYNC with the open primitive; the per-deployment
`kdf_salt` (gateway --kdf-salt / RIXI_GATEWAY_SALT) must match every peer, including provisioned
boxes (bootstrap passes it as RIXI_TUNNEL_SALT). This is the protocol layer only — a thin vendored
copy so the gateway is self-contained; it does not fork rixi's execution layer.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

NONCE_LEN = 12
# v2 key derivation — MUST match the open primitive rixi/tunnel/rixi_tunnel.py exactly.
_DEFAULT_SALT = b"rixi-tunnel-v2"
_READ = 65536


def derive_keys(secret: str, kdf_salt: str = "") -> tuple[bytes, bytes]:
    """(AES-256-GCM key, proof HMAC key) from secret + per-deployment salt: master =
    PBKDF2-HMAC-SHA256(secret, salt, 600_000) -> HKDF splits an AES key and a proof key."""
    salt = kdf_salt.encode() if kdf_salt else _DEFAULT_SALT
    master = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, 600_000, dklen=32)
    aes_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"rixi-tunnel-v2-aes").derive(master)
    proof_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"rixi-tunnel-v2-proof").derive(master)
    return aes_key, proof_key


def enc(obj: dict, key: bytes) -> bytes:
    nonce = os.urandom(NONCE_LEN)
    return nonce + AESGCM(key).encrypt(nonce, json.dumps(obj).encode(), None)


def dec(raw: bytes, key: bytes) -> dict:
    return json.loads(AESGCM(key).decrypt(raw[:NONCE_LEN], raw[NONCE_LEN:], None).decode())


def proof(proof_key: bytes, nonce_hex: str) -> str:
    return hmac.new(proof_key, nonce_hex.encode(), hashlib.sha256).hexdigest()


class AgentConn:
    """One registered agent's tunnel: serialized encrypted sends + a session→writer map.

    Mirrors the open tunnel's Conn. The gateway holds one AgentConn per connected rixi server and
    acts as the `listen` side toward it: a local TCP connection opens a session that the agent
    bridges to its own target (the rixi server)."""

    def __init__(self, ws, key: bytes):
        self.ws = ws
        self.key = key
        self._lock = asyncio.Lock()
        self.sessions: dict[str, asyncio.StreamWriter] = {}

    async def send(self, obj: dict):
        async with self._lock:
            await self.ws.send(enc(obj, self.key))

    async def pump_tcp_to_ws(self, sid: str, reader: asyncio.StreamReader):
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
            await self.close_session(sid, notify=True)

    async def on_session_data(self, sid: str, data_b64: str):
        w = self.sessions.get(sid)
        if w is not None:
            try:
                w.write(base64.b64decode(data_b64))
                await w.drain()
            except Exception:
                await self.close_session(sid, notify=True)

    async def close_session(self, sid: str, notify: bool):
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
            await self.close_session(sid, notify=False)
