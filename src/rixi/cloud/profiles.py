"""Connection profiles: everything the client needs to reach a box, saved locally.

Layout (all files 0600, directories 0700), rooted at ~/.rixi or $RIXI_CONFIG_DIR:

    profiles.json                     {"default": "<name>", "profiles": {"<name>": {...}}}
    profiles/<name>/jwt_private.pem   ES256 signing key — the box only ever sees the public half
    profiles/<name>/aes.key           base64 AES-256 key, negotiated with the box over RSA
    credentials.json                  cloud API keys, only when `rixi up --save-credentials`

The client signs a fresh, short-lived JWT for every request (aud = the box, exp = 2 minutes, a
unique jti), so a token seen on the wire is useless almost immediately.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Optional

TOKEN_TTL_SECONDS = 120
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class ProfileError(RuntimeError):
    pass


def config_dir() -> Path:
    return Path(os.environ.get("RIXI_CONFIG_DIR") or Path.home() / ".rixi")


def validate_name(name: str) -> str:
    if not _NAME.match(name or ""):
        raise ProfileError(f"invalid profile name {name!r}: use 1-40 lowercase letters, digits, "
                           f"and hyphens, starting with a letter or digit")
    return name


def _write_private(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(data)
    os.chmod(path, 0o600)


@dataclass
class Profile:
    name: str
    provider: str
    server_url: str
    ip: str
    region: str
    instance_type: str
    box_id: str
    audience: str
    rixi_ref: str
    created_at: float
    status: str = "provisioning"          # provisioning → ready
    eur_per_hour: Optional[float] = None

    # ── key material ───────────────────────────────────────────────────────
    @property
    def dir(self) -> Path:
        return config_dir() / "profiles" / self.name

    def private_key_pem(self) -> str:
        return (self.dir / "jwt_private.pem").read_text()

    def aes_key(self) -> Optional[str]:
        p = self.dir / "aes.key"
        return p.read_text().strip() if p.exists() else None

    def save_aes_key(self, key_b64: str) -> None:
        _write_private(self.dir / "aes.key", key_b64 + "\n")

    def mint_token(self) -> str:
        """A fresh ES256 JWT scoped to this box, valid for TOKEN_TTL_SECONDS."""
        import jwt
        now = int(time.time())
        return jwt.encode({"sub": "rixi-cli", "aud": self.audience, "iat": now,
                           "exp": now + TOKEN_TTL_SECONDS, "jti": uuid.uuid4().hex},
                          self.private_key_pem(), algorithm="ES256")

    def client(self, **kw):
        """An SDK Client for this box: per-request tokens + the negotiated AES key."""
        from ..client import Client
        return Client(self.server_url, token=self.mint_token, aes_key=self.aes_key(), **kw)


def generate_keypair() -> tuple[str, str]:
    """Return (private_pem, public_pem) for a new ES256 (P-256) signing key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub


# ── store ────────────────────────────────────────────────────────────────────

class ProfileStore:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or config_dir()
        self.path = self.root / "profiles.json"

    def _load(self) -> dict:
        if not self.path.exists():
            return {"default": None, "profiles": {}}
        return json.loads(self.path.read_text())

    def _save(self, data: dict) -> None:
        _write_private(self.path, json.dumps(data, indent=2, sort_keys=True) + "\n")

    def names(self):
        return sorted(self._load()["profiles"])

    def default_name(self) -> Optional[str]:
        return self._load().get("default")

    def get(self, name: str) -> Profile:
        rec = self._load()["profiles"].get(name)
        if rec is None:
            raise ProfileError(f"no profile named {name!r} (see `rixi profiles`)")
        return Profile(**rec)

    def exists(self, name: str) -> bool:
        return name in self._load()["profiles"]

    def put(self, profile: Profile, make_default: bool = False) -> None:
        data = self._load()
        data["profiles"][profile.name] = asdict(profile)
        if make_default or not data.get("default"):
            data["default"] = profile.name
        self._save(data)

    def set_default(self, name: str) -> None:
        data = self._load()
        if name not in data["profiles"]:
            raise ProfileError(f"no profile named {name!r}")
        data["default"] = name
        self._save(data)

    def remove(self, name: str) -> None:
        data = self._load()
        data["profiles"].pop(name, None)
        if data.get("default") == name:
            data["default"] = next(iter(sorted(data["profiles"])), None)
        self._save(data)
        pdir = self.root / "profiles" / name
        if pdir.exists():
            for f in pdir.iterdir():
                f.unlink()
            pdir.rmdir()

    def resolve(self, name: Optional[str] = None) -> Optional[Profile]:
        """--profile, else $RIXI_PROFILE, else the default; None when there are no profiles."""
        name = name or os.environ.get("RIXI_PROFILE") or self.default_name()
        return self.get(name) if name else None


# ── cloud credentials ────────────────────────────────────────────────────────

def load_credentials(provider: str) -> Dict[str, str]:
    path = config_dir() / "credentials.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text()).get(provider, {})


def save_credentials(provider: str, creds: Dict[str, Optional[str]]) -> None:
    path = config_dir() / "credentials.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data[provider] = {k: v for k, v in creds.items() if v}
    _write_private(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
