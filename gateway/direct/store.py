"""Durable state for direct mode — boxes, volumes, buckets and S3 keys, heartbeat credentials,
and revoked tokens.

Everything that must survive a gateway restart lives here: which boxes exist, who owns them, what
they cost, and when they started and stopped (the usage record billing is built on). Built on
sqladal (the pydal API over SQLAlchemy): SQLite by default, Postgres by URI. sqladal's sync DAL
keeps one connection per thread, so the API threads and the provision/reaper workers never share
one; every write commits immediately.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, fields
from typing import List, Optional

from sqladal import DAL, Field

log = logging.getLogger("rixi.direct.store")

LIVE_STATES = ("provisioning", "booting", "ready", "stopping", "stopped", "starting", "releasing")
RUNNING_STATES = ("provisioning", "booting", "ready", "starting")
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
    # Serving: the box runs a model behind its own /v1 API instead of waiting for uploads.
    serve_kind: Optional[str] = None        # None = an ordinary compute box
    serve_model: Optional[str] = None
    model_ready: int = 0                    # the model has finished downloading
    requests_total: int = 0
    last_request: Optional[float] = None
    # Network policy: enforced by the box's own cloud firewall once the box is ready.
    policy: Optional[str] = None            # JSON; None = the open default
    policy_applied: int = 0
    firewall_id: Optional[str] = None
    resolvers: Optional[str] = None         # JSON list the box reported (kept open for DNS)
    # Persistence: volumes that outlive the box, and stopping without losing the disk.
    volumes: Optional[str] = None           # JSON list of volume ids attached at claim
    on_idle: str = "release"                # what an idle box does: release | stop
    stopped_at: Optional[float] = None      # set while stopped (or stopping)
    stopped_total: float = 0.0              # seconds spent stopped before the current stop
    stopped_eur_per_hour: float = 0.0       # its disk, while stopped (snapshotted at claim)

    @property
    def live(self) -> bool:
        return self.state in LIVE_STATES

    def billed_until(self, now: float) -> float:
        return self.ended_at if self.ended_at is not None else now

    def stopped_seconds(self, now: float) -> float:
        current = 0.0
        if self.stopped_at is not None:
            current = max(0.0, self.billed_until(now) - self.stopped_at)
        return (self.stopped_total or 0.0) + current

    def running_seconds(self, now: float) -> float:
        return max(0.0, self.billed_until(now) - self.created_at - self.stopped_seconds(now))

    def volume_ids(self) -> List[str]:
        return json.loads(self.volumes) if self.volumes else []

    def public(self, now: Optional[float] = None) -> dict:
        """What an owner (or the portal) may see. Never includes hb_hash."""
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "hb_hash"}
        d["url"] = f"https://{self.hostname}"
        d["live"] = self.live
        d["policy"] = json.loads(self.policy) if self.policy else None
        d["volumes"] = self.volume_ids()
        d.pop("resolvers", None)
        return d


VOLUME_LIVE = ("creating", "available", "attached", "deleting")


@dataclass
class Volume:
    """A disk that outlives boxes: attached to one box at a time, mounted at
    /workspace/data/<name>, billed per GB-hour from creation until it is deleted."""
    id: str
    tenant: str
    owner: str
    name: str
    provider: str
    zone: str
    size_gb: int
    eur_per_gb_hour: float                  # snapshotted at creation
    state: str = "creating"
    box_id: Optional[str] = None            # attached to, while state == attached
    provider_id: Optional[str] = None
    created_at: float = 0.0
    deleted_at: Optional[float] = None
    error: Optional[str] = None

    @property
    def live(self) -> bool:
        return self.state in VOLUME_LIVE

    def public(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["live"] = self.live
        d["mount"] = f"/workspace/data/{self.name}"
        return d


BUCKET_LIVE = ("ready", "deleting")


@dataclass
class Bucket:
    """An S3 bucket of a tenant: a real bucket under the operator's account (`real_name`),
    reached only through the proxy under the tenant's own `name`. Billed per GB-hour of what it
    holds (measured by the scanner) and per GB downloaded through the proxy."""
    id: str
    tenant: str
    owner: str
    name: str
    region: str
    real_name: str
    state: str = "ready"
    created_at: float = 0.0
    deleted_at: Optional[float] = None
    expire_days: Optional[int] = None       # objects are deleted this many days after upload
    # prices snapshotted at creation
    eur_per_gb_hour: float = 0.0
    eur_per_gb_hour_onezone: float = 0.0
    eur_per_gb_egress: float = 0.0
    # measured by the scanner; GB-hours accumulate between scans
    bytes_std: float = 0.0
    bytes_onezone: float = 0.0
    objects: int = 0
    scanned_at: Optional[float] = None
    gbh_std: float = 0.0
    gbh_onezone: float = 0.0
    # counted by the proxy
    egress_bytes: float = 0.0
    ingress_bytes: float = 0.0
    ingress_at_scan: float = 0.0            # ingress when last measured: estimates growth since
    requests: int = 0
    purge: int = 0                          # being deleted with everything in it
    error: Optional[str] = None

    @property
    def live(self) -> bool:
        return self.state in BUCKET_LIVE

    def estimated_bytes(self) -> float:
        """What it holds now, as far as we know: the last measurement plus uploads since."""
        return self.bytes_std + self.bytes_onezone + max(
            0.0, self.ingress_bytes - self.ingress_at_scan)

    def public(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name not in (
            "real_name", "ingress_at_scan")}
        d["live"] = self.live
        d["size_gb"] = round((self.bytes_std + self.bytes_onezone) / 1e9, 6)
        d["egress_gb"] = round(self.egress_bytes / 1e9, 6)
        return d


@dataclass
class S3Key:
    """An access key for the proxy. The secret is needed to check signatures, so it is kept,
    encrypted; `buckets` limits the key to some of the tenant's buckets (None: all of them,
    including ones made later), `write` allows changes. A box key lives as long as its box."""
    access_key: str
    tenant: str
    owner: str
    name: str
    secret_enc: str
    buckets: Optional[str] = None           # JSON list of bucket ids
    write: int = 0
    box_id: Optional[str] = None
    created_at: float = 0.0
    revoked_at: Optional[float] = None
    last_used_at: Optional[float] = None

    def bucket_ids(self) -> Optional[List[str]]:
        return json.loads(self.buckets) if self.buckets else None

    def public(self) -> dict:
        return {"access_key": self.access_key, "tenant": self.tenant, "owner": self.owner,
                "name": self.name, "buckets": self.bucket_ids(), "write": bool(self.write),
                "box_id": self.box_id, "created_at": self.created_at,
                "revoked_at": self.revoked_at, "last_used_at": self.last_used_at,
                "active": self.revoked_at is None}


_BFIELDS = [f.name for f in fields(Bucket)]
_BDOUBLE = {"created_at", "deleted_at", "eur_per_gb_hour", "eur_per_gb_hour_onezone",
            "eur_per_gb_egress", "bytes_std", "bytes_onezone", "scanned_at", "gbh_std",
            "gbh_onezone", "egress_bytes", "ingress_bytes", "ingress_at_scan"}
_BINT = {"expire_days", "objects", "requests", "purge"}
_KFIELDS = [f.name for f in fields(S3Key)]
_KDOUBLE = {"created_at", "revoked_at", "last_used_at"}


def _plain_field(name: str, doubles: set, ints: set = frozenset(), texts: set = frozenset()):
    if name in doubles:
        return Field(name, "double", default=0.0 if not name.endswith("_at") else None)
    if name in ints:
        return Field(name, "integer", default=None if name == "expire_days" else 0)
    if name in texts:
        return Field(name, "text")
    return Field(name, "string", length=512 if name == "error" else 255)


_VFIELDS = [f.name for f in fields(Volume)]
_VDOUBLE = {"eur_per_gb_hour", "created_at", "deleted_at"}


_FIELDS = [f.name for f in fields(Box)]
_DOUBLE = {"eur_per_hour", "created_at", "expires_at", "idle_timeout", "ready_at", "ended_at",
           "last_heartbeat", "last_busy", "last_request", "stopped_at", "stopped_total",
           "stopped_eur_per_hour"}
_INT = {"active_tasks", "model_ready", "requests_total", "policy_applied"}
_TEXT = {"policy", "resolvers", "volumes"}


def _col(name: str) -> str:
    return "box_id" if name == "id" else name      # pydal reserves `id` for its surrogate key


def _field(name: str) -> Field:
    if name in _DOUBLE:
        return Field(name, "double")
    if name in _INT:
        return Field(name, "integer", default=0)
    if name in _TEXT:
        return Field(name, "text")
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
        self.db.define_table("rixi_volume", *[
            Field("volume_id", "string", length=32, unique=True, notnull=True) if n == "id" else
            Field(n, "double") if n in _VDOUBLE else
            Field(n, "integer") if n == "size_gb" else
            Field(n, "string", length=512 if n == "error" else 255) for n in _VFIELDS])
        self.db.define_table("rixi_bucket", *[
            Field("bucket_id", "string", length=32, unique=True, notnull=True) if n == "id" else
            _plain_field(n, _BDOUBLE, _BINT) for n in _BFIELDS])
        self.db.define_table("rixi_s3_key", *[
            Field("access_key", "string", length=32, unique=True, notnull=True)
            if n == "access_key" else _plain_field(n, _KDOUBLE, {"write"}, {"secret_enc", "buckets"})
            for n in _KFIELDS])
        self.db.define_table("rixi_revoked_jti",
                             Field("box_id", "string", length=32, notnull=True),
                             Field("jti", "string", length=128, notnull=True),
                             Field("revoked_at", "double"))
        self._add_missing_columns()
        self.db.commit()

    def _db_columns(self, table: str) -> set:
        """What the DATABASE has — not what the model declares, which is always complete.

        Uses SQLAlchemy's inspector so SQLite and Postgres take the same path: a
        dialect-specific query here would only ever be exercised in production.
        """
        from sqlalchemy import inspect as sa_inspect
        insp = sa_inspect(self.db._engine)
        if table not in insp.get_table_names():
            return set()
        return {c["name"] for c in insp.get_columns(table)}

    def _add_missing_columns(self) -> None:
        """Add columns a newer release introduced.

        define_table only creates tables that do not exist, so an upgrade that adds a field
        would otherwise query a column the live table has never had — and on Postgres the
        first such query poisons the whole transaction.
        """
        have = self._db_columns("rixi_box")
        if not have:
            return                      # table was just created with every column
        for name in _FIELDS:
            col = _col(name)
            if col in have:
                continue
            kind = ("DOUBLE PRECISION" if name in _DOUBLE else
                    "INTEGER DEFAULT 0" if name in _INT else
                    "TEXT" if name in _TEXT else "VARCHAR(255)")
            try:
                self.db.executesql(f"ALTER TABLE rixi_box ADD COLUMN {col} {kind}")
                self.db.commit()
                log.info("added column rixi_box.%s", col)
            except Exception:
                self.db.rollback()
                log.exception("could not add column rixi_box.%s", col)
                raise

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

    # -- volumes ----------------------------------------------------------
    def _volume(self, row) -> Volume:
        return Volume(**{n: row["volume_id" if n == "id" else n] for n in _VFIELDS})

    def insert_volume(self, v: Volume) -> None:
        self.db.rixi_volume.insert(**{("volume_id" if n == "id" else n): getattr(v, n)
                                      for n in _VFIELDS})
        self.db.commit()

    def update_volume(self, volume_id: str, **changes) -> None:
        bad = set(changes) - set(_VFIELDS) - {"id"}
        if bad:
            raise KeyError(f"unknown volume fields: {sorted(bad)}")
        self.db(self.db.rixi_volume.volume_id == volume_id).update(**changes)
        self.db.commit()

    def transition_volume(self, volume_id: str, from_states: tuple, **changes) -> bool:
        t = self.db.rixi_volume
        n = self.db((t.volume_id == volume_id) & t.state.belongs(list(from_states))) \
            .update(**changes)
        self.db.commit()
        return n == 1

    def get_volume(self, volume_id: str) -> Optional[Volume]:
        row = self.db(self.db.rixi_volume.volume_id == volume_id).select().first()
        self.db.commit()
        return self._volume(row) if row else None

    def list_volumes(self, tenant: Optional[str] = None, live_only: bool = False,
                     since: Optional[float] = None) -> List[Volume]:
        t = self.db.rixi_volume
        q = t.id > 0
        if tenant is not None:
            q &= t.tenant == tenant
        if live_only:
            q &= t.state.belongs(list(VOLUME_LIVE))
        if since is not None:
            q &= (t.deleted_at == None) | (t.deleted_at >= since)  # noqa: E711
        rows = self.db(q).select(orderby=t.created_at)
        self.db.commit()
        return [self._volume(r) for r in rows]

    # -- buckets ----------------------------------------------------------
    def _bucket(self, row) -> Bucket:
        return Bucket(**{n: row["bucket_id" if n == "id" else n] for n in _BFIELDS})

    def insert_bucket(self, b: Bucket) -> None:
        self.db.rixi_bucket.insert(**{("bucket_id" if n == "id" else n): getattr(b, n)
                                      for n in _BFIELDS})
        self.db.commit()

    def update_bucket(self, bucket_id: str, **changes) -> None:
        bad = set(changes) - set(_BFIELDS) - {"id"}
        if bad:
            raise KeyError(f"unknown bucket fields: {sorted(bad)}")
        self.db(self.db.rixi_bucket.bucket_id == bucket_id).update(**changes)
        self.db.commit()

    def transition_bucket(self, bucket_id: str, from_states: tuple, **changes) -> bool:
        t = self.db.rixi_bucket
        n = self.db((t.bucket_id == bucket_id) & t.state.belongs(list(from_states))) \
            .update(**changes)
        self.db.commit()
        return n == 1

    def add_bucket_traffic(self, bucket_id: str, egress: float, ingress: float,
                           requests: int) -> None:
        """Counters from the proxy, added in the database so concurrent flushes never lose one."""
        self.db.executesql(
            "UPDATE rixi_bucket SET egress_bytes = COALESCE(egress_bytes, 0) + :e, "
            "ingress_bytes = COALESCE(ingress_bytes, 0) + :i, "
            "requests = COALESCE(requests, 0) + :r WHERE bucket_id = :b",
            {"e": float(egress), "i": float(ingress), "r": int(requests), "b": bucket_id})
        self.db.commit()

    def get_bucket(self, bucket_id: str) -> Optional[Bucket]:
        row = self.db(self.db.rixi_bucket.bucket_id == bucket_id).select().first()
        self.db.commit()
        return self._bucket(row) if row else None

    def find_bucket(self, tenant: str, name: str) -> Optional[Bucket]:
        """A tenant's live bucket by the name the tenant gave it."""
        t = self.db.rixi_bucket
        row = self.db((t.tenant == tenant) & (t.name == name) &
                      t.state.belongs(list(BUCKET_LIVE))).select().first()
        self.db.commit()
        return self._bucket(row) if row else None

    def list_buckets(self, tenant: Optional[str] = None, live_only: bool = False,
                     since: Optional[float] = None) -> List[Bucket]:
        t = self.db.rixi_bucket
        q = t.id > 0
        if tenant is not None:
            q &= t.tenant == tenant
        if live_only:
            q &= t.state.belongs(list(BUCKET_LIVE))
        if since is not None:
            q &= (t.deleted_at == None) | (t.deleted_at >= since)  # noqa: E711
        rows = self.db(q).select(orderby=t.created_at)
        self.db.commit()
        return [self._bucket(r) for r in rows]

    # -- S3 keys ----------------------------------------------------------
    def insert_s3_key(self, k: S3Key) -> None:
        self.db.rixi_s3_key.insert(**{n: getattr(k, n) for n in _KFIELDS})
        self.db.commit()

    def get_s3_key(self, access_key: str) -> Optional[S3Key]:
        row = self.db(self.db.rixi_s3_key.access_key == access_key).select().first()
        self.db.commit()
        return S3Key(**{n: row[n] for n in _KFIELDS}) if row else None

    def list_s3_keys(self, tenant: Optional[str] = None, box_id: Optional[str] = None,
                     active_only: bool = True) -> List[S3Key]:
        t = self.db.rixi_s3_key
        q = t.id > 0
        if tenant is not None:
            q &= t.tenant == tenant
        if box_id is not None:
            q &= t.box_id == box_id
        if active_only:
            q &= t.revoked_at == None  # noqa: E711
        rows = self.db(q).select(orderby=t.created_at)
        self.db.commit()
        return [S3Key(**{n: r[n] for n in _KFIELDS}) for r in rows]

    def update_s3_key(self, access_key: str, **changes) -> None:
        bad = set(changes) - set(_KFIELDS)
        if bad:
            raise KeyError(f"unknown key fields: {sorted(bad)}")
        self.db(self.db.rixi_s3_key.access_key == access_key).update(**changes)
        self.db.commit()

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
