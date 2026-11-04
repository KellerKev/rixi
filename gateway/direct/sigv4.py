"""AWS Signature Version 4 for S3: checking what clients send, and signing what we forward.

Both directions use the same canonical request. S3 differs from other AWS services in one way
that matters here: the path is signed exactly as it was sent (already percent-encoded once), with
no normalisation, so the canonical URI is the raw request path.

Presigned URLs may also use the older Signature Version 2 query form (AWSAccessKeyId,
Signature, Expires) — boto3 still makes those for custom endpoints unless told otherwise — so
`parse` accepts it for presigned requests only, and `verify` checks it; it is never used upstream.

Uploads may arrive in three body forms (`x-amz-content-sha256`):
  * a hex SHA-256 of the body, or UNSIGNED-PAYLOAD — the body is the object;
  * STREAMING-AWS4-HMAC-SHA256-PAYLOAD — aws-chunked, every chunk signed in a chain seeded by
    the request signature (`ChunkDecoder` checks the chain and yields the plain bytes);
  * STREAMING-UNSIGNED-PAYLOAD-TRAILER — aws-chunked with a checksum trailer, which current AWS
    SDKs send by default over HTTPS; nothing in it is signed, so it can be forwarded as is.
"""
from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
UNSIGNED = "UNSIGNED-PAYLOAD"
STREAMING_SIGNED = "STREAMING-AWS4-HMAC-SHA256-PAYLOAD"
STREAMING_UNSIGNED_TRAILER = "STREAMING-UNSIGNED-PAYLOAD-TRAILER"
STREAMING_SIGNED_TRAILER = "STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER"
MAX_SKEW = 15 * 60
MAX_PRESIGN = 7 * 24 * 3600

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_AUTH = re.compile(r"^AWS4-HMAC-SHA256\s+Credential=([^,\s]+),\s*SignedHeaders=([^,\s]+),"
                   r"\s*Signature=([0-9a-f]{64})\s*$")
_SPACES = re.compile(r"\s+")


class SigError(Exception):
    """A request that is not (or no longer) correctly signed. `code` is the S3 error code."""

    def __init__(self, code: str, message: str, status: int = 403):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _q(s: str) -> str:
    return quote(s, safe="-_.~")


def query_pairs(raw_query: str) -> List[Tuple[str, str]]:
    """The query as decoded (name, value) pairs, in the order sent."""
    out = []
    for part in raw_query.split("&") if raw_query else []:
        if not part:
            continue
        k, _, v = part.partition("=")
        out.append((unquote(k), unquote(v)))
    return out


def canonical_query(pairs: List[Tuple[str, str]]) -> str:
    return "&".join(f"{k}={v}" for k, v in sorted((_q(k), _q(v)) for k, v in pairs))


def canonical_request(method: str, raw_path: str, pairs: List[Tuple[str, str]],
                      headers: Dict[str, str], signed: List[str], payload_hash: str) -> str:
    lines = "".join(f"{h}:{_SPACES.sub(' ', headers.get(h, '').strip())}\n" for h in signed)
    return "\n".join([method, raw_path or "/", canonical_query(pairs), lines, ";".join(signed),
                      payload_hash])


def signing_key(secret: str, date: str, region: str, service: str = "s3") -> bytes:
    k = hmac.new(("AWS4" + secret).encode(), date.encode(), hashlib.sha256).digest()
    for part in (region, service, "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    return k


def _hmac_hex(key: bytes, msg: str) -> str:
    return hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()


def _sha(data) -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def amz_now(now: Optional[float] = None) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))


def _parse_amz_date(v: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(v, "%Y%m%dT%H%M%SZ")))
    except (TypeError, ValueError):
        raise SigError("AccessDenied", "bad or missing X-Amz-Date")


# -- incoming -------------------------------------------------------------------------------

