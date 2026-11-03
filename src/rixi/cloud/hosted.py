"""`rixi up --provider eutonomous`: a box from the hosted service instead of your own cloud account.

The same profile, the same `rixi run` / `rixi exec` / `Sandbox` — only who provisions the box
differs. An account API key (made in the portal) launches the box, mints box tokens and shuts it
down; it never travels to the box. The box itself is reached directly over TLS
(https://b-<id>.run.eutonomous.eu) with a token scoped to that one box, so the service never sits in
the data path.

    export RIXI_API_KEY=rxk_…
    rixi up --provider eutonomous --size cpu-s --egress block
"""
from __future__ import annotations

import json
import time
from typing import Callable, Dict, Optional

from .. import policy as netpolicy
from .profiles import Profile, ProfileStore, _write_private

DEFAULT_API_URL = "https://rixi.eutonomous.eu"
SIZE_ALIASES = {"sample-cpu": "cpu-s", "sample-gpu": "gpu-l4"}
TOKEN_REFRESH_S = 300            # get a new box token when the cached one has < 5 min left

Echo = Callable[[str], None]


class HostedError(RuntimeError):
    pass


class Api:
    """The portal's box API, authenticated with an account API key."""

    def __init__(self, api_key: Optional[str], api_url: Optional[str] = None, session=None):
        if not api_key:
            raise HostedError("eutonomous needs an API key: create one at "
                              f"{(api_url or DEFAULT_API_URL).rstrip('/')}/keys and pass "
                              "--api-key or set RIXI_API_KEY")
        import requests
        self.url = (api_url or DEFAULT_API_URL).rstrip("/")
        self.s = session or requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {api_key}"})

    def _call(self, method: str, path: str, body: Optional[dict] = None, ok=(200, 201, 202)):
        r = self.s.request(method, self.url + path, json=body, timeout=30)
        if r.status_code not in ok:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:200]
            raise HostedError(f"{method} {path} → {r.status_code}: {detail}")
        return r.json()

    def templates(self) -> list:
        return self._call("GET", "/api/templates")["templates"]

    def launch(self, body: dict) -> dict:
        return self._call("POST", "/api/boxes", body)

    def box(self, box_id: str) -> dict:
        return self._call("GET", f"/api/boxes/{box_id}")

    def release(self, box_id: str) -> dict:
        return self._call("DELETE", f"/api/boxes/{box_id}")

    def token(self, box_id: str, label: str = "rixi cli") -> dict:
        return self._call("POST", f"/api/boxes/{box_id}/tokens", {"label": label})


def _template(api: Api, size: str, instance_type: Optional[str]) -> dict:
    name = instance_type or SIZE_ALIASES.get(size, size)
    templates = api.templates()
    for t in templates:
        if t["name"] == name:
            if not t.get("allowed", True):
                raise HostedError(f"{name} needs approval for this account ({t.get('reason', '')})")
            return t
    raise HostedError(f"unknown size {name!r} (choose from: "
                      f"{', '.join(t['name'] for t in templates)})")


def up(*, name: str, size: str, creds: Dict[str, Optional[str]], store: ProfileStore,
       instance_type: Optional[str] = None, region: Optional[str] = None,
       policy: Optional[netpolicy.NetworkPolicy] = None, ttl_hours: Optional[float] = None,
       make_default: bool = True, wait_timeout: float = 900.0, echo: Echo = print,
       _api: Optional[Api] = None, _sleep=time.sleep) -> Profile:
    api = _api or Api(creds.get("api_key"), creds.get("api_url"))
    t = _template(api, size, instance_type)
    body: dict = {"template": t["name"]}
    if region:
        body["zone"] = region
    if ttl_hours:
        body["ttl"] = int(ttl_hours * 3600)
    if policy is not None and not policy.is_default:
        body["policy"] = policy.to_dict()

    price = f" (€{t['eur_per_hour']}/h)" if t.get("eur_per_hour") else ""
    echo(f"▶ launching a {t['name']} box on eutonomous{price}")
    box = api.launch(body)
    profile = Profile(name=name, provider="eutonomous", server_url=box["url"], ip="",
                      region=box["zone"], instance_type=box["instance_type"], box_id=box["id"],
                      audience=f"box-{box['id']}", rixi_ref="", created_at=time.time(),
                      eur_per_hour=box.get("eur_per_hour"), policy=box.get("policy"),
                      api_url=api.url)
    try:
        profile.dir.mkdir(parents=True, exist_ok=True)
        store.put(profile, make_default=make_default)   # so `rixi down` works if we die now
        echo(f"✔ box {box['id']} — booting{' and applying its policy' if body.get('policy') else ''}"
             f" (about two minutes)…")
        _wait_ready(api, profile.box_id, wait_timeout, echo, _sleep)
        save_token(profile, api.token(profile.box_id))
        profile.status = "ready"
        store.put(profile, make_default=make_default)
    except BaseException as exc:
        echo(f"✖ setup failed ({exc}); shutting the box down so nothing keeps billing…")
        try:
            api.release(profile.box_id)
            store.remove(name)
        except Exception as cleanup_exc:  # never mask the original failure
            echo(f"✖ cleanup failed too: {cleanup_exc} — run `rixi down {name}` to retry")
        raise
    echo(f"✔ ready: profile {name!r}{' (default)' if store.default_name() == name else ''} → "
         f"{profile.server_url}")
    return profile


def _wait_ready(api: Api, box_id: str, timeout: float, echo: Echo, sleep) -> dict:
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    last_note = 0.0
    while time.monotonic() < deadline:
        box = api.box(box_id)
        if box["state"] == "ready":
            return box
        if not box.get("live") or box["state"] in ("releasing", "gone", "failed"):
            raise HostedError(f"the box ended while starting: {box.get('end_reason') or box['state']}")
        waited = time.monotonic() - start
        if waited - last_note >= 30:
            echo(f"  … {box['state']} ({int(waited)}s)")
            last_note = waited
        sleep(5)
    raise HostedError(f"the box was not ready within {int(timeout)}s")


def save_token(profile: Profile, minted: dict) -> None:
    _write_private(profile.dir / "box_token.json",
                   json.dumps({"token": minted["token"], "expires_at": minted["expires_at"]}))


def box_token(profile: Profile) -> str:
    """The cached box token, renewed through the API key when it is about to expire."""
    path = profile.dir / "box_token.json"
    try:
        cached = json.loads(path.read_text())
        if cached["expires_at"] - time.time() > TOKEN_REFRESH_S:
            return cached["token"]
    except (OSError, ValueError, KeyError):
        pass
    from .up import resolve_credentials
    creds = resolve_credentials("eutonomous", {})
    api = Api(creds.get("api_key"), profile.api_url or creds.get("api_url"))
    minted = api.token(profile.box_id)
    save_token(profile, minted)
    return minted["token"]


def down(profile: Profile, creds: Dict[str, Optional[str]], _api: Optional[Api] = None) -> None:
    api = _api or Api(creds.get("api_key"), profile.api_url or creds.get("api_url"))
    try:
        api.release(profile.box_id)
    except HostedError as exc:
        if "404" not in str(exc):          # already gone is fine
            raise
