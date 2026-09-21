"""Durable state for direct mode — boxes, their heartbeat credentials, and revoked tokens.

Everything that must survive a gateway restart lives here: which boxes exist, who owns them, what
they cost, and when they started and stopped (the usage record billing is built on). Built on
sqladal (the pydal API over SQLAlchemy): SQLite by default, Postgres by URI. sqladal's sync DAL
keeps one connection per thread, so the API threads and the provision/reaper workers never share
one; every write commits immediately.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, fields
from typing import List, Optional

from sqladal import DAL, Field

LIVE_STATES = ("provisioning", "booting", "ready", "releasing")
ENDED_STATES = ("gone", "failed")


@dataclass
class Box:
    id: str
    tenant: str
    owner: str                      # JWT sub that claimed it
    template: str
    provider: str
    instance_type: str
    zone: str
    eur_per_hour: float             # snapshotted at claim time; billing never re-prices a box
    hostname: str
    state: str = "provisioning"
    created_at: float = 0.0
    expires_at: Optional[float] = None
    idle_timeout: Optional[float] = None
    ready_at: Optional[float] = None
    ended_at: Optional[float] = None
    last_heartbeat: Optional[float] = None
    last_busy: Optional[float] = None
    active_tasks: int = 0
    provider_id: Optional[str] = None
    ip: Optional[str] = None
    hb_hash: str = ""               # sha256 of the box's heartbeat secret; the secret is never stored
    end_reason: Optional[str] = None
    error: Optional[str] = None

    @property
    def live(self) -> bool:
        return self.state in LIVE_STATES

    def billed_until(self, now: float) -> float:
        return self.ended_at if self.ended_at is not None else now

    def public(self, now: Optional[float] = None) -> dict:
        """What an owner (or the portal) may see. Never includes hb_hash."""
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "hb_hash"}
        d["url"] = f"https://{self.hostname}"
        d["live"] = self.live
        return d


_FIELDS = [f.name for f in fields(Box)]
_DOUBLE = {"eur_per_hour", "created_at", "expires_at", "idle_timeout", "ready_at", "ended_at",
           "last_heartbeat", "last_busy"}


def _col(name: str) -> str:
    return "box_id" if name == "id" else name      # pydal reserves `id` for its surrogate key


def _field(name: str) -> Field:
    if name in _DOUBLE:
        return Field(name, "double")
    if name == "active_tasks":
        return Field(name, "integer", default=0)
    if name == "id":
        return Field("box_id", "string", length=32, unique=True, notnull=True)
    return Field(name, "string", length=512 if name == "error" else 255)


def _uri(url: str) -> str:
    if url.startswith("sqlite:///"):                # sqlalchemy-style → pydal-style
        return "sqlite://" + url[len("sqlite:///"):]
    return url


class Store:
    def __init__(self, url: str = "sqlite://rixi-direct.db"):
        uri = _uri(url)
        folder = None
        if uri.startswith("sqlite://") and uri != "sqlite://:memory:":
            path = uri[len("sqlite://"):]
            folder = os.path.dirname(os.path.abspath(path))
            uri = "sqlite://" + os.path.basename(path)
        self.db = DAL(uri, folder=folder)
        self.db.define_table("rixi_box", *[_field(n) for n in _FIELDS])
        self.db.define_table("rixi_revoked_jti",
                             Field("box_id", "string", length=32, notnull=True),
                             Field("jti", "string", length=128, notnull=True),
                             Field("revoked_at", "double"))
        self.db.commit()

    def _box(self, row) -> Box:
        return Box(**{n: row[_col(n)] for n in _FIELDS})

    def _where(self, box_id: str):
        return self.db.rixi_box.box_id == box_id

    # -- boxes ------------------------------------------------------------
    def insert(self, box: Box) -> None:
        self.db.rixi_box.insert(**{_col(n): getattr(box, n) for n in _FIELDS})
        self.db.commit()

    def update(self, box_id: str, **changes) -> None:
        bad = set(changes) - set(_FIELDS) - {"id"}
        if bad:
            raise KeyError(f"unknown box fields: {sorted(bad)}")
        self.db(self._where(box_id)).update(**{_col(k): v for k, v in changes.items()})
        self.db.commit()

    def transition(self, box_id: str, from_states: tuple, **changes) -> bool:
        """Compare-and-set on state, so two paths (reaper, API, reconciler) never both act."""
        n = self.db(self._where(box_id) & self.db.rixi_box.state.belongs(list(from_states))) \
            .update(**{_col(k): v for k, v in changes.items()})
        self.db.commit()
        return n == 1

    def get(self, box_id: str) -> Optional[Box]:
        row = self.db(self._where(box_id)).select().first()
        self.db.commit()
        return self._box(row) if row else None

    def list(self, tenant: Optional[str] = None, live_only: bool = False) -> List[Box]:
        t = self.db.rixi_box
        q = t.id > 0
        if tenant is not None:
            q &= t.tenant == tenant
        if live_only:
            q &= t.state.belongs(list(LIVE_STATES))
        rows = self.db(q).select(orderby=t.created_at)
        self.db.commit()
        return [self._box(r) for r in rows]

    def usage_since(self, since: float, tenant: Optional[str] = None) -> List[Box]:
        """Boxes that were billable at any point after `since` (still live, or ended after it)."""
        t = self.db.rixi_box
        q = (t.ended_at == None) | (t.ended_at >= since)  # noqa: E711
        if tenant is not None:
            q &= t.tenant == tenant
        rows = self.db(q).select(orderby=t.created_at)
        self.db.commit()
        return [self._box(r) for r in rows]

    # -- token revocation -------------------------------------------------
    def revoke(self, box_id: str, jti: str) -> None:
        t = self.db.rixi_revoked_jti
        if self.db((t.box_id == box_id) & (t.jti == jti)).isempty():
            t.insert(box_id=box_id, jti=jti, revoked_at=time.time())
        self.db.commit()

    def revoked(self, box_id: str) -> List[str]:
        t = self.db.rixi_revoked_jti
        rows = self.db(t.box_id == box_id).select(t.jti, orderby=t.jti)
        self.db.commit()
        return [r.jti for r in rows]

    def close(self) -> None:
        self.db.close()
