"""`rixi up` / `rixi down`: from a cloud API key to a ready, authenticated rixi box — and back.

up:   keygen (ES256 + one-use handshake secret) → create the box (region fallback on stock-outs)
      → save the profile (so `rixi down` works even if this process dies) → wait for /health
      → RSA→AES key handshake → profile ready.
down: destroy every resource labeled for the box, then forget the profile.

Any failure after the box exists destroys it again, so a failed `up` never leaves anything billing.
"""
from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .. import __version__
from . import cloudinit, presets
from .profiles import (Profile, ProfileError, ProfileStore, _write_private, generate_keypair,
                       load_credentials, save_credentials, validate_name)
from .providers import BoxSpec, CapacityError, ProviderError, make_provider

RIXI_PORT = 9000
REPO_ARCHIVE = "https://github.com/KellerKev/rixi/archive/{ref}.tar.gz"

Echo = Callable[[str], None]


class UpError(RuntimeError):
    pass


def default_ref() -> str:
    """Boxes install the server release matching this client, so both speak the same protocol."""
    return f"v{__version__}"


def resolve_credentials(provider: str, flags: Dict[str, Optional[str]]) -> Dict[str, Optional[str]]:
    """Flags win, then the provider's usual env vars, then `--save-credentials` storage."""
    env = {
        "hetzner": {"token": "HCLOUD_TOKEN"},
        "scaleway": {"secret_key": "SCW_SECRET_KEY", "access_key": "SCW_ACCESS_KEY",
                     "project_id": "SCW_DEFAULT_PROJECT_ID"},
        "aws": {"access_key_id": "AWS_ACCESS_KEY_ID",
                "secret_access_key": "AWS_SECRET_ACCESS_KEY"},
    }[provider]
    saved = load_credentials(provider)
    return {k: flags.get(k) or os.environ.get(var) or saved.get(k) for k, var in env.items()}


def _check_ref(ref: str) -> None:
    import requests
    try:
        r = requests.head(REPO_ARCHIVE.format(ref=ref), allow_redirects=True, timeout=15)
    except requests.RequestException as exc:
        raise UpError(f"could not reach GitHub to check rixi ref {ref!r}: {exc}") from exc
    if r.status_code != 200:
        raise UpError(f"rixi ref {ref!r} is not published on GitHub (HTTP {r.status_code}). "
                      f"Pass --rixi-ref with a tag, branch, or commit that exists (e.g. main).")


def _public_ip() -> str:
    import requests
    return requests.get("https://api.ipify.org", timeout=10).text.strip()


def _default_ssh_key() -> Optional[str]:
    for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"):
        p = Path.home() / ".ssh" / name
        if p.exists():
            return p.read_text().strip()
    return None


