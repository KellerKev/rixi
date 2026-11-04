"""S3 storage for tenants — the control-plane half (the data plane is s3proxy.py).

Every tenant bucket is a real bucket under the operator's object-storage account, named
`<prefix><id>` so names never collide or reveal a tenant. Tenants never see that name or the
operator's key: they get their own access keys, which only the proxy accepts, and the proxy keeps
every request inside the buckets the key may use.

Billing follows volumes: the scanner measures what each bucket holds and accumulates GB-hours
between measurements; the proxy counts bytes downloaded. Both are reported by /api/usage as
running totals, so a billing pass that repeats can never charge twice.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional, Tuple

from cryptography.fernet import Fernet

from .config import DirectConfig
from .s3client import S3Client, S3Error
from .service import Caller, Denied, NotFound
from .store import BUCKET_LIVE, Bucket, S3Key, Store

log = logging.getLogger("rixi.direct.storage")

_TENANT = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# S3 names, without dots: a dotted name breaks TLS for virtual-hosted requests.
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_KEYNAME = re.compile(r"^[\w .@:/-]{1,64}$")


def valid_bucket_name(name) -> bool:
    return isinstance(name, str) and bool(_BUCKET.match(name))


class StorageService:
    def __init__(self, cfg: DirectConfig, store: Store, client: Optional[S3Client] = None,
                 authorize: Optional[Callable[[dict], tuple]] = None, audit=None,
                 clock: Callable[[], float] = time.time):
        if cfg.storage is None:
            raise ValueError("no [direct.storage] configuration")
        self.cfg = cfg
        self.st = cfg.storage
        self.store = store
        self.client = client or S3Client(self.st.upstream, self.st.access_key, self.st.secret_key)
        self.authorize = authorize
        self.audit = audit or (lambda event, **attrs: log.info("%s %s", event, attrs))
        self.clock = clock
        self.fernet = Fernet(self.st.key_secret)
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rixi-s3")
        self._busy: set = set()
        self._lock = threading.Lock()

    # -- helpers ----------------------------------------------------------
    def _tenant(self, caller: Caller) -> str:
        if not caller.tenant or not _TENANT.match(caller.tenant):
            raise Denied("token carries no usable tenant claim", 403)
        return caller.tenant

    def _visible(self, caller: Caller, b: Optional[Bucket]) -> Bucket:
        if b is None or not b.live or not (caller.admin or b.tenant == caller.tenant):
            raise NotFound()
        return b

    def _resolve(self, tenant: str, refs: List[str]) -> List[str]:
        """Bucket names or ids of a tenant → ids."""
        ids = []
        for ref in dict.fromkeys(refs):
            b = self.store.find_bucket(tenant, str(ref))
            if b is None:
                got = self.store.get_bucket(str(ref))
                b = got if got is not None and got.tenant == tenant and got.live else None
            if b is None:
                raise Denied(f"no bucket {ref!r}", 404)
            ids.append(b.id)
        return ids

    def may_write(self, tenant: str, sub: str = "") -> Tuple[bool, str]:
        """Whether the tenant may add data now (asks the billing service, when there is one)."""
        if self.authorize is None:
            return True, ""
        return self.authorize({"tenant": tenant, "sub": sub, "action": "storage.write",
                               "template": "storage", "provider": "s3", "instance_type": "",
                               "zone": "", "eur_per_hour": 0.0, "ttl_s": 0})

    def tenant_bytes(self, tenant: str) -> float:
        return sum(b.estimated_bytes() for b in self.store.list_buckets(tenant, live_only=True))

    # -- buckets ----------------------------------------------------------
    def create_bucket(self, caller: Caller, name: str, region: Optional[str] = None,
                      expire_days: Optional[int] = None) -> Bucket:
        tenant = self._tenant(caller)
        if not valid_bucket_name(name):
            raise Denied("bucket name: 3-63 lowercase letters, digits and '-'", 400)
        region = region or self.st.regions[0]
        if region not in self.st.regions:
            raise Denied(f"storage is offered in {', '.join(self.st.regions)}, not {region}", 400)
        expire_days = self._expiry(expire_days)
        live = self.store.list_buckets(tenant, live_only=True)
        if len(live) >= self.cfg.limits.max_buckets:
            raise Denied(f"bucket limit reached ({self.cfg.limits.max_buckets})", 429)
        if any(b.name == name for b in live):
            raise Denied(f"you already have a bucket called {name}", 409)
        allow, reason = self.may_write(tenant, caller.sub)
        if not allow:
            raise Denied(reason or "not authorized", 402)
        now = self.clock()
        for _ in range(3):
            bid = secrets.token_hex(6)
            real = f"{self.st.bucket_prefix}{bid}"
            try:
                self.client.create_bucket(region, real, {"rixi-bucket": bid, "rixi-tenant": tenant})
                self.client.put_lifecycle(region, real, expire_days, self.st.abort_multipart_days)
                break
            except S3Error as exc:
                if exc.code in ("BucketAlreadyExists", "BucketAlreadyOwnedByYou"):
                    continue
                try:
                    self.client.delete_bucket(region, real)
                except Exception:
                    pass
                raise Denied(f"could not create the bucket: {exc.code}", 502)
        else:
            raise Denied("could not find a free bucket name; try again", 503)
        b = Bucket(id=bid, tenant=tenant, owner=caller.sub, name=name, region=region,
                   real_name=real, state="ready", created_at=now, expire_days=expire_days,
                   eur_per_gb_hour=self.st.eur_per_gb_hour,
                   eur_per_gb_hour_onezone=self.st.eur_per_gb_hour_onezone,
                   eur_per_gb_egress=self.st.eur_per_gb_egress, scanned_at=now)
        self.store.insert_bucket(b)
        self.audit("bucket.created", bucket=bid, tenant=tenant, sub=caller.sub, name=name,
                   region=region)
        return b

    def _expiry(self, days) -> Optional[int]:
        if days in (None, 0):
            return None
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
            raise Denied("expire_days must be 1-3650 (or null to keep objects)", 400)
        return days

    def list_buckets(self, caller: Caller, tenant: Optional[str] = None) -> List[Bucket]:
        if not caller.admin:
            tenant = caller.tenant
            if not tenant:
                return []
        return self.store.list_buckets(tenant, live_only=True)

    def get_bucket(self, caller: Caller, bucket_id: str) -> Bucket:
        return self._visible(caller, self.store.get_bucket(bucket_id))

    def set_expiry(self, caller: Caller, bucket_id: str, expire_days) -> Bucket:
        b = self._visible(caller, self.store.get_bucket(bucket_id))
        if b.state != "ready":
            raise Denied(f"bucket {b.name} is {b.state}", 409)
        days = self._expiry(expire_days)
        try:
            self.client.put_lifecycle(b.region, b.real_name, days, self.st.abort_multipart_days)
        except S3Error as exc:
            raise Denied(f"could not set the lifecycle: {exc.code}", 502)
        self.store.update_bucket(b.id, expire_days=days)
        self.audit("bucket.expiry", bucket=b.id, tenant=b.tenant, sub=caller.sub,
                   expire_days=days)
        return self.store.get_bucket(b.id)

    def delete_bucket(self, caller: Caller, bucket_id: str, force: bool = False) -> Bucket:
        """Remove a bucket. Without `force` it must be empty (as in S3); with it, everything in
        it is deleted first, in the background."""
        b = self._visible(caller, self.store.get_bucket(bucket_id))
        if b.state == "deleting":
            return b
        if not self.store.transition_bucket(b.id, ("ready",), state="deleting",
                                            purge=1 if force else 0):
            raise Denied("the bucket changed state; try again", 409)
        self.audit("bucket.deleting", bucket=b.id, tenant=b.tenant, sub=caller.sub, force=force)
        if force:
            self._schedule_delete(self.store.get_bucket(b.id))
            return self.store.get_bucket(b.id)
        try:
            self.client.delete_bucket(b.region, b.real_name)
        except S3Error as exc:
            # S3 decides emptiness: an upload that raced the request keeps the bucket alive.
            self.store.transition_bucket(b.id, ("deleting",), state="ready")
            if exc.code == "BucketNotEmpty":
                raise Denied(f"bucket {b.name} is not empty (delete with force to empty it)",
                             409)
            raise Denied(f"could not delete the bucket: {exc.code}", 502)
        self._finish_delete(self.store.get_bucket(b.id))
        return self.store.get_bucket(b.id)

    def _schedule_delete(self, b: Bucket) -> None:
        with self._lock:
            if b.id in self._busy:
                return
            self._busy.add(b.id)
        self._pool.submit(self._empty_and_delete, b)

    def _empty_and_delete(self, b: Bucket) -> None:
        try:
            self.client.empty(b.region, b.real_name)
            self.client.delete_bucket(b.region, b.real_name)
            self._finish_delete(b)
        except Exception as exc:
            log.exception("deleting bucket %s failed", b.id)
            self.store.update_bucket(b.id, error=f"delete: {exc}"[:500])   # the scanner retries
        finally:
            with self._lock:
                self._busy.discard(b.id)

    def _retry_plain_delete(self, b: Bucket) -> None:
        try:
            self.client.delete_bucket(b.region, b.real_name)
        except S3Error as exc:
            if exc.code == "BucketNotEmpty":
                self.store.transition_bucket(b.id, ("deleting",), state="ready")
            return
        self._finish_delete(b)

    def _finish_delete(self, b: Bucket) -> None:
        now = self.clock()
        self._account(b, now, 0.0, 0.0, 0, b.ingress_bytes)
        self.store.transition_bucket(b.id, ("deleting",), state="deleted", deleted_at=now,
                                     error=None)
        self.audit("bucket.deleted", bucket=b.id, tenant=b.tenant)

    # -- keys -------------------------------------------------------------
    def _new_key(self, tenant: str, sub: str, name: str, bucket_ids: Optional[List[str]],
                 write: bool, box_id: Optional[str] = None) -> Tuple[S3Key, str]:
        access = "RX" + base64.b32encode(secrets.token_bytes(12)).decode()[:18]
        secret = secrets.token_urlsafe(30)
        k = S3Key(access_key=access, tenant=tenant, owner=sub, name=name,
                  secret_enc=self.fernet.encrypt(secret.encode()).decode(),
                  buckets=json.dumps(bucket_ids) if bucket_ids is not None else None,
                  write=1 if write else 0, box_id=box_id, created_at=self.clock())
        self.store.insert_s3_key(k)
        self.audit("s3key.created", tenant=tenant, sub=sub, access_key=access, box=box_id,
                   write=bool(write), buckets=bucket_ids)
        return k, secret

    def create_key(self, caller: Caller, name: str, buckets: Optional[List[str]] = None,
                   write: bool = True) -> Tuple[S3Key, str]:
        """A key for the tenant's buckets (or only `buckets`, by name or id). The secret is
        returned here once."""
        tenant = self._tenant(caller)
        if not isinstance(name, str) or not _KEYNAME.match(name):
            raise Denied("name: 1-64 letters, digits, spaces and . @ : / -", 400)
        if buckets is not None and (not isinstance(buckets, list) or not buckets):
            raise Denied("buckets must be a non-empty list of bucket names (or omitted for all)",
                         400)
        ids = self._resolve(tenant, buckets) if buckets is not None else None
        mine = [k for k in self.store.list_s3_keys(tenant) if not k.box_id]
        if len(mine) >= self.cfg.limits.max_s3_keys:
            raise Denied(f"S3 key limit reached ({self.cfg.limits.max_s3_keys})", 429)
        return self._new_key(tenant, caller.sub, name, ids, bool(write))

    def box_key(self, tenant: str, sub: str, box_id: str, buckets: Optional[List[str]],
                write: bool) -> Tuple[S3Key, str]:
        """A key that works only while `box_id` is live; revoked when the box ends."""
        ids = self._resolve(tenant, buckets) if buckets else None
        return self._new_key(tenant, sub, f"box {box_id}", ids, write, box_id=box_id)

    def list_keys(self, caller: Caller) -> List[S3Key]:
        return self.store.list_s3_keys(None if caller.admin else caller.tenant or "\0")

    def revoke_key(self, caller: Caller, access_key: str) -> S3Key:
        k = self.store.get_s3_key(access_key)
        if k is None or k.revoked_at is not None or not (caller.admin or k.tenant == caller.tenant):
            raise NotFound()
        self.store.update_s3_key(access_key, revoked_at=self.clock())
        self.audit("s3key.revoked", tenant=k.tenant, sub=caller.sub, access_key=access_key)
        return self.store.get_s3_key(access_key)

    def revoke_box_keys(self, box_id: str) -> None:
        for k in self.store.list_s3_keys(box_id=box_id):
            self.store.update_s3_key(k.access_key, revoked_at=self.clock())

    def secret(self, k: S3Key) -> str:
        return self.fernet.decrypt(k.secret_enc.encode()).decode()

    # -- measuring --------------------------------------------------------
    def _account(self, b: Bucket, now: float, std: float, onezone: float, objects: int,
                 ingress_seen: float) -> None:
        """Accumulate GB-hours since the last measurement (the mean of then and now)."""
        hours = max(0.0, now - (b.scanned_at or b.created_at)) / 3600.0
        self.store.update_bucket(
            b.id, bytes_std=std, bytes_onezone=onezone, objects=int(objects), scanned_at=now,
            ingress_at_scan=ingress_seen,
            gbh_std=round((b.gbh_std or 0.0) + (b.bytes_std + std) / 2 / 1e9 * hours, 9),
            gbh_onezone=round((b.gbh_onezone or 0.0) + (b.bytes_onezone + onezone) / 2 / 1e9 *
                              hours, 9))

    def measure(self, b: Bucket) -> None:
        ingress_seen = b.ingress_bytes
        m = self.client.measure(b.region, b.real_name)
        self._account(b, self.clock(), m["STANDARD"], m["ONEZONE_IA"], m["objects"],
                      ingress_seen)

    def scan_once(self, force: bool = False) -> dict:
        report = {"measured": 0, "failed": 0}
        now = self.clock()
        for b in self.store.list_buckets(live_only=True):
            if b.state == "deleting":
                # finish a delete that failed or was cut short by a restart
                if b.purge:
                    self._schedule_delete(b)
                else:
                    self._retry_plain_delete(b)
                continue
            if not force and b.scanned_at and now - b.scanned_at < self.st.scan_interval:
                continue
            try:
                self.measure(b)
                report["measured"] += 1
            except Exception as exc:
                log.warning("measuring bucket %s failed: %s", b.id, exc)
                report["failed"] += 1
        return report

    def usage(self, caller: Caller, since: float, tenant: Optional[str] = None) -> List[dict]:
        if not caller.admin:
            tenant = caller.tenant
        out = []
        for b in self.store.list_buckets(tenant, since=since):
            if b.state not in BUCKET_LIVE + ("deleted",):
                continue
            out.append({"bucket": b.id, "tenant": b.tenant, "name": b.name, "region": b.region,
                        "state": b.state, "created_at": b.created_at, "deleted_at": b.deleted_at,
                        "gb_hours": round(b.gbh_std or 0.0, 6),
                        "gb_hours_onezone": round(b.gbh_onezone or 0.0, 6),
                        "egress_gb": round((b.egress_bytes or 0.0) / 1e9, 6),
                        "eur_per_gb_hour": b.eur_per_gb_hour,
                        "eur_per_gb_hour_onezone": b.eur_per_gb_hour_onezone,
                        "eur_per_gb_egress": b.eur_per_gb_egress,
                        "size_gb": round((b.bytes_std + b.bytes_onezone) / 1e9, 6)})
        return out

    def start(self, stop_event: threading.Event) -> None:
        def loop():
            while not stop_event.wait(min(300.0, self.st.scan_interval)):
                try:
                    self.scan_once()
                except Exception:
                    log.exception("storage scan failed")
        threading.Thread(target=loop, daemon=True, name="rixi-s3-scan").start()

    def stop(self) -> None:
        self._pool.shutdown(wait=False)
