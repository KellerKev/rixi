"""SMCP (Secure MCP) protocol for rixi — client + server.

SMCP (github.com/KellerKev/smcp) is a WebSocket MCP variant with a Fernet-encrypted,
HMAC-signed envelope and a `handshake -> auth -> capability_discovery -> tool_invoke`
flow. This module lets rixi speak it in BOTH directions, so the agent's MCP interface
gains SMCP as a second protocol that interoperates with malgra (server) and wolfgang
(client). The wire format mirrors those reference implementations verbatim:

  Fernet key = urlsafe_b64( PBKDF2-HMAC-SHA256(secret_key, b"scp_salt_2024", 100000, 32) )
  signature  = hex( HMAC-SHA256(secret_key, id + type + f"{secs}.0") )
  envelope   = {id, type, timestamp: float(secs), payload, encrypted, signature}
  encrypted payload = {"encrypted_data": fernet.encrypt(json)}

Everything is import-guarded: when websockets / cryptography / pyjwt are missing,
HAS_SMCP is False and the client/server raise a clear error instead of crashing the host.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

log = logging.getLogger("rixi.smcp")

try:
    import websockets
    from cryptography.fernet import Fernet
    HAS_SMCP = True
except Exception:  # pragma: no cover - optional dependency
    HAS_SMCP = False

try:
    import jwt as _jwt  # PyJWT, server-side only
    HAS_JWT = True
except Exception:  # pragma: no cover
    HAS_JWT = False

_SALT = b"scp_salt_2024"


# ───────────────────────── crypto / envelope ──────────────────────────────
def _fernet(secret: str):
    key = hashlib.pbkdf2_hmac("sha256", secret.encode(), _SALT, 100_000, dklen=32)
    return Fernet(base64.urlsafe_b64encode(key))


def _sign(secret: str, mid: str, mtype: str, ts_str: str) -> str:
    return hmac.new(secret.encode(), (mid + mtype + ts_str).encode(), hashlib.sha256).hexdigest()


def _envelope(secret: str, fernet, mtype: str, payload: dict, encrypt: bool) -> dict:
    mid, secs = str(uuid.uuid4()), int(time.time())
    pf = {"encrypted_data": fernet.encrypt(json.dumps(payload).encode()).decode()} if encrypt else payload
    return {"id": mid, "type": mtype, "timestamp": float(secs),
            "payload": pf, "encrypted": encrypt, "signature": _sign(secret, mid, mtype, f"{secs}.0")}


def _decrypt(fernet, resp: dict):
    if resp.get("encrypted"):
        return json.loads(fernet.decrypt(resp["payload"]["encrypted_data"].encode()))
    return resp.get("payload")


def _verify_signature(secret: str, msg: dict) -> bool:
    """Recompute and constant-time-compare the envelope signature."""
    try:
        secs = int(float(msg.get("timestamp", 0)))
        expected = _sign(secret, str(msg.get("id", "")), str(msg.get("type", "")), f"{secs}.0")
        return hmac.compare_digest(expected, str(msg.get("signature", "")))
    except Exception:
        return False


# ───────────────────────────── client ─────────────────────────────────────
class SMCPConfig:
    """Plain config bag (mirrors the canonical SMCPConfig surface)."""

    def __init__(self) -> None:
        self.server_url: str = ""
        self.api_key: str = ""
        self.secret_key: str = ""
        self.jwt_secret: str = ""   # server-side only; accepted but unused by the client
        self.node_id: str = ""
        self.mode: str = ""


class SMCPClient:
    """Canonical SMCP WebSocket client (Fernet + HMAC envelope)."""

    def __init__(self, config: SMCPConfig) -> None:
        self.config = config
        self.capabilities: dict = {}
        self._ws = None
        self._token = None
        self._fernet = None

    async def _rt(self, msg: dict) -> dict:
        await self._ws.send(json.dumps(msg))
        return json.loads(await self._ws.recv())

    async def connect(self) -> None:
        if not HAS_SMCP:
            raise RuntimeError("smcp client requires websockets + cryptography")
        secret = self.config.secret_key
        if not secret:
            raise RuntimeError("smcp requires secret_key (the protocol encrypts after handshake)")
        url = self.config.server_url
        if not url:
            raise RuntimeError("smcp requires a server url (ws://…)")
        self._fernet = _fernet(secret)
        f, mk = self._fernet, lambda t, p, e: _envelope(secret, self._fernet, t, p, e)

        self._ws = await websockets.connect(url)

        r = await self._rt(mk("handshake", {"client_id": self.config.node_id or "rixi",
                                             "protocol_version": "1.0"}, False))
        if r.get("type") != "handshake":
            raise RuntimeError(f"smcp handshake failed: {r}")

        auth = _decrypt(f, await self._rt(mk("auth", {"api_key": self.config.api_key or ""}, True)))
        if not isinstance(auth, dict) or auth.get("status") != "success":
            raise RuntimeError(f"smcp auth failed: {auth}")
        self._token = auth.get("token")

        caps = _decrypt(f, await self._rt(mk("capability_discovery", {"token": self._token}, True)))
        self.capabilities = (caps or {}).get("capabilities", {}) if isinstance(caps, dict) else {}

    async def invoke_tool(self, tool_name: str, **params) -> Any:
        if self._ws is None:
            raise RuntimeError("smcp client not connected")
        secret = self.config.secret_key
        msg = _envelope(secret, self._fernet, "tool_invoke",
                        {"token": self._token, "tool_name": tool_name, "parameters": params}, True)
        resp = await self._rt(msg)
        if resp.get("type") == "error":
            err = _decrypt(self._fernet, resp)
            detail = err.get("error") if isinstance(err, dict) else err
            raise RuntimeError(f"smcp tool error: {detail}")
        out = _decrypt(self._fernet, resp)
        return out.get("result") if isinstance(out, dict) else out

    async def disconnect(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None


# ───────────────────────────── server ─────────────────────────────────────
# A tool is: name -> {"description": str, "parameters": <json-schema props>, "handler": callable}
# handler(params: dict) -> result (sync or async).
Tool = Dict[str, Any]
ToolHandler = Callable[[Dict[str, Any]], Any]


class SMCPToolServer:
    """Serves a tool registry over the SMCP protocol (Fernet + HMAC + JWT sessions).

    Mirrors malgra's reference server: handshake -> auth(api_key -> JWT) ->
    capability_discovery -> tool_invoke, so any SMCP client (wolfgang, malgra's probe)
    can discover and invoke the tools.
    """

    def __init__(self, tools: Dict[str, Tool], *, secret_key: str,
                 jwt_secret: str = "rixi-smcp-default-jwt-secret-change-me", node_id: str = "rixi",
                 api_key: Optional[str] = None,
                 api_keys: Optional[Dict[str, str]] = None,
                 default_agent: str = "rixi-client") -> None:
        if not HAS_SMCP:
            raise RuntimeError("smcp server requires websockets + cryptography")
        if not HAS_JWT:
            raise RuntimeError("smcp server requires pyjwt")
        if not secret_key:
            raise RuntimeError("smcp server requires a secret_key")
        self.tools = tools
        self.secret_key = secret_key
        self.jwt_secret = jwt_secret
        self.node_id = node_id
        self.api_key = api_key
        self.api_keys = api_keys or {}
        self.default_agent = default_agent
        self._fernet = _fernet(secret_key)

    # -- helpers ----------------------------------------------------------
    def capabilities(self) -> Dict[str, Any]:
        return {
            name: {
                "name": name,
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {}),
                "auth_required": True,
            }
            for name, t in self.tools.items()
        }

    def _resolve_agent(self, provided: str) -> Optional[str]:
        # Open dev mode when no keys are configured.
        if not self.api_keys and not self.api_key:
            return self.default_agent
        for agent_id, key in self.api_keys.items():
            if hmac.compare_digest(provided.encode(), str(key).encode()):
                return agent_id
        if self.api_key and hmac.compare_digest(provided.encode(), self.api_key.encode()):
            return self.default_agent
        return None

    def _issue_token(self, client_id: str) -> str:
        now = int(time.time())
        claims = {"client_id": client_id,
                  "permissions": ["tool_invoke", "discovery"],
                  "exp": now + 3600, "iat": now}
        return _jwt.encode(claims, self.jwt_secret, algorithm="HS256")

    def _valid_token(self, token: str) -> bool:
        try:
            _jwt.decode(token, self.jwt_secret, algorithms=["HS256"])
            return True
        except Exception:
            return False

    def _reply(self, mtype: str, payload: dict, encrypt: bool = True) -> dict:
        return _envelope(self.secret_key, self._fernet, mtype, payload, encrypt)

    def _error(self, detail: str) -> dict:
        return self._reply("error", {"error": detail}, True)

    async def handle_message(self, raw: dict) -> Optional[dict]:
        """Process one inbound envelope, return the response envelope (or None)."""
        if not _verify_signature(self.secret_key, raw):
            return self._error("invalid signature")

        mtype = raw.get("type")
        try:
            inner = _decrypt(self._fernet, raw) if raw.get("encrypted") else (raw.get("payload") or {})
        except Exception:
            return self._error("could not decrypt payload")
        if not isinstance(inner, dict):
            inner = {}

        if mtype == "handshake":
            return self._reply("handshake", {
                "node_id": self.node_id,
                "protocol_version": "1.0",
                "capabilities_count": len(self.tools),
                "encryption_enabled": True,
            }, encrypt=False)

        if mtype == "auth":
            agent = self._resolve_agent(str(inner.get("api_key", "")))
            if agent is None:
                return self._error("Authentication failed")
            return self._reply("auth", {"status": "success",
                                        "token": self._issue_token(agent),
                                        "expires_in": 3600})

        if mtype == "capability_discovery":
            if not self._valid_token(str(inner.get("token", ""))):
                return self._error("Unauthorized")
            return self._reply("capability_discovery", {"capabilities": self.capabilities()})

        if mtype == "tool_invoke":
            if not self._valid_token(str(inner.get("token", ""))):
                return self._error("Unauthorized")
            name = str(inner.get("tool_name", ""))
            params = inner.get("parameters", {})
            tool = self.tools.get(name)
            if tool is None:
                return self._error(f"unknown tool '{name}'")
            try:
                handler: ToolHandler = tool["handler"]
                result = handler(params if isinstance(params, dict) else {})
                if hasattr(result, "__await__"):
                    result = await result  # type: ignore[assignment]
                return self._reply("tool_response", {"tool_name": name, "result": result, "status": "success"})
            except Exception as e:  # noqa: BLE001 - report tool failures over the wire
                return self._error(f"Tool execution failed: {e}")

        return self._error(f"unsupported message type: {mtype}")

    async def _conn_handler(self, ws):
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                await ws.send(json.dumps(self._error("invalid json")))
                continue
            resp = await self.handle_message(msg)
            if resp is not None:
                await ws.send(json.dumps(resp))

    async def serve(self, host: str = "127.0.0.1", port: int = 8770):
        """Start the WebSocket server (returns the server object; caller awaits it)."""
        log.info("SMCP server listening on ws://%s:%d with %d tools", host, port, len(self.tools))
        return await websockets.serve(self._conn_handler, host, port)
