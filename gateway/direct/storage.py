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
import hashlib
import json
import logging
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote, urlparse

from cryptography.fernet import Fernet

from . import sigv4
from .config import DirectConfig
from .s3client import S3Client, S3Error
from .service import Caller, Denied, NotFound
from .store import BUCKET_LIVE, Bucket, S3Key, Share, Store

log = logging.getLogger("rixi.direct.storage")

_TENANT = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# S3 names, without dots: a dotted name breaks TLS for virtual-hosted requests.
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_KEYNAME = re.compile(r"^[\w .@:/-]{1,64}$")
# The key a tenant's browser console signs with. Customers never see it or its secret; "#" is
# outside _KEYNAME, so no key a customer makes can carry this name.
CONSOLE_KEY = "#console"
CONSOLE_EXPIRES = 15 * 60
MAX_DELETE = 10000
# Share links: at most a week (as long as a presigned URL may live), and each opening gets a
# fresh URL valid for a few minutes, so a revoked link stops working within that.
SHARE_MAX_HOURS = 7 * 24
SHARE_URL_EXPIRES = 5 * 60
MAX_SHARES = 1000                           # live links per tenant
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
# Web pages a tenant lets call a bucket from the browser: https, or http on the tenant's own
# machine while developing. No paths, no wildcards.
_ORIGIN = re.compile(r"^(https://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+"
                     r"|http://(localhost|127\.0\.0\.1))(:\d{1,5})?$")
