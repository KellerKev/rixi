"""Render the first-boot user-data for a `rixi up` box from the bundled bootstrap script.

Every value is substituted into a single-quoted shell string or a quoted heredoc, so each one is
validated against a strict pattern first — a crafted ref or SSH key cannot break out of the quoting.
"""
from __future__ import annotations

import re
from importlib import resources
from typing import Optional

_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_AUDIENCE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SECRET = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SSH_KEY = re.compile(r"^(ssh-(ed25519|rsa)|ecdsa-sha2-nistp(256|384|521)|sk-[a-z0-9@.-]+) "
                      r"[A-Za-z0-9+/=]+( [^'\n\r]{0,200})?$")
_PEM = re.compile(r"^-----BEGIN PUBLIC KEY-----\n[A-Za-z0-9+/=\n]+-----END PUBLIC KEY-----\n?$")


class RenderError(ValueError):
    pass


def _check(name: str, value: str, pattern: re.Pattern) -> str:
    if not pattern.match(value):
        raise RenderError(f"refusing to render user-data: invalid {name}")
    return value


def render(*, rixi_ref: str, port: int, audience: str, jwt_public_key_pem: str,
           key_secret: str, ssh_public_key: Optional[str] = None) -> str:
    """Return the shell user-data that installs and starts the rixi server on a new box."""
    if not 1 <= int(port) <= 65535:
        raise RenderError("refusing to render user-data: invalid port")
    template = resources.files("rixi.cloud").joinpath("box_bootstrap.sh").read_text()
    pem = _check("JWT public key", jwt_public_key_pem.strip() + "\n", _PEM).rstrip("\n")
    ssh = ssh_public_key.strip() if ssh_public_key else ""
    if ssh:
        _check("SSH public key", ssh, _SSH_KEY)
    values = {
        "__RIXI_REF__": _check("rixi ref", rixi_ref, _REF),
        "__RIXI_PORT__": str(int(port)),
        "__RIXI_AUDIENCE__": _check("audience", audience, _AUDIENCE),
        "__RIXI_KEY_SECRET__": _check("key secret", key_secret, _SECRET),
        "__RIXI_SSH_PUBKEY__": ssh,
        "__RIXI_JWT_PUBLIC_KEY__": pem,
    }
    for placeholder, value in values.items():
        template = template.replace(placeholder, value)
    return template