@dataclass
class Signed:
    """What a client's request claims: who signed it, how, and over what."""
    access_key: str
    date: str                       # yyyymmdd of the credential scope
    region: str
    amz_date: str
    signed_headers: List[str]
    signature: str
    payload_hash: str
    presigned: bool = False
    expires: Optional[int] = None
    pairs: List[Tuple[str, str]] = field(default_factory=list)   # query without X-Amz-Signature
    v2: bool = False                # a Signature Version 2 presigned URL (expires = unix time)

    @property
    def scope(self) -> str:
        return f"{self.date}/{self.region}/s3/aws4_request"


def _credential(cred: str) -> Tuple[str, str, str]:
    parts = cred.split("/")
    if len(parts) != 5 or parts[3] != "s3" or parts[4] != "aws4_request" or not parts[0]:
        raise SigError("AuthorizationHeaderMalformed", "the credential scope is not for s3", 400)
    return parts[0], parts[1], parts[2]


def parse(raw_query: str, headers: Dict[str, str]) -> Optional[Signed]:
    """The signature on a request (header or presigned URL); None when it carries none."""
    pairs = query_pairs(raw_query)
    q = dict(pairs)
    auth = headers.get("authorization", "")
    if auth:
        m = _AUTH.match(auth)
        if not m:
            raise SigError("AuthorizationHeaderMalformed", "only AWS4-HMAC-SHA256 is accepted", 400)
        ak, date, region = _credential(m.group(1))
        payload = headers.get("x-amz-content-sha256", "")
        if not payload:
            raise SigError("InvalidRequest", "x-amz-content-sha256 is required", 400)
        return Signed(access_key=ak, date=date, region=region,
                      amz_date=headers.get("x-amz-date", ""),
                      signed_headers=m.group(2).split(";"), signature=m.group(3),
                      payload_hash=payload, pairs=pairs)
    if "Signature" in q and "AWSAccessKeyId" in q and "Expires" in q:
        try:
            expires = int(q["Expires"])
        except ValueError:
            raise SigError("AccessDenied", "Expires must be a unix time")
        return Signed(access_key=q["AWSAccessKeyId"], date="", region="", amz_date="",
                      signed_headers=[], signature=q["Signature"], payload_hash=UNSIGNED,
                      presigned=True, expires=expires, pairs=pairs, v2=True)
    if "X-Amz-Signature" in q:
        if q.get("X-Amz-Algorithm") != ALGORITHM:
            raise SigError("AuthorizationQueryParametersError", "only AWS4-HMAC-SHA256", 400)
        ak, date, region = _credential(q.get("X-Amz-Credential", ""))
        try:
            expires = int(q.get("X-Amz-Expires", ""))
        except ValueError:
            raise SigError("AuthorizationQueryParametersError", "X-Amz-Expires is required", 400)
        return Signed(access_key=ak, date=date, region=region, amz_date=q.get("X-Amz-Date", ""),
                      signed_headers=q.get("X-Amz-SignedHeaders", "").split(";"),
                      signature=q["X-Amz-Signature"], payload_hash=UNSIGNED, presigned=True,
                      expires=expires, pairs=[p for p in pairs if p[0] != "X-Amz-Signature"])
    return None


# Query parameters that are part of what a Signature Version 2 request signs.
_V2_SUBRESOURCES = {
    "acl", "cors", "delete", "lifecycle", "location", "logging", "notification", "partNumber",
    "policy", "requestPayment", "restore", "tagging", "torrent", "uploadId", "uploads",
    "versionId", "versioning", "versions", "website", "response-content-type",
    "response-content-language", "response-expires", "response-cache-control",
    "response-content-disposition", "response-content-encoding", "select", "select-type",
    "object-lock", "storageClass", "attributes"}


