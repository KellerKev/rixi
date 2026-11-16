"""SMCP-free tunnel wire format — mirrors the OPEN rixi tunnel (rixi/tunnel/rixi_tunnel.py).

The gateway is the multi-agent generalization of the open tunnel's `listen` role, so it must speak
the exact same wire format to interoperate with the unmodified public `rixi-tunnel connect`
(v2 crypto — verified byte-compatible with rixi/tunnel/rixi_tunnel.py):

  master    = PBKDF2-HMAC-SHA256(secret, kdf_salt or b"rixi-tunnel-v2", 600_000, 32)
  AES key   = HKDF(master, info=b"rixi-tunnel-v2-aes")   (AES-256-GCM channel key)
  proof key = HKDF(master, info=b"rixi-tunnel-v2-proof") (separate from the AES key)
  auth      = listener sends challenge{nonce}; dialer returns auth{proof=HMAC(proof_key,nonce),
              node_id, nonce, [node_proof]} — both under the shared AES key
  session   = HKDF(AES key ‖ node token, salt="<listener nonce>|<dialer nonce>",
              info=b"rixi-tunnel-v3-session"): the key for this one connection
  frame     = nonce(12) || AES-256-GCM(session key, json, aad = direction ‖ counter(8))
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


# Which way a frame travels: bound into it, so one side's frames cannot be fed back to it.
TO_DIALER, TO_LISTENER = b"L", b"D"


def session_key(key: bytes, listener_nonce: str, dialer_nonce: str, node_token: str = "") -> bytes:
    """The key for one connection: from the shared key and both sides' fresh nonces — and, for a
    node that was given a token of its own, that token, so others who hold the shared secret
    cannot derive it."""
    return HKDF(algorithm=hashes.SHA256(), length=32,
                salt=(listener_nonce + "|" + dialer_nonce).encode(),
                info=b"rixi-tunnel-v3-session").derive(key + node_token.encode())


def node_proof(node_token: str, listener_nonce: str, dialer_nonce: str) -> str:
    """Proof of a node's own token for this connection (the token itself is never sent)."""
    return hmac.new(node_token.encode(), f"rixi-node|{listener_nonce}|{dialer_nonce}".encode(),
                    hashlib.sha256).hexdigest()


def seal(obj: dict, key: bytes, direction: bytes, counter: int) -> bytes:
    nonce = os.urandom(NONCE_LEN)
    return nonce + AESGCM(key).encrypt(nonce, json.dumps(obj).encode(),
                                       direction + counter.to_bytes(8, "big"))


def unseal(raw: bytes, key: bytes, direction: bytes, counter: int) -> dict:
    return json.loads(AESGCM(key).decrypt(raw[:NONCE_LEN], raw[NONCE_LEN:],
                                          direction + counter.to_bytes(8, "big")).decode())


class AgentConn:
    """One registered agent's tunnel: serialized encrypted sends + a session→writer map.

    Mirrors the open tunnel's Conn. The gateway holds one AgentConn per connected rixi server and
    acts as the `listen` side toward it: a local TCP connection opens a session that the agent
    bridges to its own target (the rixi server)."""

    def __init__(self, ws, key: bytes, sends: bytes = TO_DIALER):
        """`key` is this connection's session key; `sends` the direction this side's frames go."""
        self.ws = ws
        self.key = key
        self._sends = sends
        self._receives = TO_LISTENER if sends == TO_DIALER else TO_DIALER
        self._tx = self._rx = 0
        self._lock = asyncio.Lock()
        self.sessions: dict[str, asyncio.StreamWriter] = {}

    async def send(self, obj: dict):
        async with self._lock:
            frame = seal(obj, self.key, self._sends, self._tx)
            self._tx += 1
            await self.ws.send(frame)

    def open(self, raw: bytes) -> dict:
        """Decrypt the next frame from the peer: it must be the next one, and one of theirs."""
        obj = unseal(raw, self.key, self._receives, self._rx)
        self._rx += 1
        return obj

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
