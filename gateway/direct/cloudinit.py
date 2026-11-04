"""User-data for a direct-mode box.

The box gets exactly what it needs and nothing shared: its own id, its own heartbeat secret, the
hostname its certificate is for, and where to fetch token signing keys. The pinned bootstrap script
does the rest (see box/bootstrap-direct.sh). Rendered as JSON, which is valid YAML, so no value can
break out of the document.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from .config import DirectConfig

_SAFE = re.compile(r"^[A-Za-z0-9._:/@=+-]*$")
_SSH_KEY = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|"
                      r"sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/=]+( [^\n\r]{0,200})?$")


def valid_ssh_key(key: str) -> bool:
    return bool(_SSH_KEY.match(key.strip()))


_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,100}$")


def valid_model(name: str) -> bool:
    return bool(_MODEL.match(name))


_LLM_URL = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._-]+)*/?$")
# A box-only token from the hosted gateway, or the customer's own provider key (direct mode).
_LLM_TOKEN = re.compile(r"^[A-Za-z0-9._+/=-]{8,256}$")


def normalize_llm(llm: dict) -> Optional[dict]:
    """Where the box's workloads send model calls, and with which key; None if unusable.

    `openai_base_url` is used as given (it usually ends in /v1); `anthropic_base_url` is
    optional. The older {base_url} form means a gateway serving both under one prefix."""
    if llm.get("base_url") and not llm.get("openai_base_url"):
        base = str(llm["base_url"]).rstrip("/")
        llm = {"openai_base_url": base + "/v1", "anthropic_base_url": base,
               "token": llm.get("token")}
    openai_url = str(llm.get("openai_base_url") or "")
    anthropic_url = str(llm.get("anthropic_base_url") or "")
    token = str(llm.get("token") or "")
    if not _LLM_URL.fullmatch(openai_url) or not _LLM_TOKEN.fullmatch(token):
        return None
    if anthropic_url and not _LLM_URL.fullmatch(anthropic_url):
        return None
    return {"openai_base_url": openai_url.rstrip("/"),
            "anthropic_base_url": anthropic_url.rstrip("/"), "token": token}


def valid_llm(llm: dict) -> bool:
    return normalize_llm(llm) is not None


def render(cfg: DirectConfig, *, box_id: str, tenant: str, hostname: str, box_secret: str,
           ssh_key: Optional[str] = None, serve: Optional[dict] = None,
           llm: Optional[dict] = None, volumes: Optional[list] = None) -> str:
    env = {
        "RIXI_BOX_ID": box_id,
        "RIXI_TENANT": tenant,
        "RIXI_HOSTNAME": hostname,
        "RIXI_HEARTBEAT_URL": cfg.heartbeat_url,
        "RIXI_BOX_SECRET": box_secret,
        "RIXI_JWKS_URL": cfg.box_jwks_url,
        "RIXI_REF": cfg.rixi_ref,
        "RIXI_REPO": cfg.rixi_repo,
    }
    if cfg.acme_email:
        env["RIXI_ACME_EMAIL"] = cfg.acme_email
    if cfg.acme_ca:
        env["RIXI_ACME_CA"] = cfg.acme_ca
    if serve:
        # The box serves a model behind its own /v1, guarded by a key only this box holds.
        if not valid_model(serve["model"]):
            raise ValueError("not a usable model name")
        env["RIXI_SERVE_KIND"] = serve["kind"]
        env["RIXI_SERVE_MODEL"] = serve["model"]
        env["RIXI_ENDPOINT_KEY"] = serve["key"]
        if cfg.ollama_version:
            env["RIXI_OLLAMA_VERSION"] = cfg.ollama_version
    if llm:
        # Model calls: either through a gateway that holds the real key (the box gets a token
        # worthless anywhere else and once the box is gone), or straight to the customer's own
        # endpoint with their own key — their choice. Neither is stored by the gateway.
        llm = normalize_llm(llm)
        if llm is None:
            raise ValueError("not a usable model endpoint")
        env["RIXI_LLM_OPENAI_URL"] = llm["openai_base_url"]
        if llm["anthropic_base_url"]:
            env["RIXI_LLM_ANTHROPIC_URL"] = llm["anthropic_base_url"]
        env["RIXI_LLM_TOKEN"] = llm["token"]
    if volumes:
        # provider id:name pairs; the box finds each disk by id and mounts it under
        # /workspace/data/<name>, formatting it only the first time.
        env["RIXI_VOLUMES"] = "+".join(f"{pid}:{name}" for pid, name in volumes)
    for k, v in env.items():
        if not _SAFE.match(v):
            raise ValueError(f"{k} contains characters not allowed in box env")
    doc: dict = {
        "write_files": [{
            "path": "/etc/rixi/box.env", "permissions": "0600", "owner": "root:root",
            "content": "".join(f"{k}={v}\n" for k, v in env.items()),
        }],
        "runcmd": [[
            "bash", "-c",
            f"curl -fsSL --retry 5 {cfg.bootstrap} -o /opt/rixi-bootstrap-direct.sh "
            "&& bash /opt/rixi-bootstrap-direct.sh",
        ]],
    }
    if ssh_key:
        if not valid_ssh_key(ssh_key):
            raise ValueError("not an OpenSSH public key")
        doc["ssh_authorized_keys"] = [ssh_key.strip()]
    return "#cloud-config\n" + json.dumps(doc, indent=1) + "\n"
