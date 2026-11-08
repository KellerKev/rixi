"""S3 proxy — tenants' buckets on one operator account, reached with the tenants' own keys.

    python -m gateway.direct.s3proxy --config rixi.toml --bind 127.0.0.1:7102

Speaks S3 (path-style, and virtual-hosted under the endpoint's own name). Each request:

  1. must be signed (sigv4, header or presigned URL) with a key the gateway issued; a box's key
     only works while its box is live;
  2. may only touch the buckets that key allows, and only change them if it may write, the
     tenant's account allows it (the billing service is asked, cached for a minute) and the
     tenant is under its storage limit;
  3. is checked against a list of operations — objects, multipart uploads, listings, tagging.
     Bucket policies, ACLs other than private, CORS, website, object lock, replication and
     notification are refused, so no key can make data public or move it elsewhere (a tenant
     makes a bucket public, or lets web origins call it, through the gateway's API instead);
  4. is sent on to the tenant's real bucket, re-signed with the operator's key. Bodies stream
     through; a signed body hash is checked here (holding back the last piece, so a mismatch
     never completes upstream) and signed chunks are checked chunk by chunk.

Listings, multipart results and errors name the real bucket; those (small) responses are
rewritten to the tenant's name. Object bodies are never inspected. Downloaded bytes are
counted per bucket and flushed to the store, where billing reads them.

Nothing is served unsigned. A bucket its tenant made public can also be read by other accounts:
GET or HEAD of `/<bucket id>/<key>`, signed with any of their keys that is not limited to certain
buckets — no listing, no query. It is served only while the bucket's account may add data (so it
stops when the balance runs out) and the bucket is under its monthly limit, billed to that
account, and always as a file that cannot run in the browser: a type from INLINE_TYPES by
extension, anything else as a download, with nosniff and a sandboxing CSP.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import re
import secrets
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

import httpx

from . import sigv4
from .service import Caller, Denied, NotFound
from .storage import INLINE_TYPES, StorageService, valid_bucket_name
from .store import LIVE_STATES, BoxObject, Bucket, S3Key, month

log = logging.getLogger("rixi.direct.s3proxy")

XMLNS = "http://s3.amazonaws.com/doc/2006-03-01/"
CACHE_S = 15.0
WRITE_CACHE_S = 60.0
FLUSH_S = 10.0
MAX_REWRITE = 32 * 1024 * 1024

# Headers that change what a request does and are never forwarded.
_DROP = {"authorization", "host", "x-amz-date", "x-amz-content-sha256", "x-amz-security-token",
         "x-amz-expected-bucket-owner", "x-amz-source-expected-bucket-owner",
         "x-amz-request-payer", "x-amz-website-redirect-location",
         "x-amz-bypass-governance-retention", "x-amz-bucket-object-lock-enabled",
         "x-amz-acl", "x-amz-copy-source", "x-amz-user-agent"}
_DROP_PREFIX = ("x-amz-grant-", "x-amz-object-lock-")
_PASS = {"content-type", "content-md5", "content-encoding", "content-disposition",
         "content-language", "cache-control", "expires", "range", "if-match", "if-none-match",
         "if-modified-since", "if-unmodified-since"}
_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
        "proxy-authenticate", "proxy-authorization", "server"}
_STORAGE_CLASSES = ("STANDARD", "ONEZONE_IA")
_BUCKET_ID = re.compile(r"[0-9a-f]{12}")
# What a download from another account's public bucket passes on: to the bucket, and back to the browser.
_PUBLIC_IN = ("range", "if-match", "if-none-match", "if-modified-since", "if-unmodified-since")
_PUBLIC_OUT = {"content-length", "content-range", "accept-ranges", "etag", "last-modified",
               "cache-control", "expires", "content-encoding"}
_PUBLIC_CODES = {304: None, 404: "NoSuchKey", 412: "PreconditionFailed", 416: "InvalidRange"}

_LIST_Q = {"list-type", "prefix", "delimiter", "marker", "max-keys", "encoding-type",
           "continuation-token", "start-after", "fetch-owner", "x-id", "allow-unordered",
           "optional-object-attributes"}
_UPLOADS_Q = {"uploads", "key-marker", "upload-id-marker", "max-uploads", "prefix", "delimiter",
              "encoding-type", "x-id"}
_VERSIONS_Q = {"versions", "key-marker", "version-id-marker", "max-keys", "prefix", "delimiter",
               "encoding-type", "x-id"}
_GET_Q = {"versionId", "partNumber", "x-id", "response-content-type", "response-content-language",
          "response-expires", "response-cache-control", "response-content-disposition",
          "response-content-encoding"}


@dataclass
class Op:
    name: str
    write: bool = False
    adds_data: bool = False      # counts toward the storage limit
    rewrite: bool = False        # response names the bucket


def classify(method: str, has_key: bool, q: Dict[str, str]) -> Optional[Op]:
    """Which S3 operation a request is, if it is one the proxy allows."""
    keys = set(q) - {"x-id"}
    if not has_key:
        if method in ("GET", "HEAD") and keys <= _LIST_Q:
            return Op("ListObjects", rewrite=True) if method == "GET" else Op("HeadBucket")
        if method == "GET" and "uploads" in keys and keys <= _UPLOADS_Q:
            return Op("ListMultipartUploads", rewrite=True)
        if method == "GET" and "versions" in keys and keys <= _VERSIONS_Q:
            return Op("ListObjectVersions", rewrite=True)
        if method == "GET" and keys == {"location"}:
            return Op("GetBucketLocation")
        if method == "GET" and keys == {"versioning"}:
            return Op("GetBucketVersioning")
        if method == "GET" and keys == {"lifecycle"}:
            return Op("GetBucketLifecycle")
        if method == "POST" and keys == {"delete"}:
            return Op("DeleteObjects", write=True, rewrite=True)
        if method == "PUT" and not keys:
            return Op("CreateBucket", write=True)
        if method == "DELETE" and not keys:
            return Op("DeleteBucket", write=True)
        return None
    if method in ("GET", "HEAD") and keys <= _GET_Q:
        return Op("GetObject" if method == "GET" else "HeadObject")
    if method == "GET" and "uploadId" in keys and keys <= {"uploadId", "max-parts",
                                                            "part-number-marker"}:
        return Op("ListParts", rewrite=True)
    if keys in ({"tagging"}, {"tagging", "versionId"}):
        if method == "GET":
            return Op("GetObjectTagging")
        if method in ("PUT", "DELETE"):
            return Op("PutObjectTagging", write=True)
    if method == "GET" and keys <= {"attributes", "versionId"} and "attributes" in keys:
        return Op("GetObjectAttributes")
    if method == "PUT" and not keys:
        return Op("PutObject", write=True, adds_data=True)
    if method == "PUT" and keys == {"partNumber", "uploadId"}:
        return Op("UploadPart", write=True, adds_data=True)
    if method == "POST" and keys == {"uploads"}:
        return Op("CreateMultipartUpload", write=True, rewrite=True)
    if method == "POST" and keys == {"uploadId"}:
        return Op("CompleteMultipartUpload", write=True, rewrite=True)
    if method == "DELETE" and keys <= {"versionId"}:
        return Op("DeleteObject", write=True)
    if method == "DELETE" and keys == {"uploadId"}:
        return Op("AbortMultipartUpload", write=True)
    return None


class S3Error(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def error_body(code: str, message: str, resource: str = "", request_id: str = "") -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>{code}</Code>'
            f"<Message>{_xml(message)}</Message><Resource>{_xml(resource)}</Resource>"
            f"<RequestId>{request_id}</RequestId></Error>").encode()


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


class Proxy:
    def __init__(self, storage: StorageService, http: Optional[httpx.AsyncClient] = None,
                 clock=time.time):
        self.storage = storage
        self.store = storage.store
        self.st = storage.st
        self.clock = clock
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0, write=300.0),
                                              limits=httpx.Limits(max_connections=200))
        self.endpoint_host = urlparse(self.st.endpoint_url).hostname or ""
        self._keys: Dict[str, Tuple[float, Optional[S3Key], str]] = {}
        self._boxes: Dict[str, Tuple[float, bool]] = {}
        self._writes: Dict[str, Tuple[float, bool, str]] = {}
        self._buckets: Dict[Tuple[str, str], Tuple[float, Optional[Bucket]]] = {}
        self._tenant_bytes: Dict[str, Tuple[float, float]] = {}
        self._by_id: Dict[str, Tuple[float, Optional[Bucket]]] = {}
        self._origins: Tuple[float, set] = (-1e9, set())
        self._public: Dict[str, float] = {}         # public downloads since the last flush
        self._traffic: Dict[str, List[float]] = {}
        self._key_seen: Dict[str, float] = {}       # access key → when it last signed a request
        # objects written with a box's key, and objects deleted, since the last flush
        self._box_writes: Dict[Tuple[str, str], BoxObject] = {}
        self._deleted: Dict[Tuple[str, str], None] = {}
        self._flusher: Optional[asyncio.Task] = None

    # -- cached lookups ---------------------------------------------------
    async def _key(self, access_key: str) -> Tuple[Optional[S3Key], str]:
        now = time.monotonic()
        hit = self._keys.get(access_key)
        if hit and now - hit[0] < CACHE_S:
            return hit[1], hit[2]
        k = await asyncio.to_thread(self.store.get_s3_key, access_key) \
            if re.fullmatch(r"[A-Z0-9]{8,32}", access_key) else None
        secret = self.storage.secret(k) if k is not None else ""
        if len(self._keys) > 10000:
            self._keys.clear()
        self._keys[access_key] = (now, k, secret)
        return k, secret

    async def _box_live(self, box_id: str) -> bool:
        now = time.monotonic()
        hit = self._boxes.get(box_id)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
        box = await asyncio.to_thread(self.store.get, box_id)
        live = box is not None and box.state in LIVE_STATES and box.state != "releasing"
        self._boxes[box_id] = (now, live)
        return live

    async def _may_write(self, tenant: str, sub: str) -> Tuple[bool, str]:
        now = time.monotonic()
        hit = self._writes.get(tenant)
        if hit and now - hit[0] < WRITE_CACHE_S:
            return hit[1], hit[2]
        allow, reason = await asyncio.to_thread(self.storage.may_write, tenant, sub)
        self._writes[tenant] = (now, allow, reason or "")
        return allow, reason or ""

    async def _bucket(self, tenant: str, name: str) -> Optional[Bucket]:
        now = time.monotonic()
        hit = self._buckets.get((tenant, name))
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
        b = await asyncio.to_thread(self.store.find_bucket, tenant, name)
        if len(self._buckets) > 10000:
            self._buckets.clear()
        self._buckets[(tenant, name)] = (now, b)
        return b

    async def _bucket_by_id(self, bucket_id: str) -> Optional[Bucket]:
        now = time.monotonic()
        hit = self._by_id.get(bucket_id)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
        b = await asyncio.to_thread(self.store.get_bucket, bucket_id)
        if len(self._by_id) > 10000:
            self._by_id.clear()
        self._by_id[bucket_id] = (now, b)
        return b

    async def _bucket_origins(self) -> set:
        now = time.monotonic()
        if now - self._origins[0] >= CACHE_S:
            self._origins = (now, await asyncio.to_thread(self.store.cors_origins))
        return self._origins[1]

    async def _used(self, tenant: str) -> float:
        now = time.monotonic()
        hit = self._tenant_bytes.get(tenant)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
        used = await asyncio.to_thread(self.storage.tenant_bytes, tenant)
        self._tenant_bytes[tenant] = (now, used)
        return used

    # -- counters ---------------------------------------------------------
    def _count(self, bucket_id: str, egress: float = 0.0, ingress: float = 0.0,
               requests: int = 0) -> None:
        c = self._traffic.setdefault(bucket_id, [0.0, 0.0, 0])
        c[0] += egress
        c[1] += ingress
        c[2] += requests

    async def flush(self) -> None:
        used, self._key_seen = self._key_seen, {}
        for ak, at in used.items():
            try:
                await asyncio.to_thread(self.store.update_s3_key, ak, last_used_at=at)
            except Exception:
                log.exception("could not record use of key %s", ak)
        writes, self._box_writes = self._box_writes, {}
        deleted, self._deleted = self._deleted, {}
        try:
            if writes:
                await asyncio.to_thread(self.store.record_box_objects, list(writes.values()))
            by_bucket: Dict[str, List[str]] = {}
            for bid, k in deleted:
                by_bucket.setdefault(bid, []).append(k)
            for bid, keys in by_bucket.items():
                await asyncio.to_thread(self.store.forget_box_objects, bid, keys)
        except Exception:
            log.exception("could not record objects written by boxes")
        public, self._public = self._public, {}
        m = month(self.clock())
        for bid, n in public.items():
            try:
                await asyncio.to_thread(self.store.add_public_traffic, bid, n, m)
            except Exception:
                log.exception("could not record public downloads for bucket %s", bid)
                self._public[bid] = self._public.get(bid, 0.0) + n
        traffic, self._traffic = self._traffic, {}
        for bid, (e, i, r) in traffic.items():
            try:
                await asyncio.to_thread(self.store.add_bucket_traffic, bid, e, i, int(r))
            except Exception:
                log.exception("could not record traffic for bucket %s", bid)
                c = self._traffic.setdefault(bid, [0.0, 0.0, 0])
                c[0] += e
                c[1] += i
                c[2] += r

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_S)
            await self.flush()

    # -- ASGI -------------------------------------------------------------
    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    self._flusher = asyncio.create_task(self._flush_loop())
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    if self._flusher:
                        self._flusher.cancel()
                    await self.flush()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        rid = secrets.token_hex(8)
        # A request answered before its body was read must not leave that body on a kept-alive
        # connection: the next request would be read from the middle of it.
        hdrs = dict(scope.get("headers") or [])
        unread = {"open": bool(hdrs.get(b"transfer-encoding")) or
                  hdrs.get(b"content-length", b"0").strip(b"0") != b""}
        receive = _tracking(receive, unread)
        path = scope.get("path", "")
        origin = hdrs.get(b"origin", b"").decode("latin-1")
        # who may read the answer: the console always; a tenant's own web origins once the
        # bucket is known (handle() sets it)
        cors = {"origin": origin if origin and origin in self.st.cors_origins else None}
        if origin and scope["method"] == "OPTIONS" and hdrs.get(b"access-control-request-method"):
            if cors["origin"] or origin in await self._bucket_origins():
                return await self._preflight(send, origin, hdrs, rid)
        send = _with_cors(send, cors)
        try:
            await self.handle(scope, receive, send, rid, cors)
        except S3Error as e:
            await self._send_error(send, e.status, e.code, e.message, path, rid, unread["open"])
        except sigv4.SigError as e:
            if getattr(e, "canonical_request", None):
                log.info("request %s: signature mismatch; canonical request was %r", rid,
                         e.canonical_request)
            await self._send_error(send, e.status, e.code, e.message, path, rid, unread["open"])
        except Exception:
            log.exception("request %s failed", rid)
            await self._send_error(send, 500, "InternalError", "We encountered an internal error. "
                                   "Please try again.", path, rid, unread["open"])

    async def _preflight(self, send, origin: str, hdrs: Dict[bytes, bytes], rid: str,
                         methods: str = "GET, HEAD, PUT, POST, DELETE") -> None:
        """A browser asking whether a page may send this request. The answer allows nothing by
        itself: the request that follows must still be signed."""
        asked = hdrs.get(b"access-control-request-headers", b"").decode("latin-1")
        allow = ",".join(h for h in (x.strip().lower() for x in asked.split(","))
                         if re.fullmatch(r"[a-z0-9-]{1,64}", h))
        headers = [("access-control-allow-origin", origin),
                   ("access-control-allow-methods", methods),
                   ("access-control-max-age", "600"), ("vary", "Origin")]
        if allow:
            headers.append(("access-control-allow-headers", allow))
        await self._send(send, 204, b"", headers, rid)

    async def _send_error(self, send, status, code, message, resource, rid,
                          close: bool = False) -> None:
        body = error_body(code, message, resource, rid)
        headers = [(b"content-type", b"application/xml"),
                   (b"content-length", str(len(body)).encode()),
                   (b"x-amz-request-id", rid.encode())]
        if close:
            headers.append((b"connection", b"close"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _send(self, send, status: int, body: bytes = b"",
                    headers: Optional[List[Tuple[str, str]]] = None, rid: str = "") -> None:
        h = [(k.encode(), v.encode()) for k, v in (headers or [])]
        h += [(b"content-length", str(len(body)).encode()), (b"x-amz-request-id", rid.encode())]
        await send({"type": "http.response.start", "status": status, "headers": h})
        await send({"type": "http.response.body", "body": body})

    async def handle(self, scope, receive, send, rid: str, cors: Optional[dict] = None) -> None:
        method = scope["method"]
        raw_path = (scope.get("raw_path") or scope["path"].encode()).decode("latin-1")
        raw_query = scope.get("query_string", b"").decode("latin-1")
        headers: Dict[str, str] = {}
        for k, v in scope["headers"]:
            name = k.decode("latin-1").lower()
            val = v.decode("latin-1")
            headers[name] = f"{headers[name]},{val}" if name in headers else val

        if raw_path == "/.rixi/health" and method == "GET" and not raw_query:
            return await self._send(send, 200, b"ok", [("content-type", "text/plain")], rid)

        # where: virtual-hosted (<bucket>.<endpoint>) or path-style (/<bucket>/<key>)
        host = headers.get("host", "").split(":")[0].lower()
        if self.endpoint_host and host.endswith("." + self.endpoint_host):
            bucket_name = host[: -len(self.endpoint_host) - 1]
            key_raw = raw_path[1:]
            prefix_raw = ""
        else:
            rest = raw_path[1:]
            bucket_raw, sep, key_raw = rest.partition("/")
            bucket_name = unquote(bucket_raw)
            prefix_raw = bucket_raw
        has_key = key_raw != ""
        resource = raw_path

        signed = sigv4.parse(raw_query, headers)
        if signed is None:
            # even a public bucket is read with a key: there are no anonymous downloads
            raise S3Error(403, "AccessDenied", "Anonymous access is not allowed")
        key, secret = await self._key(signed.access_key)
        if key is None or key.revoked_at is not None:
            raise S3Error(403, "InvalidAccessKeyId",
                          "The access key you provided does not exist in our records.")
        if key.box_id and not await self._box_live(key.box_id):
            raise S3Error(403, "InvalidAccessKeyId", "This key belonged to a box that has ended.")
        signing_key = sigv4.verify(signed, secret, method, raw_path, headers, now=self.clock(),
                                   v2_resource=raw_path if prefix_raw else
                                   f"/{bucket_name}{raw_path}")
        self._key_seen[key.access_key] = self.clock()
        caller = Caller(sub=key.owner, tenant=key.tenant)
        q = dict(signed.pairs)
        for p in [p for p in q if p.startswith("X-Amz-") or
                  (signed.v2 and p in ("AWSAccessKeyId", "Signature", "Expires"))]:
            q.pop(p)

        if not bucket_name:
            if method != "GET" or q:
                raise S3Error(405, "MethodNotAllowed", "Only ListBuckets is allowed here")
            return await self._list_buckets(send, key, rid)
        if not valid_bucket_name(bucket_name):
            raise S3Error(404, "NoSuchBucket", "The specified bucket does not exist")
        op = classify(method, has_key, q)
        if op is None:
            raise S3Error(501, "NotImplemented",
                          "This operation is not offered (bucket settings are managed by rixi)")
        if op.write and not key.write:
            raise S3Error(403, "AccessDenied", "This key is read-only")

        if op.name == "CreateBucket":
            return await self._create_bucket(send, receive, key, caller, bucket_name, rid)

        bucket = await self._bucket(key.tenant, bucket_name)
        allowed = key.bucket_ids()
        if bucket is None and op.name in ("GetObject", "HeadObject") and allowed is None and \
                not set(q) - {"x-id"} and self.st.public_buckets and _BUCKET_ID.fullmatch(bucket_name):
            # another account's public bucket, named by its id: any key may read from it
            return await self._public_get(send, method, bucket_name, key_raw, headers, rid, cors)
        if bucket is None or bucket.state != "ready" or (allowed is not None and
                                                         bucket.id not in allowed):
            raise S3Error(404, "NoSuchBucket", "The specified bucket does not exist")

        origin = headers.get("origin", "")
        if cors is not None and origin and origin in bucket.cors_origins():
            cors["origin"] = origin
        if op.name == "DeleteBucket":
            return await self._delete_bucket(send, key, caller, bucket, rid)
        if op.name == "GetBucketLocation":
            body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<LocationConstraint xmlns="{XMLNS}">'
                    f"{bucket.region}</LocationConstraint>").encode()
            return await self._send(send, 200, body, [("content-type", "application/xml")], rid)
        if op.name == "GetBucketVersioning":
            body = f'<?xml version="1.0" encoding="UTF-8"?>\n<VersioningConfiguration ' \
                   f'xmlns="{XMLNS}"/>'.encode()
            return await self._send(send, 200, body, [("content-type", "application/xml")], rid)

        if op.adds_data or op.name in ("CreateMultipartUpload", "CompleteMultipartUpload"):
            # only adding data needs the account's say-so: reading and deleting always work,
            # so a tenant out of credit can still take their data out and clean up
            allow, reason = await self._may_write(key.tenant, key.owner)
            if not allow:
                raise S3Error(403, "AccessDenied", reason or "This account may not add data now")

        fwd = self._forward_headers(headers, op)
        copy_src = headers.get("x-amz-copy-source")
        if copy_src:
            if op.name not in ("PutObject", "UploadPart"):
                raise S3Error(400, "InvalidRequest", "x-amz-copy-source is not valid here")
            fwd["x-amz-copy-source"] = await self._copy_source(key, copy_src, bucket.region)

        mode = signed.payload_hash
        length = headers.get("content-length")
        if op.adds_data:
            declared = headers.get("x-amz-decoded-content-length") or length or "0"
            try:
                adding = float(declared)
            except ValueError:
                raise S3Error(400, "InvalidArgument", "bad content length")
            limit = self.storage.cfg.limits.max_storage_gb * 1e9
            if await self._used(key.tenant) + adding > limit:
                raise S3Error(403, "QuotaExceeded", f"Storage limit reached "
                              f"({self.storage.cfg.limits.max_storage_gb:g} GB)")

        body, upstream_hash, fwd_len = self._body(receive, method, mode, headers, signing_key,
                                                  signed, bucket.id)
        if fwd_len is not None:
            fwd["content-length"] = fwd_len
        if mode == sigv4.STREAMING_SIGNED:
            # forwarded decoded: it is no longer aws-chunked
            fwd.pop("x-amz-decoded-content-length", None)
            enc = [e.strip() for e in fwd.pop("content-encoding", "").split(",")
                   if e.strip() and e.strip() != "aws-chunked"]
            if enc:
                fwd["content-encoding"] = ",".join(enc)

        path = f"/{bucket.real_name}" + (f"/{key_raw}" if has_key else "")
        if not has_key and raw_path.endswith("/") and prefix_raw:
            path = f"/{bucket.real_name}/"
        auth_params = ("X-Amz-",) + (("AWSAccessKeyId=", "Signature=", "Expires=")
                                     if signed.v2 else ())
        upstream_query = "&".join(part for part in raw_query.split("&")
                                  if part and not part.startswith(auth_params))
        endpoint = self.storage.client.endpoint(bucket.region)
        up_host = urlparse(endpoint).netloc
        signed_headers = sigv4.sign(method, up_host, path, upstream_query, fwd,
                                    self.st.access_key, self.st.secret_key, bucket.region,
                                    payload_hash=upstream_hash)
        url = httpx.URL(endpoint, raw_path=(path + ("?" + upstream_query if upstream_query
                                                    else "")).encode("latin-1"))
        req = self.http.build_request(method, url, headers=signed_headers, content=body)
        try:
            resp = await self.http.send(req, stream=True)
        except _BodyError as e:
            raise S3Error(e.status, e.code, e.message)
        except httpx.HTTPError as exc:
            log.warning("upstream %s %s: %s", method, bucket.id, exc)
            raise S3Error(502, "ServiceUnavailable", "Storage is not reachable; try again")
        try:
            await self._relay(send, resp, op, bucket, endpoint, rid)
        finally:
            await resp.aclose()
            self._count(bucket.id, requests=1)
        if 200 <= resp.status_code < 300 and has_key:
            self._note(op, key, bucket, unquote(key_raw), headers, copy_src)

    async def _public_get(self, send, method: str, bucket_id: str, key_raw: str,
                          headers: Dict[str, str], rid: str, cors: Optional[dict]) -> None:
        """A signed download from another account's bucket that its tenant made public. The
        bucket's tenant pays for it, up to the monthly limit they set."""
        raw_path = f"/{bucket_id}/{key_raw}"
        b = await self._bucket_by_id(bucket_id)
        if b is None or b.state != "ready" or not b.public_read or b.public_blocked:
            raise S3Error(404, "NoSuchBucket", "The specified bucket does not exist")
        origin = headers.get("origin", "")
        if cors is not None and origin and origin in b.cors_origins():
            cors["origin"] = origin
        allow, _ = await self._may_write(b.tenant, b.owner)
        if not allow:
            raise S3Error(403, "AccessDenied", "This file is not available right now")
        cap = (b.public_cap_gb or self.st.public_cap_gb) * 1e9
        if b.public_bytes_in(month(self.clock())) + self._public.get(b.id, 0.0) >= cap:
            raise S3Error(403, "AccessDenied",
                          "This bucket has reached its download limit for this month")
        name = unquote(key_raw).rsplit("/", 1)[-1]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        ctype = INLINE_TYPES.get(ext)
        disposition = "inline" if ctype else "attachment; filename*=UTF-8''" + quote(name, safe="")
        fwd = {h: headers[h] for h in _PUBLIC_IN if h in headers}
        path = f"/{b.real_name}/{key_raw}"
        endpoint = self.storage.client.endpoint(b.region)
        signed_headers = sigv4.sign(method, urlparse(endpoint).netloc, path, "", fwd,
                                    self.st.access_key, self.st.secret_key, b.region,
                                    payload_hash=sigv4.EMPTY_SHA256)
        req = self.http.build_request(method, httpx.URL(endpoint, raw_path=path.encode("latin-1")),
                                      headers=signed_headers)
        try:
            resp = await self.http.send(req, stream=True)
        except httpx.HTTPError as exc:
            log.warning("upstream public %s %s: %s", method, b.id, exc)
            raise S3Error(502, "ServiceUnavailable", "Storage is not reachable; try again")
        try:
            out = [("cross-origin-resource-policy", "cross-origin"),
                   ("x-content-type-options", "nosniff"),
                   ("content-security-policy", "default-src 'none'; img-src 'self'; "
                    "media-src 'self'; style-src 'unsafe-inline'; sandbox"),
                   ("x-amz-request-id", rid)]
            st = resp.status_code
            if st >= 300 and st != 304:
                await resp.aread()
                code = _PUBLIC_CODES.get(st) or ("ServiceUnavailable" if st >= 500
                                                 else "AccessDenied")
                msg = {"NoSuchKey": "The specified key does not exist.",
                       "PreconditionFailed": "At least one of the preconditions failed.",
                       "InvalidRange": "The requested range is not satisfiable",
                       "ServiceUnavailable": "Storage is not reachable; try again"}.get(
                    code, "Access denied")
                body = error_body(code, msg, raw_path, rid) if method == "GET" else b""
                status = {"AccessDenied": 403, "ServiceUnavailable": 502}.get(code, st)
                await self._send(send, status, body,
                                 out[1:-1] + [("content-type", "application/xml")], rid)
                return
            out += [(k, v) for k, v in resp.headers.items() if k.lower() in _PUBLIC_OUT]
            if st != 304:
                out += [("content-type", ctype or "application/octet-stream"),
                        ("content-disposition", disposition)]
            await send({"type": "http.response.start", "status": st,
                        "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in out]})
            async for chunk in resp.aiter_raw():
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
                self._count(b.id, egress=len(chunk))
                self._public[b.id] = self._public.get(b.id, 0.0) + len(chunk)
            await send({"type": "http.response.body", "body": b""})
        finally:
            await resp.aclose()
            self._count(b.id, requests=1)

    def _note(self, op: Op, key: S3Key, bucket: Bucket, obj: str, headers: Dict[str, str],
              copied: Optional[str]) -> None:
        """Remember what a box wrote (and what anyone deleted), so a box's files can be listed."""
        where = (bucket.id, obj)
        if op.name == "DeleteObject":
            self._box_writes.pop(where, None)
            self._deleted[where] = None
        elif op.name in ("PutObject", "CompleteMultipartUpload") and key.box_id:
            size = None
            if op.name == "PutObject" and not copied:
                try:
                    size = float(headers.get("x-amz-decoded-content-length") or
                                 headers.get("content-length") or 0)
                except ValueError:
                    pass
            self._deleted.pop(where, None)
            self._box_writes[where] = BoxObject(box_id=key.box_id, tenant=key.tenant,
                                                bucket_id=bucket.id, key=obj, size=size,
                                                written_at=self.clock())

    # -- pieces -----------------------------------------------------------
    def _forward_headers(self, headers: Dict[str, str], op: Op) -> Dict[str, str]:
        acl = headers.get("x-amz-acl")
        if acl and acl not in ("private", "bucket-owner-full-control"):
            raise S3Error(403, "AccessDenied", "Objects are private; ACLs cannot be changed")
        if any(h.startswith("x-amz-grant-") for h in headers):
            raise S3Error(403, "AccessDenied", "Objects are private; ACLs cannot be changed")
        sc = headers.get("x-amz-storage-class")
        if sc and sc not in _STORAGE_CLASSES:
            raise S3Error(400, "InvalidStorageClass",
                          f"Storage classes offered: {', '.join(_STORAGE_CLASSES)}")
        out = {}
        for h, v in headers.items():
            if h in _DROP or h.startswith(_DROP_PREFIX) or h in _HOP:
                continue
            if h in _PASS or h.startswith("x-amz-"):
                out[h] = v
        return out

    async def _copy_source(self, key: S3Key, value: str, region: str) -> str:
        """`/bucket/key` (url-encoded, optional ?versionId=) → the same in the real bucket, if
        this key may read it and it is in the same region (S3 copies within one only)."""
        raw = value.lstrip("/")
        src_bucket_raw, _, rest = raw.partition("/")
        name = unquote(src_bucket_raw)
        b = await self._bucket(key.tenant, name) if valid_bucket_name(name) else None
        allowed = key.bucket_ids()
        if b is None or b.state != "ready" or not rest or (allowed is not None and
                                                           b.id not in allowed):
            raise S3Error(404, "NoSuchBucket", "The copy source bucket does not exist")
        if b.region != region:
            raise S3Error(400, "InvalidRequest", f"Copying between regions ({b.region} → "
                          f"{region}) is not supported; download and upload instead")
        return f"/{b.real_name}/{rest}"

    def _body(self, receive, method, mode, headers, signing_key, signed, bucket_id):
        """(body stream, payload hash to sign upstream, content-length to send)."""
        if method in ("GET", "HEAD", "DELETE") and not headers.get("content-length", "0") \
                .strip("0"):
            return None, sigv4.EMPTY_SHA256 if mode != sigv4.UNSIGNED else sigv4.UNSIGNED, None
        # Without a Content-Length (current SDKs send checksum-trailer uploads with chunked
        # transfer encoding) the body goes upstream chunked too; S3 judges it as it would.
        length = headers.get("content-length")
        if mode == sigv4.STREAMING_SIGNED_TRAILER:
            raise S3Error(501, "NotImplemented", "Signed trailers are not supported; use "
                                                 "unsigned payload trailers over HTTPS")

        async def raw():
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    raise _BodyError(400, "IncompleteBody", "the client went away")
                chunk = msg.get("body", b"")
                if chunk:
                    self._count(bucket_id, ingress=len(chunk))
                    yield chunk
                if not msg.get("more_body"):
                    return

        if mode == sigv4.STREAMING_SIGNED:
            try:
                decoded = int(headers.get("x-amz-decoded-content-length", ""))
            except ValueError:
                raise S3Error(411, "MissingContentLength",
                              "x-amz-decoded-content-length is required")
            dec = sigv4.ChunkDecoder(signing_key, signed.signature, signed.amz_date, signed.scope,
                                     decoded)

            async def plain():
                try:
                    async for chunk in raw():
                        for piece in dec.feed(chunk):
                            yield piece
                    dec.close()
                except sigv4.SigError as e:
                    raise _BodyError(e.status, e.code, e.message)
            return plain(), sigv4.UNSIGNED, str(decoded)

        if re.fullmatch(r"[0-9a-f]{64}", mode):
            async def checked():
                h, held = hashlib.sha256(), None
                async for chunk in raw():
                    h.update(chunk)
                    if held is not None:
                        yield held
                    held = chunk
                if h.hexdigest() != mode:
                    raise _BodyError(400, "XAmzContentSHA256Mismatch",
                                     "The provided 'x-amz-content-sha256' header does not "
                                     "match what was computed.")
                if held is not None:
                    yield held
            return checked(), mode, length

        # UNSIGNED-PAYLOAD, or an unsigned aws-chunked body with a checksum trailer: as is
        return raw(), mode, length

    async def _relay(self, send, resp: httpx.Response, op: Op, bucket: Bucket, endpoint: str,
                     rid: str) -> None:
        headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in _HOP
                   and k.lower() != "content-length"]
        headers.append(("x-amz-request-id", rid))
        compressed = resp.headers.get("content-encoding", "identity") not in ("identity", "")
        if (op.rewrite or resp.status_code >= 300) and not compressed:
            data = b""
            async for chunk in resp.aiter_raw():
                data += chunk
                if len(data) > MAX_REWRITE:
                    raise S3Error(502, "InternalError", "response too large")
            data = self._rewrite(data, bucket, endpoint) if data else data
            out = [(k.encode(), v.encode()) for k, v in headers]
            out.append((b"content-length", str(len(data)).encode()))
            await send({"type": "http.response.start", "status": resp.status_code,
                        "headers": out})
            await send({"type": "http.response.body", "body": data})
            self._count(bucket.id, egress=len(data))
            return
        length = resp.headers.get("content-length")
        out = [(k.encode(), v.encode()) for k, v in headers]
        if length is not None:
            out.append((b"content-length", length.encode()))
        await send({"type": "http.response.start", "status": resp.status_code, "headers": out})
        async for chunk in resp.aiter_raw():
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
            self._count(bucket.id, egress=len(chunk))
        await send({"type": "http.response.body", "body": b""})

    def _rewrite(self, data: bytes, b: Bucket, endpoint: str) -> bytes:
        """The tenant's bucket name instead of the real one, our endpoint instead of upstream."""
        real, name = b.real_name.encode(), b.name.encode()
        up = urlparse(endpoint)
        ours = self.st.endpoint_url.encode()
        data = data.replace(f"{up.scheme}://{up.netloc}/".encode() + real, ours + b"/" + name)
        data = data.replace(f"{up.scheme}://".encode() + real + b"." + up.netloc.encode(),
                            ours + b"/" + name)
        for tag in (b"Bucket", b"Name", b"BucketName"):
            data = data.replace(b"<" + tag + b">" + real + b"</" + tag + b">",
                                b"<" + tag + b">" + name + b"</" + tag + b">")
        data = data.replace(b"<Resource>/" + real, b"<Resource>/" + name)
        return data

    async def _list_buckets(self, send, key: S3Key, rid: str) -> None:
        rows = await asyncio.to_thread(self.store.list_buckets, key.tenant, True)
        allowed = key.bucket_ids()
        items = "".join(f"<Bucket><Name>{b.name}</Name><CreationDate>{_iso(b.created_at)}"
                        f"</CreationDate></Bucket>" for b in rows
                        if b.state == "ready" and (allowed is None or b.id in allowed))
        body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<ListAllMyBucketsResult xmlns="{XMLNS}">'
                f"<Owner><ID>{_xml(key.tenant)}</ID><DisplayName>{_xml(key.tenant)}</DisplayName>"
                f"</Owner><Buckets>{items}</Buckets></ListAllMyBucketsResult>").encode()
        await self._send(send, 200, body, [("content-type", "application/xml")], rid)

    async def _create_bucket(self, send, receive, key: S3Key, caller: Caller, name: str,
                             rid: str) -> None:
        if key.bucket_ids() is not None:
            raise S3Error(403, "AccessDenied", "This key is limited to some buckets")
        data = b""
        while True:
            msg = await receive()
            data += msg.get("body", b"")
            if len(data) > 65536:
                raise S3Error(400, "MalformedXML", "body too large")
            if msg["type"] == "http.disconnect" or not msg.get("more_body"):
                break
        region = None
        if data.strip():
            try:
                root = ET.fromstring(data)
                el = root.find(f"{{{XMLNS}}}LocationConstraint")
                if el is None:
                    el = root.find("LocationConstraint")
                region = (el.text or "").strip() if el is not None else None
            except ET.ParseError:
                raise S3Error(400, "MalformedXML", "The XML you provided was not well-formed")
            if region and region not in self.st.regions:
                raise S3Error(400, "InvalidLocationConstraint",
                              f"Regions offered: {', '.join(self.st.regions)}")
        try:
            b = await asyncio.to_thread(self.storage.create_bucket, caller, name, region or None)
        except Denied as d:
            code = {409: "BucketAlreadyOwnedByYou", 429: "TooManyBuckets", 400: "InvalidBucketName",
                    402: "AccessDenied"}.get(d.status, "AccessDenied")
            raise S3Error(409 if d.status == 409 else 400 if d.status in (400, 429) else 403,
                          code, d.reason)
        self._buckets.pop((key.tenant, name), None)
        await self._send(send, 200, b"", [("location", f"/{b.name}")], rid)

    async def _delete_bucket(self, send, key: S3Key, caller: Caller, b: Bucket, rid: str):
        if key.bucket_ids() is not None:
            raise S3Error(403, "AccessDenied", "This key is limited to some buckets")
        try:
            await asyncio.to_thread(self.storage.delete_bucket, caller, b.id, False)
        except NotFound:
            raise S3Error(404, "NoSuchBucket", "The specified bucket does not exist")
        except Denied as d:
            if d.status == 409:
                raise S3Error(409, "BucketNotEmpty", "The bucket you tried to delete is not empty")
            raise S3Error(503 if d.status >= 500 else 403, "AccessDenied", d.reason)
        self._buckets.pop((key.tenant, b.name), None)
        await self._send(send, 204, b"", [], rid)