def up(*, provider: str, size: str = "sample-cpu", name: Optional[str] = None,
       creds: Dict[str, Optional[str]], instance_type: Optional[str] = None,
       region: Optional[str] = None, allow_from: str = "0.0.0.0/0",
       ssh_key: Optional[str] = "auto", rixi_ref: Optional[str] = None,
       save_creds: bool = False, make_default: bool = True,
       wait_timeout: float = 900.0, keep_on_failure: bool = False,
       store: Optional[ProfileStore] = None, echo: Echo = print,
       _provider=None, _sleep=time.sleep, _check=True) -> Profile:
    store = store or ProfileStore()
    name = validate_name(name or f"{provider}-{size}")
    if store.exists(name):
        raise UpError(f"profile {name!r} already exists — `rixi down {name}` first, or pick "
                      f"another --name")
    spec_r = presets.resolve(provider, size, instance_type, region)
    ref = rixi_ref or default_ref()
    if _check:
        _check_ref(ref)
    cidrs = [f"{_public_ip()}/32"] if allow_from == "auto" else \
        [c.strip() for c in allow_from.split(",") if c.strip()]
    ssh_pub = _default_ssh_key() if ssh_key == "auto" else (
        Path(ssh_key).expanduser().read_text().strip() if ssh_key else None)

    cloud = _provider or make_provider(provider, creds)
    if save_creds:
        save_credentials(provider, creds)

    priv_pem, pub_pem = generate_keypair()
    key_secret = secrets.token_urlsafe(32)
    audience = f"rixi-{name}"
    user_data = cloudinit.render(rixi_ref=ref, port=RIXI_PORT, audience=audience,
                                 jwt_public_key_pem=pub_pem, key_secret=key_secret,
                                 ssh_public_key=ssh_pub)
    spec = BoxSpec(name=name, instance_type=spec_r.instance_type, user_data=user_data,
                   gpu=spec_r.gpu, ports=[RIXI_PORT, 22], allow_from=cidrs,
                   ssh_public_key=ssh_pub)

    price = f" (≈ €{spec_r.eur_per_hour}/h)" if spec_r.eur_per_hour else ""
    echo(f"▶ creating {provider} {spec_r.instance_type}{price} as {name!r} — rixi {ref}")
    box = _create_with_fallback(cloud, spec, spec_r.regions, echo)

    profile = Profile(name=name, provider=provider, server_url=f"http://{box.ip}:{RIXI_PORT}",
                      ip=box.ip, region=box.region, instance_type=box.instance_type,
                      box_id=box.box_id, audience=audience, rixi_ref=ref,
                      created_at=time.time(), eur_per_hour=spec_r.eur_per_hour)
    try:
        _write_private(profile.dir / "jwt_private.pem", priv_pem)
        store.put(profile, make_default=make_default)
        echo(f"✔ box {box.box_id} at {box.ip} ({box.region}) — installing rixi "
             f"(first boot takes a few minutes)…")
        client = profile.client()
        _wait_healthy(client, wait_timeout, echo, _sleep)
        profile.save_aes_key(client.handshake(key_secret))
        profile.status = "ready"
        store.put(profile, make_default=make_default)
    except BaseException as exc:
        hint = f" — debug with: ssh root@{box.ip} cat /var/log/rixi-bootstrap.log" \
            if ssh_pub else ""
        if keep_on_failure:
            echo(f"✖ setup failed; box kept as {name!r} for debugging{hint}")
            raise
        echo(f"✖ setup failed ({exc}); destroying the box so nothing keeps billing…")
        try:
            cloud.destroy(name, box.region)
            store.remove(name)
        except Exception as cleanup_exc:  # never mask the original failure
            echo(f"✖ cleanup failed too: {cleanup_exc} — run `rixi down {name}` to retry")
        raise
    echo(f"✔ ready: profile {name!r}{' (default)' if store.default_name() == name else ''} → "
         f"{profile.server_url}")
    return profile


def _create_with_fallback(cloud, spec: BoxSpec, regions: List[str], echo: Echo):
    last: Optional[Exception] = None
    for region in regions:
        try:
            return cloud.create(spec, region)
        except CapacityError as exc:
            last = exc
            echo(f"… no capacity for {spec.instance_type} in {region}; trying the next region")
            _cleanup(cloud, spec.name, region, echo)     # partial resources, before retrying
        except BaseException:
            _cleanup(cloud, spec.name, region, echo)     # partial resources, then fail fast
            raise
    raise UpError(f"no capacity for {spec.instance_type} in {', '.join(regions)}: {last}")


def _cleanup(cloud, name: str, region: str, echo: Echo) -> None:
    try:
        cloud.destroy(name, region)
    except Exception as exc:  # never mask the error that got us here
        echo(f"✖ could not clean up partial resources in {region}: {exc}")


def _wait_healthy(client, timeout: float, echo: Echo, sleep) -> None:
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    last_note = 0.0
    while time.monotonic() < deadline:
        try:
            client.health()
            return
        except Exception:
            pass
        waited = time.monotonic() - start
        if waited - last_note >= 30:
            echo(f"  … still installing ({int(waited)}s)")
            last_note = waited
        sleep(5)
    raise UpError(f"the box did not come up within {int(timeout)}s")


def down(name: str, creds: Dict[str, Optional[str]], *, store: Optional[ProfileStore] = None,
         echo: Echo = print, _provider=None) -> None:
    store = store or ProfileStore()
    profile = store.get(name)
    cloud = _provider or make_provider(profile.provider, creds)
    echo(f"▶ destroying {profile.provider} box {name!r} ({profile.ip}, {profile.region})…")
    cloud.destroy(name, profile.region)
    store.remove(name)
    echo(f"✔ destroyed and removed profile {name!r}")


__all__ = ["up", "down", "resolve_credentials", "default_ref", "UpError", "ProfileError",
           "ProviderError"]