def _verify_v2(s: Signed, secret: str, method: str, resource_path: str,
               headers: Dict[str, str], now: float) -> None:
    if now > s.expires:
        raise SigError("AccessDenied", "Request has expired")
    if s.expires > now + MAX_PRESIGN + MAX_SKEW:
        raise SigError("AccessDenied", "Expires is too far in the future")
    sub = sorted((k, v) for k, v in s.pairs if k in _V2_SUBRESOURCES)
    resource = resource_path + ("?" + "&".join(f"{k}={v}" if v else k for k, v in sub)
                                if sub else "")
    amz = "".join(f"{h}:{headers[h].strip()}\n" for h in sorted(headers)
                  if h.startswith("x-amz-"))
    sts = "\n".join([method, headers.get("content-md5", ""), headers.get("content-type", ""),
                     str(s.expires)]) + "\n" + amz + resource
    want = base64.b64encode(hmac.new(secret.encode(), sts.encode(), hashlib.sha1).digest()).decode()
    if not hmac.compare_digest(want, s.signature):
        err = SigError("SignatureDoesNotMatch", "The request signature we calculated does not "
                                                "match the signature you provided.")
        err.canonical_request = sts
        raise err


def verify(s: Signed, secret: str, method: str, raw_path: str, headers: Dict[str, str],
           now: Optional[float] = None, v2_resource: Optional[str] = None) -> bytes:
    """Check the signature and its time window; returns the signing key (for chunk chains).

    `v2_resource` is the /bucket/key path a Signature Version 2 URL signs (the request path, or
    the bucket plus the path for a virtual-hosted request)."""
    now = time.time() if now is None else now
    if s.v2:
        _verify_v2(s, secret, method, v2_resource or raw_path, headers, now)
        return b""
    t = _parse_amz_date(s.amz_date)
    if s.amz_date[:8] != s.date:
        raise SigError("AccessDenied", "X-Amz-Date does not match the credential scope")
    if s.presigned:
        if not 1 <= (s.expires or 0) <= MAX_PRESIGN:
            raise SigError("AuthorizationQueryParametersError", "X-Amz-Expires out of range", 400)
        if now > t + s.expires:
            raise SigError("AccessDenied", "Request has expired")
        if t > now + MAX_SKEW:
            raise SigError("RequestTimeTooSkewed", "the request is dated in the future")
    elif abs(now - t) > MAX_SKEW:
        raise SigError("RequestTimeTooSkewed", "the difference between the request time and "
                                               "the server's time is too large")
    if "host" not in s.signed_headers:
        raise SigError("AccessDenied", "the host header must be signed")
    unsigned_amz = [h for h in headers if h.startswith("x-amz-") and h not in s.signed_headers]
    if not s.presigned and unsigned_amz:
        raise SigError("AccessDenied", f"headers must be signed: {', '.join(sorted(unsigned_amz))}")
    payload = s.payload_hash
    if not (payload in (UNSIGNED, STREAMING_SIGNED, STREAMING_UNSIGNED_TRAILER,
                        STREAMING_SIGNED_TRAILER) or _HEX64.match(payload)):
        raise SigError("InvalidArgument", "x-amz-content-sha256 is not a known value", 400)
    creq = canonical_request(method, raw_path, s.pairs, headers, s.signed_headers, payload)
    sts = "\n".join([ALGORITHM, s.amz_date, s.scope, _sha(creq)])
    key = signing_key(secret, s.date, s.region)
    if not hmac.compare_digest(_hmac_hex(key, sts), s.signature):
        err = SigError("SignatureDoesNotMatch", "The request signature we calculated does not "
                                                "match the signature you provided.")
        err.canonical_request = creq        # what we signed over, for the operator's log
        raise err
    return key


