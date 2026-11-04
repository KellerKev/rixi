"""sigv4 against botocore, the reference implementation, in both directions."""
from __future__ import annotations

import hashlib
import hmac
import time

import pytest

botocore = pytest.importorskip("botocore")
from botocore.auth import S3SigV4Auth, S3SigV4QueryAuth  # noqa: E402
from botocore.awsrequest import AWSRequest  # noqa: E402
from botocore.credentials import Credentials  # noqa: E402

from gateway.direct import sigv4  # noqa: E402

AK, SK = "RXTESTKEY000000000AA", "test-secret/with+chars"
PATH = "/bucket/dir/a%20b%2Bc~%C3%A4.bin"
QUERY = "x-id=GetObject&prefix=a%2Fb&response-content-type=text%2Fplain"


def _parts(req: AWSRequest):
    from urllib.parse import urlsplit
    u = urlsplit(req.url)
    headers = {k.lower(): (v.decode() if isinstance(v, bytes) else v)
               for k, v in req.headers.items()}
    return u.path, u.query, headers


def test_a_request_botocore_signed_is_accepted_and_a_changed_one_is_not():
    req = AWSRequest(method="GET", url=f"https://s3.example.com{PATH}?{QUERY}",
                     headers={"x-amz-content-sha256": sigv4.UNSIGNED, "range": "bytes=0-9"})
    S3SigV4Auth(Credentials(AK, SK), "s3", "fr-par").add_auth(req)
    path, query, headers = _parts(req)
    headers["host"] = "s3.example.com"
    s = sigv4.parse(query, headers)
    assert s.access_key == AK and s.region == "fr-par"
    sigv4.verify(s, SK, "GET", path, headers)
    with pytest.raises(sigv4.SigError) as e:
        sigv4.verify(s, SK, "GET", path.replace("a%20b", "a%20c"), headers)
    assert e.value.code == "SignatureDoesNotMatch"
    with pytest.raises(sigv4.SigError):
        sigv4.verify(s, SK, "DELETE", path, headers)
    with pytest.raises(sigv4.SigError):
        sigv4.verify(s, "other", "GET", path, headers)


def test_unsigned_amz_headers_and_old_requests_are_refused():
    req = AWSRequest(method="GET", url="https://s3.example.com/b/k",
                     headers={"x-amz-content-sha256": sigv4.UNSIGNED})
    S3SigV4Auth(Credentials(AK, SK), "s3", "fr-par").add_auth(req)
    path, query, headers = _parts(req)
    headers["host"] = "s3.example.com"
    s = sigv4.parse(query, headers)
    with pytest.raises(sigv4.SigError, match="must be signed"):
        sigv4.verify(s, SK, "GET", path, {**headers, "x-amz-copy-source": "/other/k"})
    with pytest.raises(sigv4.SigError) as e:
        sigv4.verify(s, SK, "GET", path, headers, now=time.time() + 3600)
    assert e.value.code == "RequestTimeTooSkewed"


def test_presigned_urls_from_botocore_verify_until_they_expire():
    req = AWSRequest(method="GET", url=f"https://s3.example.com{PATH}")
    S3SigV4QueryAuth(Credentials(AK, SK), "s3", "fr-par", expires=300).add_auth(req)
    path, query, headers = _parts(req)
    headers = {"host": "s3.example.com"}
    s = sigv4.parse(query, headers)
    assert s.presigned
    sigv4.verify(s, SK, "GET", path, headers)
    with pytest.raises(sigv4.SigError, match="expired"):
        sigv4.verify(s, SK, "GET", path, headers, now=time.time() + 301)


def test_what_we_sign_is_what_botocore_would_sign():
    now = time.time()
    headers = {"content-type": "application/octet-stream", "x-amz-meta-run": "42",
               "x-amz-storage-class": "STANDARD"}
    ours = sigv4.sign("PUT", "s3.fr-par.scw.cloud", "/rixi-abc/dir/a%20b", "partNumber=1&"
                      "uploadId=xy%2Fz", headers, AK, SK, "fr-par", payload_hash=sigv4.UNSIGNED,
                      now=now)
    req = AWSRequest(method="PUT", url="https://s3.fr-par.scw.cloud/rixi-abc/dir/a%20b?"
                                       "partNumber=1&uploadId=xy%2Fz",
                     headers={k: v for k, v in ours.items() if k != "authorization"})
    req.context["timestamp"] = ours["x-amz-date"]
    auth = S3SigV4Auth(Credentials(AK, SK), "s3", "fr-par")
    creq = auth.canonical_request(req)
    expected = auth.signature(auth.string_to_sign(req, creq), req)
    assert ours["authorization"].endswith("Signature=" + expected)


def _chunked(body: bytes, size: int, key: bytes, seed: str, amz_date: str, scope: str):
    prev, wire = seed, b""
    for piece in [body[i:i + size] for i in range(0, len(body), size)] + [b""]:
        sts = "\n".join(["AWS4-HMAC-SHA256-PAYLOAD", amz_date, scope, prev, sigv4.EMPTY_SHA256,
                         hashlib.sha256(piece).hexdigest()])
        sig = hmac.new(key, sts.encode(), hashlib.sha256).hexdigest()
        wire += f"{len(piece):x};chunk-signature={sig}\r\n".encode() + piece + b"\r\n"
        prev = sig
    return wire


@pytest.mark.parametrize("feed", [1, 7, 1000, 10**6])
def test_signed_chunks_decode_however_the_bytes_arrive(feed):
    key = sigv4.signing_key(SK, "20261104", "fr-par")
    body = bytes(range(256)) * 300
    wire = _chunked(body, 8192, key, "a" * 64, "20261104T000000Z", "20261104/fr-par/s3/aws4_request")
    dec = sigv4.ChunkDecoder(key, "a" * 64, "20261104T000000Z", "20261104/fr-par/s3/aws4_request",
                             len(body))
    out = b"".join(b"".join(dec.feed(wire[i:i + feed])) for i in range(0, len(wire), feed))
    dec.close()
    assert out == body


def test_a_reordered_or_truncated_chunk_stream_is_refused():
    key = sigv4.signing_key(SK, "20261104", "fr-par")
    scope = "20261104/fr-par/s3/aws4_request"
    body = b"x" * 20000
    wire = _chunked(body, 8192, key, "b" * 64, "20261104T000000Z", scope)
    dec = sigv4.ChunkDecoder(key, "c" * 64, "20261104T000000Z", scope, len(body))
    with pytest.raises(sigv4.SigError):              # wrong seed: the chain does not hold
        dec.feed(wire)
    dec = sigv4.ChunkDecoder(key, "b" * 64, "20261104T000000Z", scope, len(body))
    dec.feed(wire[: len(wire) // 2])
    with pytest.raises(sigv4.SigError):
        dec.close()
    dec = sigv4.ChunkDecoder(key, "b" * 64, "20261104T000000Z", scope, len(body) - 1)
    with pytest.raises(sigv4.SigError):
        dec.feed(wire)
