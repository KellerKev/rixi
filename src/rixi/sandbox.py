"""Sandboxed environments: a throwaway box as one object — create, drive, destroy.

    from rixi import Sandbox

    with Sandbox.create(provider="eutonomous", size="cpu-s",       # or your own cloud: scaleway, …
                        policy={"egress": {"mode": "block"}}) as sb:
        sb.write_file("main.py", "print(6 * 7)")
        print(sb.exec("python3 main.py").stdout)      # 42
    # the box is destroyed here, even if the block raised

A sandbox is a `rixi up` box with a profile, so everything else works on it too (`rixi run
--profile <sb.name>`, the CLI, the Client). `Sandbox.attach(name)` drives an existing profile without
owning it. A sandbox you created is also destroyed when the interpreter exits, as a last resort; a
killed process cannot do that, so `rixi profiles` / `rixi down` remain the way to find and remove
what is left.
"""
from __future__ import annotations

import atexit
import logging
import secrets
import threading
from typing import Dict, Iterator, List, Optional, Union

from .client import Client, ExecResult, RunResult

log = logging.getLogger("rixi.sandbox")

_owned: Dict[str, "Sandbox"] = {}
_owned_lock = threading.Lock()


@atexit.register
def _destroy_leftovers() -> None:
    with _owned_lock:
        left = list(_owned.values())
    for sb in left:
        try:
            log.warning("destroying sandbox %s at exit", sb.name)
            sb.destroy()
        except Exception as exc:  # noqa: BLE001 — never fail interpreter shutdown
            log.error("could not destroy sandbox %s: %s — run `rixi down %s`", sb.name, exc,
                      sb.name)


class Sandbox:
    """One box you drive command by command. Create with `Sandbox.create()` or `attach()`."""

    def __init__(self, profile, *, owned: bool, creds: Optional[Dict[str, Optional[str]]] = None,
                 store=None):
        self.profile = profile
        self.owned = owned
        self._creds = creds
        self._store = store
        self._client: Optional[Client] = None
        self.destroyed = False
        if owned:
            with _owned_lock:
                _owned[profile.name] = self

    # ── lifecycle ──────────────────────────────────────────────────────────
    @classmethod
    def create(cls, *, provider: str, size: str = "sample-cpu", name: Optional[str] = None,
               policy: Optional[dict] = None, allow_from: str = "0.0.0.0/0",
               region: Optional[str] = None, instance_type: Optional[str] = None,
               creds: Optional[Dict[str, Optional[str]]] = None, ssh_key: Optional[str] = None,
               ttl_hours: Optional[float] = None, wait_timeout: float = 900.0, store=None, echo=None) -> "Sandbox":
        """Provision a box, wait until it is ready (and sealed, with a policy), return it.

        Credentials come from `creds`, else the provider's usual env vars, else saved ones.
        Nothing is left billing if this raises.
        """
        from .cloud import up as up_mod
        resolved = up_mod.resolve_credentials(provider, dict(creds or {}))
        profile = up_mod.up(
            provider=provider, size=size, name=name or f"sb-{secrets.token_hex(4)}",
            creds=resolved, instance_type=instance_type, region=region, allow_from=allow_from,
            ssh_key=ssh_key, policy=policy, ttl_hours=ttl_hours, make_default=False, wait_timeout=wait_timeout,
            store=store, echo=echo or log.info)
        return cls(profile, owned=True, creds=resolved, store=store)

    @classmethod
    def attach(cls, name: Optional[str] = None, *, store=None) -> "Sandbox":
        """Drive an existing profile (default: the default one). Not destroyed on exit."""
        from .cloud.profiles import ProfileError, ProfileStore
        store = store or ProfileStore()
        profile = store.resolve(name)
        if profile is None:
            raise ProfileError("no profile to attach to (see `rixi profiles`)")
        return cls(profile, owned=False, store=store)

    def destroy(self) -> None:
        """Destroy the box and forget its profile. Safe to call twice."""
        if self.destroyed:
            return
        from .cloud import up as up_mod
        creds = self._creds or up_mod.resolve_credentials(self.profile.provider, {})
        up_mod.down(self.name, creds, store=self._store, echo=log.info)
        self.destroyed = True
        with _owned_lock:
            _owned.pop(self.name, None)

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc) -> None:
        if self.owned:
            self.destroy()

    def __repr__(self) -> str:
        state = "destroyed" if self.destroyed else self.profile.server_url
        return f"<Sandbox {self.name} {self.profile.provider} {state}>"

    # ── what it is ─────────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return self.profile.name

    @property
    def policy(self) -> Optional[dict]:
        return self.profile.policy

    @property
    def client(self) -> Client:
        if self.destroyed:
            raise RuntimeError(f"sandbox {self.name} is destroyed")
        if self._client is None:
            self._client = self.profile.client()
        return self._client

    # ── commands and files ─────────────────────────────────────────────────
    def exec(self, cmd: Union[str, List[str]], *, cwd: Optional[str] = None,
             env: Optional[Dict[str, str]] = None, timeout: Optional[float] = None,
             stdin: Optional[str] = None) -> ExecResult:
        return self.client.exec(cmd, cwd=cwd, env=env, timeout=timeout, stdin=stdin)

    def exec_stream(self, cmd: Union[str, List[str]], **kw) -> Iterator[tuple]:
        return self.client.exec_stream(cmd, **kw)

    def write_file(self, path: str, data: Union[bytes, str]) -> dict:
        return self.client.write_file(path, data)

    def read_file(self, path: str) -> bytes:
        return self.client.read_file(path)

    def list_files(self, path: str = ".") -> List[dict]:
        return self.client.list_files(path)

    def delete_file(self, path: str) -> None:
        self.client.delete_file(path)

    def run(self, project_dir: str = ".", *, task: str = "default", **kw) -> RunResult:
        """Ship a Pixi project and run one of its tasks (see `Client.run`)."""
        return self.client.run(project_dir, task=task, **kw)