_EXPOSE = b"ETag, Content-Length, Content-Range, Content-Type, Last-Modified, x-amz-request-id"


def _with_cors(send, cors: dict):
    """`send`, adding the CORS headers that let a page read the response, when `cors` names an
    origin by the time the response starts."""
    async def wrapped(msg):
        if msg["type"] == "http.response.start" and cors.get("origin"):
            msg = {**msg, "headers": list(msg.get("headers") or []) + [
                (b"access-control-allow-origin", cors["origin"].encode("latin-1")),
                (b"access-control-expose-headers", _EXPOSE), (b"vary", b"Origin")]}
        await send(msg)
    return wrapped


def _tracking(receive, unread: dict):
    """`receive`, noting when the request body has been read to its end."""
    async def wrapped():
        msg = await receive()
        if msg["type"] == "http.disconnect" or (msg["type"] == "http.request"
                                                and not msg.get("more_body")):
            unread["open"] = False
        return msg
    return wrapped


class _BodyError(Exception):
    """Raised inside a request body stream: the upstream request is abandoned unfinished."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def main() -> None:
    from . import http_authorizer, load
    from .store import Store

    ap = argparse.ArgumentParser(description="rixi S3 proxy (tenant buckets on one account)")
    ap.add_argument("--config", default=os.getenv("RIXI_GATEWAY_CONFIG", "rixi.toml"))
    ap.add_argument("--bind", default=os.getenv("RIXI_S3_BIND", "127.0.0.1:7102"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load(args.config)
    if cfg.storage is None:
        raise SystemExit("no [direct.storage] table in the config")
    authorize = http_authorizer(cfg.authorizer_url, cfg.authorizer_token) \
        if cfg.authorizer_url else None
    storage = StorageService(cfg, Store(cfg.store), authorize=authorize)
    host, _, port = args.bind.rpartition(":")
    import uvicorn
    uvicorn.run(Proxy(storage), host=host or "127.0.0.1", port=int(port), log_level="info",
                proxy_headers=False)


if __name__ == "__main__":
    main()