MAX_ORIGINS = 10
# What a browser may show inline, by extension, always with this content type: nothing that runs
# script when opened (no HTML, no SVG), whatever the uploader claimed.
INLINE_TYPES = {
    "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "avif": "image/avif", "mp4": "video/mp4",
    "webm": "video/webm", "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
    "txt": "text/plain; charset=utf-8",
}


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

    def info(self) -> dict:
        """What the storage offers: where, in which regions, at what (cost) price."""
        return {"endpoint_url": self.st.endpoint_url, "regions": list(self.st.regions),
                "eur_per_gb_hour": self.st.eur_per_gb_hour,
                "eur_per_gb_hour_onezone": self.st.eur_per_gb_hour_onezone,
                "eur_per_gb_egress": self.st.eur_per_gb_egress,
                "shares": bool(self.st.share_url), "share_max_hours": SHARE_MAX_HOURS,
                "public_buckets": self.st.public_buckets,
                "public_cap_gb": self.st.public_cap_gb,
                "public_max_cap_gb": self.st.public_max_cap_gb, "max_cors_origins": MAX_ORIGINS}

    def public_url(self, b: Bucket) -> str:
        """Where anyone downloads a public bucket's files: <this>/<key>."""
        return f"{self.st.endpoint_url}/{b.id}"

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

    def set_public(self, caller: Caller, bucket_id: str, public: bool,
                   cap_gb=None) -> Bucket:
        """Let anyone download the bucket's files with no key (never list them), or stop that.
        Downloads count as the tenant's traffic, up to `cap_gb` a month."""
        b = self._visible(caller, self.store.get_bucket(bucket_id))
        if b.state != "ready":
            raise Denied(f"bucket {b.name} is {b.state}", 409)
        if not isinstance(public, bool):
            raise Denied("public must be true or false", 400)
        changes: dict = {}
        if cap_gb is not None:
            if isinstance(cap_gb, bool) or not isinstance(cap_gb, (int, float)) or \
                    not 0 < cap_gb <= self.st.public_max_cap_gb:
                raise Denied(f"the monthly limit must be more than 0 and at most "
                             f"{self.st.public_max_cap_gb:g} GB", 400)
            changes["public_cap_gb"] = float(cap_gb)
        if public:
            if not self.st.public_buckets:
                raise Denied("public buckets are not offered here", 403)
            if b.public_blocked and not caller.admin:
                raise Denied("public access to this bucket was stopped by the operator", 403)
            allow, reason = self.may_write(b.tenant, caller.sub)
            if not allow:
                raise Denied(reason or "this account cannot publish files now", 402)
            if not b.public_cap_gb and "public_cap_gb" not in changes:
                changes["public_cap_gb"] = self.st.public_cap_gb
        changes["public_read"] = 1 if public else 0
        self.store.update_bucket(b.id, **changes)
        if bool(b.public_read) != public:
            self.audit("bucket.public" if public else "bucket.private", bucket=b.id,
                       tenant=b.tenant, sub=caller.sub, cap_gb=changes.get("public_cap_gb"))
        return self.store.get_bucket(b.id)

    def block_public(self, caller: Caller, bucket_id: str, blocked: bool) -> Bucket:
        """The operator stops a bucket being public (e.g. after an abuse report), so the tenant
        cannot turn it back on — or lifts that."""
        if not caller.admin:
            raise NotFound()
        b = self._visible(caller, self.store.get_bucket(bucket_id))
        if not isinstance(blocked, bool):
            raise Denied("public_blocked must be true or false", 400)
        changes = {"public_blocked": 1 if blocked else 0}
        if blocked:
            changes["public_read"] = 0
        self.store.update_bucket(b.id, **changes)
        self.audit("bucket.public_blocked" if blocked else "bucket.public_unblocked",
                   bucket=b.id, tenant=b.tenant, sub=caller.sub)
        return self.store.get_bucket(b.id)

    def set_cors(self, caller: Caller, bucket_id: str, origins) -> Bucket:
        """Web pages (origins) whose scripts may call the bucket — signed, with a key or a
        presigned URL; this lets a browser read the answers, it grants no access."""
        b = self._visible(caller, self.store.get_bucket(bucket_id))
        if b.state != "ready":
            raise Denied(f"bucket {b.name} is {b.state}", 409)
        if not isinstance(origins, list) or not all(isinstance(o, str) for o in origins):
            raise Denied("cors_origins must be a list of origins", 400)
        clean = list(dict.fromkeys(o.strip().rstrip("/").lower() for o in origins))
        if len(clean) > MAX_ORIGINS:
            raise Denied(f"at most {MAX_ORIGINS} origins", 400)
        for o in clean:
            if not _ORIGIN.match(o):
                raise Denied(f"{o!r} is not an origin: https://host[:port] (http only for "
                             "localhost), no path, no wildcard", 400)
        self.store.update_bucket(b.id, cors=json.dumps(clean) if clean else None)
        self.audit("bucket.cors", bucket=b.id, tenant=b.tenant, sub=caller.sub, origins=clean)
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
        self.store.forget_box_objects(b.id)
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
        mine = [k for k in self.store.list_s3_keys(tenant) if not k.box_id and
                k.name != CONSOLE_KEY]
        if len(mine) >= self.cfg.limits.max_s3_keys:
            raise Denied(f"S3 key limit reached ({self.cfg.limits.max_s3_keys})", 429)
        return self._new_key(tenant, caller.sub, name, ids, bool(write))

    def box_key(self, tenant: str, sub: str, box_id: str, buckets: Optional[List[str]],
                write: bool) -> Tuple[S3Key, str]:
        """A key that works only while `box_id` is live; revoked when the box ends."""
        ids = self._resolve(tenant, buckets) if buckets else None
        return self._new_key(tenant, sub, f"box {box_id}", ids, write, box_id=box_id)

    def list_keys(self, caller: Caller) -> List[S3Key]:
        return [k for k in self.store.list_s3_keys(None if caller.admin else caller.tenant or "\0")
                if k.name != CONSOLE_KEY]

    def revoke_key(self, caller: Caller, access_key: str) -> S3Key:
        k = self.store.get_s3_key(access_key)
        if k is None or k.revoked_at is not None or k.name == CONSOLE_KEY or \
                not (caller.admin or k.tenant == caller.tenant):
            raise NotFound()
        self.store.update_s3_key(access_key, revoked_at=self.clock())
        self.audit("s3key.revoked", tenant=k.tenant, sub=caller.sub, access_key=access_key)
        return self.store.get_s3_key(access_key)

    def revoke_box_keys(self, box_id: str) -> None:
        for k in self.store.list_s3_keys(box_id=box_id):
            self.store.update_s3_key(k.access_key, revoked_at=self.clock())

    def secret(self, k: S3Key) -> str:
        return self.fernet.decrypt(k.secret_enc.encode()).decode()

    # -- the browser console ----------------------------------------------
    def _console_key(self, tenant: str, sub: str) -> Tuple[S3Key, str]:
        for k in self.store.list_s3_keys(tenant):
            if k.name == CONSOLE_KEY and not k.box_id:
                return k, self.secret(k)
        return self._new_key(tenant, sub, CONSOLE_KEY, None, True)

    def _ready(self, caller: Caller, bucket_id: str) -> Bucket:
        b = self._visible(caller, self.store.get_bucket(bucket_id))
        if b.state != "ready":
            raise Denied(f"bucket is {b.state}", 409)
        return b

    @staticmethod
    def _object_key(key) -> str:
        if not isinstance(key, str) or not key or len(key.encode()) > 1024:
            raise Denied("key: 1-1024 bytes", 400)
        if key.startswith("/") or any(seg in (".", "..") for seg in key.split("/")) or \
                any(ord(ch) < 32 for ch in key):
            raise Denied("key: no leading /, no . or .. segments, no control characters", 400)
        return key

    @staticmethod
    def _attachment(key: str, filename: Optional[str]) -> str:
        name = filename if isinstance(filename, str) and filename else key.rsplit("/", 1)[-1]
        return "response-content-disposition=" + quote(
            "attachment; filename*=UTF-8''" + quote(name, safe=""), safe="")

    def _sign(self, b: Bucket, tenant: str, sub: str, method: str, key: str, query: str,
              expires: int) -> dict:
        k, secret = self._console_key(tenant, sub)
        host = urlparse(self.st.endpoint_url).netloc
        path = "/" + b.name + "/" + quote(key, safe="/-_.~")
        qs = sigv4.presign(method, host, path, query, k.access_key, secret, b.region,
                           int(expires), now=self.clock())
        return {"method": method, "url": self.st.endpoint_url.rstrip("/") + path + "?" + qs,
                "expires_at": self.clock() + int(expires)}

    def presign(self, caller: Caller, bucket_id: str, op: str, key: str,
                upload_id: Optional[str] = None, part: Optional[int] = None,
                filename: Optional[str] = None, expires: int = CONSOLE_EXPIRES,
                inline: bool = False) -> dict:
        """A URL the browser uses against the proxy, signed with the tenant's console key, so
        the data goes browser ⇄ proxy and the proxy applies the same checks and counters as for
        any other client. op: get (download; `inline` to show it, for INLINE_TYPES only), put,
        create_multipart, upload_part, complete_multipart, abort_multipart, delete."""
        tenant = self._tenant(caller)
        b = self._ready(caller, bucket_id)
        key = self._object_key(key)
        if op in ("upload_part", "complete_multipart", "abort_multipart") and (
                not isinstance(upload_id, str) or not upload_id):
            raise Denied("upload_id is required", 400)
        if op == "upload_part" and (isinstance(part, bool) or not isinstance(part, int) or
                                    not 1 <= part <= 10000):
            raise Denied("part must be 1-10000", 400)
        method, query = {
            "get": ("GET", ""), "put": ("PUT", ""), "delete": ("DELETE", ""),
            "create_multipart": ("POST", "uploads="),
            "upload_part": ("PUT", f"partNumber={part}&uploadId={quote(upload_id or '', safe='')}"),
            "complete_multipart": ("POST", f"uploadId={quote(upload_id or '', safe='')}"),
            "abort_multipart": ("DELETE", f"uploadId={quote(upload_id or '', safe='')}"),
        }.get(op, (None, None))
        if method is None:
            raise Denied(f"unknown op {op!r}", 400)
        if op == "get" and inline:
            ctype = INLINE_TYPES.get(key.rsplit(".", 1)[-1].lower() if "." in key else "")
            if ctype is None:
                raise Denied("this kind of file cannot be shown inline", 400)
            query = "response-content-disposition=inline&response-content-type=" + \
                quote(ctype, safe="")
        elif op == "get":
            query = self._attachment(key, filename)
        return self._sign(b, tenant, caller.sub, method, key, query, expires)

    def list_objects(self, caller: Caller, bucket_id: str, prefix: str = "",
                     token: Optional[str] = None, max_keys: int = 200) -> dict:
        b = self._ready(caller, bucket_id)
        if not isinstance(prefix, str) or len(prefix.encode()) > 1024:
            raise Denied("prefix: at most 1024 bytes", 400)
        try:
            page = self.client.page(b.region, b.real_name, prefix, token,
                                    max(1, min(int(max_keys), 1000)))
        except S3Error as e:
            raise Denied(f"listing failed: {e.code}", 502)
        return {"bucket": b.name, "prefix": prefix, **page}

    def delete_objects(self, caller: Caller, bucket_id: str, keys: Optional[List[str]] = None,
                       prefixes: Optional[List[str]] = None) -> dict:
        """Delete objects, and everything under folder prefixes (at most MAX_DELETE a call;
        `more` says to call again)."""
        b = self._ready(caller, bucket_id)
        keys = [self._object_key(k) for k in (keys or [])]
        prefixes = list(prefixes or [])
        if any(not isinstance(p, str) or not p.endswith("/") for p in prefixes):
            raise Denied("prefixes must be folder names ending in /", 400)
        real = b.real_name
        todo, more = list(dict.fromkeys(keys)), False
        for p in prefixes:
            for k, _size, _cls in self.client.objects(b.region, real, p):
                if len(todo) >= MAX_DELETE:
                    more = True
                    break
                todo.append(k)
        if len(todo) > MAX_DELETE:
            todo, more = todo[:MAX_DELETE], True
        try:
            for i in range(0, len(todo), 1000):
                self.client.delete_keys(b.region, real, todo[i:i + 1000])
        except S3Error as e:
            raise Denied(f"delete failed: {e.code}", 502)
        self.store.forget_box_objects(b.id, todo)
        self.audit("objects.deleted", bucket=b.id, tenant=b.tenant, sub=caller.sub,
                   count=len(todo), prefixes=prefixes[:5])
        return {"deleted": len(todo), "more": more}

    # -- share links ------------------------------------------------------
    def _share_view(self, sh: Share, buckets: Optional[dict] = None) -> dict:
        now = self.clock()
        b = (buckets or {}).get(sh.bucket_id) or self.store.get_bucket(sh.bucket_id)
        state = ("revoked" if sh.revoked_at is not None else
                 "expired" if sh.expires_at <= now else
                 "gone" if b is None or b.state != "ready" else
                 "used up" if sh.max_downloads is not None and sh.downloads >= sh.max_downloads
                 else "active")
        token = self.fernet.decrypt(sh.token_enc.encode()).decode()
        return {"id": sh.id, "tenant": sh.tenant, "bucket_id": sh.bucket_id,
                "bucket": b.name if b else None,
                "key": sh.key, "filename": sh.filename, "created_at": sh.created_at,
                "expires_at": sh.expires_at, "revoked_at": sh.revoked_at,
                "max_downloads": sh.max_downloads, "downloads": sh.downloads,
                "last_download_at": sh.last_download_at, "state": state,
                "active": state == "active", "url": f"{self.st.share_url}/{token}"}

    def create_share(self, caller: Caller, bucket_id: str, key: str, hours,
                     filename: Optional[str] = None, max_downloads=None) -> dict:
        """A link anyone can open to download `key`, for `hours` (at most a week)."""
        tenant = self._tenant(caller)
        if not self.st.share_url:
            raise Denied("this gateway has no share_url configured", 404)
        b = self._ready(caller, bucket_id)
        key = self._object_key(key)
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or \
                not 0 < hours <= SHARE_MAX_HOURS:
            raise Denied(f"hours must be more than 0 and at most {SHARE_MAX_HOURS}", 400)
        if max_downloads is not None and (isinstance(max_downloads, bool) or
                                          not isinstance(max_downloads, int) or
                                          not 1 <= max_downloads <= 100000):
            raise Denied("max_downloads must be 1-100000 (or omitted)", 400)
        if filename is not None and (not isinstance(filename, str) or not filename or
                                     len(filename) > 255 or "/" in filename or
                                     any(ord(c) < 32 for c in filename)):
            raise Denied("filename: 1-255 characters, no / or control characters", 400)
        now = self.clock()
        live = [sh for sh in self.store.list_shares(tenant, expired_since=now)
                if sh.revoked_at is None]
        if len(live) >= MAX_SHARES:
            raise Denied(f"share link limit reached ({MAX_SHARES} active)", 429)
        try:
            self.client.call("HEAD", b.region, b.real_name, key, ok=(200,))
        except S3Error as e:
            if e.status == 404:
                raise Denied("no such file", 404)
            raise Denied(f"could not check the file: {e.code}", 502)
        token = secrets.token_urlsafe(24)
        sh = Share(id=secrets.token_hex(6), tenant=tenant, owner=caller.sub, bucket_id=b.id,
                   key=key, filename=filename or key.rsplit("/", 1)[-1],
                   token_hash=hashlib.sha256(token.encode()).hexdigest(),
                   token_enc=self.fernet.encrypt(token.encode()).decode(), created_at=now,
                   expires_at=now + float(hours) * 3600, max_downloads=max_downloads)
        self.store.insert_share(sh)
        self.audit("share.created", share=sh.id, bucket=b.id, tenant=tenant, sub=caller.sub,
                   hours=hours, max_downloads=max_downloads)
        return self._share_view(sh, {b.id: b})

    def list_shares(self, caller: Caller, bucket_id: Optional[str] = None,
                    token: Optional[str] = None) -> List[dict]:
        """Live links and those that ended in the last week; with `token`, the one link it opens
        (whenever it ended), e.g. for an operator answering an abuse report."""
        tenant = None if caller.admin and not caller.tenant else self._tenant(caller)
        if token is not None:
            sh = self.store.find_share(hashlib.sha256(token.encode()).hexdigest()) \
                if isinstance(token, str) and _TOKEN.match(token) else None
            if sh is None or (tenant is not None and sh.tenant != tenant):
                return []
            return [self._share_view(sh)]
        if bucket_id is not None:
            self._visible(caller, self.store.get_bucket(bucket_id))
        rows = self.store.list_shares(tenant, bucket_id,
                                      expired_since=self.clock() - SHARE_MAX_HOURS * 3600)
        buckets = {b.id: b for b in self.store.list_buckets(tenant)}
        return [self._share_view(sh, buckets) for sh in rows]

    def revoke_share(self, caller: Caller, share_id: str) -> dict:
        sh = self.store.get_share(share_id) if isinstance(share_id, str) else None
        if sh is None or not (caller.admin or sh.tenant == caller.tenant):
            raise NotFound()
        if sh.revoked_at is None:
            self.store.update_share(sh.id, revoked_at=self.clock())
            self.audit("share.revoked", share=sh.id, tenant=sh.tenant, sub=caller.sub)
        return self._share_view(self.store.get_share(sh.id))

    def open_share(self, token: str) -> str:
        """The URL to send someone who opened a share link to: a download that works for a few
        minutes. Raises NotFound for an unknown token and Denied(410) for one that has ended."""
        if not isinstance(token, str) or not _TOKEN.match(token):
            raise NotFound()
        sh = self.store.find_share(hashlib.sha256(token.encode()).hexdigest())
        if sh is None:
            raise NotFound()
        now = self.clock()
        if sh.revoked_at is not None or sh.expires_at <= now:
            raise Denied("This link has expired.", 410)
        b = self.store.get_bucket(sh.bucket_id)
        if b is None or b.state != "ready":
            raise Denied("This file is no longer available.", 410)
        try:
            self.client.call("HEAD", b.region, b.real_name, sh.key, ok=(200,))
        except S3Error as e:
            if e.status == 404:
                raise Denied("This file is no longer available.", 410)
            raise Denied("Storage is not reachable; try again in a minute.", 502)
        if not self.store.take_download(sh.id, now):
            raise Denied("This link has reached its download limit.", 410)
        signed = self._sign(b, sh.tenant, sh.owner, "GET", sh.key,
                            self._attachment(sh.key, sh.filename),
                            max(1, min(SHARE_URL_EXPIRES, int(sh.expires_at - now))))
        return signed["url"]

    # -- what boxes wrote -------------------------------------------------
    def box_summary(self, caller: Caller) -> List[dict]:
        """Every box that wrote objects that are still there (as far as the proxy saw)."""
        tenant = None if caller.admin and not caller.tenant else self._tenant(caller)
        out = []
        for row in self.store.box_object_summary(tenant):
            box = self.store.get(row["box_id"])
            out.append({**row, "template": box.template if box else None,
                        "state": box.state if box else None,
                        "created_at": box.created_at if box else None,
                        "ended_at": box.ended_at if box else None})
        return out

    def box_objects(self, caller: Caller, box_id: str, before: Optional[float] = None,
                    limit: int = 200) -> dict:
        """The objects a box wrote with its own key, newest first (`before` pages back)."""
        tenant = None if caller.admin and not caller.tenant else self._tenant(caller)
        box = self.store.get(box_id) if isinstance(box_id, str) else None
        if box is not None and not (caller.admin or box.tenant == caller.tenant):
            raise NotFound()
        limit = max(1, min(int(limit), 1000))
        rows = self.store.list_box_objects(tenant, box_id, before, limit + 1)
        if not rows and box is None:
            raise NotFound()
        buckets = {b.id: b for b in self.store.list_buckets(tenant, live_only=True)}
        objects = [{"bucket_id": o.bucket_id, "bucket": buckets[o.bucket_id].name,
                    "key": o.key, "size": o.size, "written_at": o.written_at}
                   for o in rows[:limit] if o.bucket_id in buckets]
        return {"box_id": box_id, "objects": objects,
                "before": rows[limit - 1].written_at if len(rows) > limit else None}

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
