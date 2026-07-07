"""Thin async driver around the `tofu` (or `terraform`) CLI.

Per claim: copy a provider module into a workdir, write a tfvars.json for non-secret vars,
then init → apply → output -json. destroy on release. No cloud SDK — OpenTofu/Terraform does
the cloud work, so the gateway stays provider-agnostic.

Secret handling: provider credentials and secret variables (tunnel_secret, key_secret, …) are
passed via the process environment (provider creds directly; secret tf variables as `TF_VAR_*`)
and are NEVER written to terraform.tfvars.json. OpenTofu does record applied values in
`terraform.tfstate`, so **state and plan files are encrypted at rest** (OpenTofu 1.7+ native
AES-GCM via `TF_ENCRYPTION`, key from `RIXI_STATE_PASSPHRASE` or an auto-generated 0600 key file);
the workdir is also created 0700 / files 0600. For an HA gateway, point state at a remote backend
via `RIXI_STATE_BACKEND` (JSON) or a catalog `[state.backend]` block.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import stat
from pathlib import Path
from typing import Optional


def find_tofu() -> Optional[str]:
    return shutil.which("tofu") or shutil.which("terraform")


def _state_dir() -> Path:
    return Path(os.getenv("RIXI_STATE_DIR") or (Path.home() / ".rixi"))


def _state_passphrase() -> str:
    """Passphrase for state encryption: RIXI_STATE_PASSPHRASE, else an auto-generated key
    persisted 0600 to <state_dir>/state.key so it survives restarts (needed to decrypt on destroy)."""
    env = os.getenv("RIXI_STATE_PASSPHRASE")
    if env:
        return env
    d = _state_dir()
    d.mkdir(parents=True, exist_ok=True)
    keyfile = d / "state.key"
    if keyfile.exists():
        return keyfile.read_text().strip()
    passphrase = secrets.token_urlsafe(32)
    keyfile.write_text(passphrase)
    os.chmod(keyfile, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    return passphrase


def _encryption_env() -> dict:
    """TF_ENCRYPTION config (OpenTofu 1.7+ native state+plan encryption). Older tofu/terraform
    ignore the env var, so setting it is a safe no-op there. Opt out with RIXI_STATE_ENCRYPTION=off."""
    if os.getenv("RIXI_STATE_ENCRYPTION", "on").lower() in ("off", "0", "false", "no"):
        return {}
    p = _state_passphrase().replace("\\", "\\\\").replace('"', '\\"')
    hcl = (
        'key_provider "pbkdf2" "rixi" {\n'
        f'  passphrase = "{p}"\n'
        "}\n"
        'method "aes_gcm" "rixi" {\n'
        "  keys = key_provider.pbkdf2.rixi\n"
        "}\n"
        "state {\n  method = method.aes_gcm.rixi\n}\n"
        "plan {\n  method = method.aes_gcm.rixi\n}\n"
    )
    return {"TF_ENCRYPTION": hcl}


def _backend_config() -> Optional[dict]:
    """Optional remote state backend, from RIXI_STATE_BACKEND (JSON). None = local state."""
    raw = os.getenv("RIXI_STATE_BACKEND")
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
        return cfg if isinstance(cfg, dict) and cfg.get("type") else None
    except (ValueError, TypeError):
        return None


def _backend_hcl(cfg: dict) -> str:
    btype = cfg["type"]
    lines = [f'  backend "{btype}" {{']
    for k, v in cfg.items():
        if k == "type":
            continue
        if isinstance(v, bool):
            lines.append(f"    {k} = {str(v).lower()}")
        elif isinstance(v, (int, float)):
            lines.append(f"    {k} = {v}")
        else:
            lines.append(f'    {k} = "{v}"')
    lines.append("  }")
    return "terraform {\n" + "\n".join(lines) + "\n}\n"


class TofuRunner:
    def __init__(self, module_dir, workdir, binary: Optional[str] = None,
                 env: Optional[dict] = None):
        self.module_dir = Path(module_dir)
        self.workdir = Path(workdir)
        self.binary = binary or find_tofu()
        # Extra env (e.g. provider credentials) merged onto the process env for every call —
        # so creds reach the provider without ever touching tfvars on disk.
        self.env = env or None
        # State is encrypted at rest (TF_ENCRYPTION) on every invocation; optional remote backend.
        self._enc_env = _encryption_env()
        self._backend = _backend_config()

    async def _run(self, *args: str, extra_env: Optional[dict] = None) -> str:
        if not self.binary:
            raise RuntimeError("no `tofu`/`terraform` binary found on PATH")
        merged = {}
        merged.update(self._enc_env)   # state/plan encryption on every call
        if self.env:
            merged.update(self.env)
        if extra_env:
            merged.update(extra_env)
        proc_env = {**os.environ, **merged} if merged else None
        proc = await asyncio.create_subprocess_exec(
            self.binary, *args, cwd=str(self.workdir), env=proc_env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        text = out.decode(errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(f"`{self.binary} {' '.join(args)}` failed:\n{text[-3000:]}")
        return text

    @staticmethod
    def _tf_var_env(secret_vars: Optional[dict]) -> dict:
        # OpenTofu reads TF_VAR_<name> for variable <name>; keeps secrets out of tfvars.json.
        return {f"TF_VAR_{k}": (v if isinstance(v, str) else json.dumps(v))
                for k, v in (secret_vars or {}).items() if v is not None}

    def _lock_down(self) -> None:
        # State/tfvars may carry sensitive values — restrict to the owner.
        try:
            os.chmod(self.workdir, stat.S_IRWXU)  # 0700
            for f in self.workdir.iterdir():
                if f.is_file():
                    os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass

    def _stage(self, variables: dict):
        self.workdir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.workdir, stat.S_IRWXU)  # 0700 before we write anything
        shutil.copytree(self.module_dir, self.workdir, dirs_exist_ok=True)
        # Optional remote state backend (HA gateway). Local state (still encrypted) when unset.
        if self._backend:
            (self.workdir / "backend.tf").write_text(_backend_hcl(self._backend))
        # Only NON-secret variables are persisted to tfvars.json; secrets go via TF_VAR_ env.
        (self.workdir / "terraform.tfvars.json").write_text(
            json.dumps({k: v for k, v in variables.items() if v is not None}, indent=2))
        self._lock_down()

    async def apply(self, variables: dict, secret_vars: Optional[dict] = None) -> dict:
        self._stage(variables)
        extra = self._tf_var_env(secret_vars)
        await self._run("init", "-input=false", "-no-color", extra_env=extra)
        await self._run("apply", "-auto-approve", "-input=false", "-no-color", extra_env=extra)
        out = await self._run("output", "-json", "-no-color", extra_env=extra)
        self._lock_down()
        return {k: v.get("value") for k, v in json.loads(out or "{}").items()}

    async def validate(self, variables: dict, secret_vars: Optional[dict] = None) -> str:
        self._stage(variables)
        extra = self._tf_var_env(secret_vars)
        await self._run("init", "-input=false", "-backend=false", "-no-color", extra_env=extra)
        return await self._run("validate", "-no-color", extra_env=extra)

    async def destroy(self, variables: Optional[dict] = None,
                      secret_vars: Optional[dict] = None):
        if variables is not None:
            (self.workdir / "terraform.tfvars.json").write_text(
                json.dumps({k: v for k, v in variables.items() if v is not None}, indent=2))
            self._lock_down()
        await self._run("destroy", "-auto-approve", "-input=false", "-no-color",
                        extra_env=self._tf_var_env(secret_vars))