class ChunkDecoder:
    """aws-chunked with signed chunks → the plain body, checking every chunk's signature.

    Feed it bytes as they arrive; it returns the payload decoded so far. `close()` raises if the
    stream ended early or carried a different length than was declared."""

    _HEAD = re.compile(rb"^([0-9a-fA-F]{1,16});chunk-signature=([0-9a-f]{64})$")
    MAX_CHUNK = 64 * 1024 * 1024

    def __init__(self, key: bytes, seed_signature: str, amz_date: str, scope: str,
                 decoded_length: Optional[int] = None):
        self.key = key
        self.prev = seed_signature
        self.amz_date = amz_date
        self.scope = scope
        self.expected = decoded_length
        self.buf = b""
        self.need: Optional[Tuple[int, str]] = None   # (size, signature) of the chunk being read
        self.done = False
        self.total = 0

    def _check(self, data: bytes, sig: str) -> None:
        sts = "\n".join(["AWS4-HMAC-SHA256-PAYLOAD", self.amz_date, self.scope, self.prev,
                         EMPTY_SHA256, hashlib.sha256(data).hexdigest()])
        if not hmac.compare_digest(_hmac_hex(self.key, sts), sig):
            raise SigError("SignatureDoesNotMatch", "a chunk signature does not match")
        self.prev = sig

    def feed(self, data: bytes) -> List[bytes]:
        if self.done:
            if data.strip(b"\r\n"):
                raise SigError("IncompleteBody", "data after the final chunk", 400)
            return []
        self.buf += data
        out = []
        while True:
            if self.need is None:
                i = self.buf.find(b"\r\n")
                if i < 0:
                    if len(self.buf) > 4096:
                        raise SigError("IncompleteBody", "a chunk header is too long", 400)
                    return out
                m = self._HEAD.match(self.buf[:i])
                if not m:
                    raise SigError("IncompleteBody", "not an aws-chunked body", 400)
                size = int(m.group(1), 16)
                if size > self.MAX_CHUNK:
                    raise SigError("IncompleteBody", "a chunk is too large", 400)
                self.need = (size, m.group(2).decode())
                self.buf = self.buf[i + 2:]
            size, sig = self.need
            if len(self.buf) < size + 2:
                return out
            chunk, tail = self.buf[:size], self.buf[size:size + 2]
            if tail != b"\r\n":
                raise SigError("IncompleteBody", "a chunk is not terminated", 400)
            self._check(chunk, sig)
            self.buf = self.buf[size + 2:]
            self.need = None
            if size == 0:
                self.done = True
                if self.buf.strip(b"\r\n"):
                    raise SigError("IncompleteBody", "data after the final chunk", 400)
                return out
            self.total += size
            if self.expected is not None and self.total > self.expected:
                raise SigError("IncompleteBody", "more data than x-amz-decoded-content-length",
                               400)
            out.append(chunk)

    def close(self) -> None:
        if not self.done:
            raise SigError("IncompleteBody", "the body ended before its final chunk", 400)
        if self.expected is not None and self.total != self.expected:
            raise SigError("IncompleteBody", "the body is not x-amz-decoded-content-length long",
                           400)


# -- outgoing -------------------------------------------------------------------------------

def sign(method: str, host: str, raw_path: str, raw_query: str, headers: Dict[str, str],
         access_key: str, secret: str, region: str, payload_hash: str = UNSIGNED,
         now: Optional[float] = None) -> Dict[str, str]:
    """Headers to send upstream: `headers` plus host, date, payload hash and Authorization.

    Signs host, every x-amz-* header and the content headers S3 checks, so nothing that
    changes what the request does can be altered after signing."""
    amz_date = amz_now(now)
    out = {k.lower(): v for k, v in headers.items()}
    out.update({"host": host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash})
    signed = sorted(h for h in out if h in ("host", "content-md5", "content-type")
                    or h.startswith("x-amz-"))
    creq = canonical_request(method, raw_path, query_pairs(raw_query), out, signed, payload_hash)
    date = amz_date[:8]
    scope = f"{date}/{region}/s3/aws4_request"
    sts = "\n".join([ALGORITHM, amz_date, scope, _sha(creq)])
    sig = _hmac_hex(signing_key(secret, date, region), sts)
    out["authorization"] = (f"{ALGORITHM} Credential={access_key}/{scope}, "
                            f"SignedHeaders={';'.join(signed)}, Signature={sig}")
    return out
