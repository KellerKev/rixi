"""The S3 proxy, end to end: boto3 → proxy (TLS) → an in-memory S3 that checks the re-signing.

What must hold: a tenant's key reaches only that tenant's buckets (and only the ones it is
limited to), never learns the real bucket names, cannot make data public or touch bucket
settings, and every body form a client may send arrives intact — or not at all.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import os
import socket
import threading
import time
from urllib.parse import quote

import pytest

boto3 = pytest.importorskip("boto3")
import httpx  # noqa: E402
import uvicorn  # noqa: E402
from botocore.config import Config  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from gateway.direct import s3proxy, sigv4  # noqa: E402
from gateway.direct.config import parse  # noqa: E402
from gateway.direct.s3proxy import Proxy  # noqa: E402
from gateway.direct.service import Caller  # noqa: E402
from gateway.direct.storage import StorageService  # noqa: E402
from gateway.direct.store import Box, Store  # noqa: E402

from fake_s3 import FakeS3  # noqa: E402  (pytest puts this folder on sys.path)

OP_KEY, OP_SECRET = "SCWOPERATOR0000000", "operator-secret"
A = Caller(sub="alice", tenant="t-a")
B = Caller(sub="bob", tenant="t-b")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _cert(folder) -> tuple:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
            .sign(key, hashes.SHA256()))
    kp, cp = os.path.join(folder, "k.pem"), os.path.join(folder, "c.pem")
    with open(kp, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(cp, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return kp, cp


def _serve(app, port, **kw):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           lifespan="on", **kw))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.02)
    return server


class Env:
    pass


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    folder = tmp_path_factory.mktemp("s3")
    s3proxy_flush, s3proxy_cache = s3proxy.FLUSH_S, s3proxy.CACHE_S
    s3proxy.FLUSH_S, s3proxy.CACHE_S, s3proxy.WRITE_CACHE_S = 0.1, 0.0, 0.0
    up_port, px_port, plain_port = _free_port(), _free_port(), _free_port()
    cfg = parse({"direct": {
        "box_domain": "run.example.com", "heartbeat_url": "https://gw/hb",
        "box_jwks_url": "https://p/jwks", "rixi_ref": "v1.0.0", "jwt_audience": "rixi-gateway",
        "limits": {"max_buckets": 50, "max_storage_gb": 1, "max_s3_keys": 100},
        "storage": {"endpoint_url": f"https://localhost:{px_port}",
                    "upstream": f"http://127.0.0.1:{up_port}", "regions": ["fr-par", "nl-ams"],
                    "access_key": OP_KEY, "secret_key": OP_SECRET,
                    "key_secret": Fernet.generate_key().decode()}},
        "template": {"cpu": {"provider": "dummy", "type": "x", "zones": ["fr-par-1"]}}})
    e = Env()
    e.fake = FakeS3(OP_KEY, OP_SECRET)
    e.store = Store(f"sqlite:///{folder}/d.db")
    e.allow = {"t-a": True, "t-b": True}
    e.now = [time.time()]
    e.storage = StorageService(cfg, e.store,
                               authorize=lambda p: (e.allow.get(p["tenant"], False), "no credit"),
                               clock=lambda: e.now[0])
    e.proxy = Proxy(e.storage)
    kp, cp = _cert(str(folder))
    e.servers = [_serve(e.fake, up_port),
                 _serve(e.proxy, px_port, ssl_keyfile=kp, ssl_certfile=cp),
                 _serve(e.proxy, plain_port)]
    e.endpoint = f"https://localhost:{px_port}"
    e.plain = f"http://127.0.0.1:{plain_port}"
    e.cfg = cfg
    yield e
    for s in e.servers:
        s.should_exit = True
    s3proxy.FLUSH_S, s3proxy.CACHE_S = s3proxy_flush, s3proxy_cache


def client(env, key, secret, **cfg):
    return boto3.client("s3", endpoint_url=env.endpoint, region_name="fr-par", verify=False,
                        aws_access_key_id=key, aws_secret_access_key=secret,
                        config=Config(retries={"max_attempts": 1}, **cfg))


def key_for(env, caller, name="k", **kw):
    k, secret = env.storage.create_key(caller, name, **kw)
    return k.access_key, secret


def code(exc_info) -> str:
    return exc_info.value.response["Error"]["Code"]


# ── buckets and isolation ───────────────────────────────────────────────

def test_a_tenant_makes_and_lists_its_own_buckets_only(env):
    s3a = client(env, *key_for(env, A))
    s3b = client(env, *key_for(env, B))
    s3a.create_bucket(Bucket="iso-data")
    s3b.create_bucket(Bucket="iso-other")
    assert [b["Name"] for b in s3a.list_buckets()["Buckets"]] == ["iso-data"]
    assert [b["Name"] for b in s3b.list_buckets()["Buckets"]] == ["iso-other"]
    # names are per tenant: B may have its own "iso-data", A's stays A's
    s3b.create_bucket(Bucket="iso-data")
    s3a.put_object(Bucket="iso-data", Key="x", Body=b"alice")
    s3b.put_object(Bucket="iso-data", Key="x", Body=b"bob")
    assert s3a.get_object(Bucket="iso-data", Key="x")["Body"].read() == b"alice"
    assert s3b.get_object(Bucket="iso-data", Key="x")["Body"].read() == b"bob"
    with pytest.raises(ClientError) as e:
        s3b.get_object(Bucket="iso-other-nope", Key="x")
    assert code(e) == "NoSuchBucket"


def test_the_real_bucket_name_never_reaches_the_tenant(env):
    s3 = client(env, *key_for(env, A))
    s3.create_bucket(Bucket="naming")
    s3.put_object(Bucket="naming", Key="dir/a.txt", Body=b"1")
    real = env.store.find_bucket("t-a", "naming").real_name
    listing = s3.list_objects_v2(Bucket="naming")
    assert listing["Name"] == "naming" and listing["Contents"][0]["Key"] == "dir/a.txt"
    mpu = s3.create_multipart_upload(Bucket="naming", Key="big")
    assert mpu["Bucket"] == "naming"
    s3.abort_multipart_upload(Bucket="naming", Key="big", UploadId=mpu["UploadId"])
    with pytest.raises(ClientError) as e:
        s3.get_object(Bucket="naming", Key="missing")
    assert real not in str(e.value.response)
    assert s3.get_bucket_location(Bucket="naming")["LocationConstraint"] == "fr-par"


def test_a_limited_key_sees_only_its_buckets_and_a_read_only_key_cannot_write(env):
    s3 = client(env, *key_for(env, A))
    s3.create_bucket(Bucket="lim-one")
    s3.create_bucket(Bucket="lim-two")
    s3.put_object(Bucket="lim-two", Key="k", Body=b"secret")
    one = client(env, *key_for(env, A, buckets=["lim-one"]))
    assert [b["Name"] for b in one.list_buckets()["Buckets"]] == ["lim-one"]
    with pytest.raises(ClientError) as e:
        one.get_object(Bucket="lim-two", Key="k")
    assert code(e) == "NoSuchBucket"
    with pytest.raises(ClientError) as e:
        one.create_bucket(Bucket="lim-three")
    assert code(e) == "AccessDenied"
    ro = client(env, *key_for(env, A, write=False))
    assert ro.get_object(Bucket="lim-two", Key="k")["Body"].read() == b"secret"
    with pytest.raises(ClientError) as e:
        ro.put_object(Bucket="lim-two", Key="k", Body=b"overwrite")
    assert code(e) == "AccessDenied"


def test_nothing_can_be_made_public_and_bucket_settings_are_not_offered(env):
    s3 = client(env, *key_for(env, A))
    s3.create_bucket(Bucket="lockdown")
    with pytest.raises(ClientError) as e:
        s3.put_object(Bucket="lockdown", Key="k", Body=b"x", ACL="public-read")
    assert code(e) == "AccessDenied"
    with pytest.raises(ClientError) as e:
        s3.put_bucket_policy(Bucket="lockdown", Policy='{"Statement": []}')
    assert code(e) == "NotImplemented"
    with pytest.raises(ClientError) as e:
        s3.put_bucket_acl(Bucket="lockdown", ACL="public-read")
    assert code(e) == "NotImplemented"
    with pytest.raises(ClientError) as e:
        s3.put_object(Bucket="lockdown", Key="k", Body=b"x", StorageClass="GLACIER")
    assert code(e) == "InvalidStorageClass"
    s3.put_object(Bucket="lockdown", Key="k", Body=b"x", ACL="private",
                  StorageClass="ONEZONE_IA")


def test_unknown_revoked_and_wrong_secrets_are_refused(env):
    ak, secret = key_for(env, A)
    s3 = client(env, ak, secret)
    s3.list_buckets()
    with pytest.raises(ClientError) as e:
        client(env, ak, secret + "x").list_buckets()
    assert code(e) == "SignatureDoesNotMatch"
    with pytest.raises(ClientError) as e:
        client(env, "RXNOSUCHKEY0000000AA", secret).list_buckets()
    assert code(e) == "InvalidAccessKeyId"
    env.storage.revoke_key(A, ak)
    with pytest.raises(ClientError) as e:
        s3.list_buckets()
    assert code(e) == "InvalidAccessKeyId"
    r = httpx.get(env.endpoint + "/", verify=False)
    assert r.status_code == 403 and b"AccessDenied" in r.content


def test_a_box_key_dies_with_its_box(env):
    s3 = client(env, *key_for(env, A))
    s3.create_bucket(Bucket="boxdata")
    env.store.insert(Box(id="bx1", tenant="t-a", owner="alice", template="cpu", provider="dummy",
                         instance_type="x", zone="fr-par-1", eur_per_hour=0.1,
                         hostname="b-bx1.run.example.com", state="ready",
                         created_at=time.time()))
    k, secret = env.storage.box_key("t-a", "alice", "bx1", ["boxdata"], True)
    boxs3 = client(env, k.access_key, secret)
    boxs3.put_object(Bucket="boxdata", Key="result.csv", Body=b"1,2,3")
    env.store.update("bx1", state="gone")
    with pytest.raises(ClientError) as e:
        boxs3.get_object(Bucket="boxdata", Key="result.csv")
    assert code(e) == "InvalidAccessKeyId"
    env.storage.revoke_box_keys("bx1")
    assert env.store.get_s3_key(k.access_key).revoked_at is not None


# ── bodies ──────────────────────────────────────────────────────────────

def test_default_sdk_uploads_with_checksum_trailers_arrive_intact(env):
    s3 = client(env, *key_for(env, A))
    s3.create_bucket(Bucket="bodies")
    data = os.urandom(300_000)
    s3.put_object(Bucket="bodies", Key="trailer.bin", Body=data)
    real = env.store.find_bucket("t-a", "bodies").real_name
    stored = env.fake.buckets[real]["objects"]["trailer.bin"]
    assert stored["data"] == data
    assert stored["headers"].get("x-amz-content-sha256") == sigv4.STREAMING_UNSIGNED_TRAILER
    assert s3.get_object(Bucket="bodies", Key="trailer.bin")["Body"].read() == data
    got = s3.get_object(Bucket="bodies", Key="trailer.bin", Range="bytes=10-19")
    assert got["Body"].read() == data[10:20]


def test_multipart_uploads_and_unusual_keys(env):
    s3 = client(env, *key_for(env, A))
    s3.create_bucket(Bucket="multi")
    from boto3.s3.transfer import TransferConfig
    data = os.urandom(12 * 1024 * 1024)
    s3.upload_fileobj(io.BytesIO(data), "multi", "dir/a b+c~ä.bin",
                      Config=TransferConfig(multipart_threshold=5 * 1024 * 1024,
                                            multipart_chunksize=5 * 1024 * 1024))
    out = io.BytesIO()
    s3.download_fileobj("multi", "dir/a b+c~ä.bin", out)
    assert out.getvalue() == data
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="multi", Prefix="dir/")["Contents"]]
    assert keys == ["dir/a b+c~ä.bin"]


def _signed_put(env, ak, secret, bucket, key, body: bytes, sha: str, chunked=False,
                tamper=False):
    host = env.endpoint.split("//")[1]
    path = f"/{bucket}/{quote(key)}"
    amz_date = sigv4.amz_now()
    date = amz_date[:8]
    headers = {"host": host, "x-amz-date": amz_date}
    if chunked:
        headers.update({"x-amz-content-sha256": sigv4.STREAMING_SIGNED,
                        "content-encoding": "aws-chunked",
                        "x-amz-decoded-content-length": str(len(body))})
    else:
        headers["x-amz-content-sha256"] = sha
    signed = sorted(headers)
    creq = sigv4.canonical_request("PUT", path, [], headers, signed,
                                   headers["x-amz-content-sha256"])
    scope = f"{date}/fr-par/s3/aws4_request"
    skey = sigv4.signing_key(secret, date, "fr-par")
    import hmac
    sts = "\n".join([sigv4.ALGORITHM, amz_date, scope, hashlib.sha256(creq.encode()).hexdigest()])
    sig = hmac.new(skey, sts.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (f"{sigv4.ALGORITHM} Credential={ak}/{scope}, "
                                f"SignedHeaders={';'.join(signed)}, Signature={sig}")
    if chunked:
        prev, wire = sig, b""
        pieces = [body[i:i + 65536] for i in range(0, len(body), 65536)] + [b""]
        for n, piece in enumerate(pieces):
            csts = "\n".join(["AWS4-HMAC-SHA256-PAYLOAD", amz_date, scope, prev,
                              sigv4.EMPTY_SHA256, hashlib.sha256(piece).hexdigest()])
            csig = hmac.new(skey, csts.encode(), hashlib.sha256).hexdigest()
            sent = piece[:-1] + b"!" if tamper and n == 1 else piece
            wire += f"{len(piece):x};chunk-signature={csig}\r\n".encode() + sent + b"\r\n"
            prev = csig
        body = wire
    return httpx.put(env.endpoint + path, content=body, headers=headers, verify=False)


def test_signed_chunks_are_checked_one_by_one(env):
    ak, secret = key_for(env, A)
    client(env, ak, secret).create_bucket(Bucket="chunks")
    data = os.urandom(200_000)
    r = _signed_put(env, ak, secret, "chunks", "ok.bin", data, "", chunked=True)
    assert r.status_code == 200, r.text
    real = env.store.find_bucket("t-a", "chunks").real_name
    assert env.fake.buckets[real]["objects"]["ok.bin"]["data"] == data
    r = _signed_put(env, ak, secret, "chunks", "bad.bin", data, "", chunked=True, tamper=True)
    assert r.status_code in (400, 403) and "bad.bin" not in env.fake.buckets[real]["objects"]


def test_a_body_that_does_not_match_its_signed_hash_never_lands(env):
    ak, secret = key_for(env, A)
    client(env, ak, secret).create_bucket(Bucket="hashes")
    real = env.store.find_bucket("t-a", "hashes").real_name
    good = b"exactly this" * 1000
    r = _signed_put(env, ak, secret, "hashes", "ok", good, hashlib.sha256(good).hexdigest())
    assert r.status_code == 200, r.text
    r = _signed_put(env, ak, secret, "hashes", "swapped", b"something else entirely",
                    hashlib.sha256(good).hexdigest())
    assert r.status_code == 400 and b"XAmzContentSHA256Mismatch" in r.content
    assert "swapped" not in env.fake.buckets[real]["objects"]


def test_copy_stays_inside_the_tenant(env):
    s3a = client(env, *key_for(env, A))
    s3b = client(env, *key_for(env, B))
    s3a.create_bucket(Bucket="copy-src")
    s3a.create_bucket(Bucket="copy-dst")
    s3a.put_object(Bucket="copy-src", Key="f", Body=b"payload")
    s3a.copy_object(Bucket="copy-dst", Key="g", CopySource={"Bucket": "copy-src", "Key": "f"})
    assert s3a.get_object(Bucket="copy-dst", Key="g")["Body"].read() == b"payload"
    s3b.create_bucket(Bucket="copy-thief")
    with pytest.raises(ClientError) as e:
        s3b.copy_object(Bucket="copy-thief", Key="stolen",
                        CopySource={"Bucket": "copy-src", "Key": "f"})
    assert code(e) == "NoSuchBucket"


def test_presigned_urls_work_until_they_expire(env):
    s3 = client(env, *key_for(env, A))
    s3.create_bucket(Bucket="shared")
    s3.put_object(Bucket="shared", Key="report.pdf", Body=b"%PDF")
    url = s3.generate_presigned_url("get_object", Params={"Bucket": "shared",
                                                          "Key": "report.pdf"}, ExpiresIn=60)
    assert httpx.get(url, verify=False).content == b"%PDF"
    old = s3.generate_presigned_url("get_object", Params={"Bucket": "shared",
                                                          "Key": "report.pdf"}, ExpiresIn=1)
    time.sleep(2)
    r = httpx.get(old, verify=False)
    assert r.status_code == 403 and b"expired" in r.content
    # boto3's default for custom endpoints: Signature Version 2 query parameters
    v2 = client(env, *key_for(env, A), signature_version="s3")
    url = v2.generate_presigned_url("get_object", Params={"Bucket": "shared", "Key": "report.pdf",
                                                          "ResponseContentType": "application/pdf"},
                                    ExpiresIn=60)
    assert "AWSAccessKeyId=" in url
    r = httpx.get(url, verify=False)
    assert r.status_code == 200 and r.content == b"%PDF", r.text
    assert httpx.get(url.replace("report.pdf", "other.pdf"), verify=False).status_code == 403


# ── money ───────────────────────────────────────────────────────────────

def test_an_account_that_may_not_spend_can_read_but_not_write(env):
    s3 = client(env, *key_for(env, A))
    s3.create_bucket(Bucket="frozen")
    s3.put_object(Bucket="frozen", Key="k", Body=b"before")
    env.allow["t-a"] = False
    try:
        assert s3.get_object(Bucket="frozen", Key="k")["Body"].read() == b"before"
        with pytest.raises(ClientError) as e:
            s3.put_object(Bucket="frozen", Key="k2", Body=b"after")
        assert code(e) == "AccessDenied" and "no credit" in str(e.value)
        with pytest.raises(ClientError) as e:
            s3.create_multipart_upload(Bucket="frozen", Key="big")
        assert code(e) == "AccessDenied"
        s3.delete_object(Bucket="frozen", Key="k")              # cleaning up still works
    finally:
        env.allow["t-a"] = True


def test_the_storage_limit_refuses_uploads_beyond_it(env):
    s3 = client(env, *key_for(env, B))
    s3.create_bucket(Bucket="quota")
    b = env.store.find_bucket("t-b", "quota")
    env.store.update_bucket(b.id, bytes_std=0.9999e9)         # as measured
    try:
        s3.put_object(Bucket="quota", Key="small", Body=b"x" * 10)
        with pytest.raises(ClientError) as e:
            s3.put_object(Bucket="quota", Key="big", Body=b"x" * 200_000)
        assert code(e) == "QuotaExceeded"
    finally:
        env.store.update_bucket(b.id, bytes_std=0.0)


def test_downloads_are_counted_and_storage_is_measured_into_gb_hours(env):
    s3 = client(env, *key_for(env, A))
    s3.create_bucket(Bucket="metered")
    b = env.store.find_bucket("t-a", "metered")
    data = os.urandom(500_000)
    s3.put_object(Bucket="metered", Key="d", Body=data)
    for _ in range(3):
        s3.get_object(Bucket="metered", Key="d")["Body"].read()
    deadline = time.time() + 5
    while env.store.get_bucket(b.id).egress_bytes < 3 * len(data) and time.time() < deadline:
        time.sleep(0.05)
    got = env.store.get_bucket(b.id)
    assert got.egress_bytes >= 3 * len(data) and got.ingress_bytes >= len(data)
    # one measurement two hours after creation: the mean of 0 and 500 kB, for two hours
    env.now[0] = b.created_at + 7200
    env.storage.measure(env.store.get_bucket(b.id))
    m = env.store.get_bucket(b.id)
    assert m.bytes_std == len(data) and m.objects == 1
    assert m.gbh_std == pytest.approx(len(data) / 2 / 1e9 * 2, rel=1e-6)
    row = [u for u in env.storage.usage(A, 0) if u["bucket"] == b.id][0]
    assert row["egress_gb"] >= 3 * len(data) / 1e9 and row["gb_hours"] == pytest.approx(
        m.gbh_std, rel=1e-6)
    env.now[0] = time.time()


def test_deleting_a_bucket_needs_it_empty_unless_forced(env):
    s3 = client(env, *key_for(env, A))
    s3.create_bucket(Bucket="doomed")
    s3.put_object(Bucket="doomed", Key="k", Body=b"x")
    with pytest.raises(ClientError) as e:
        s3.delete_bucket(Bucket="doomed")
    assert code(e) == "BucketNotEmpty"
    b = env.store.find_bucket("t-a", "doomed")
    assert b.state == "ready"
    env.storage.delete_bucket(A, b.id, force=True)
    deadline = time.time() + 5
    while env.store.get_bucket(b.id).state != "deleted" and time.time() < deadline:
        time.sleep(0.05)
    assert env.store.get_bucket(b.id).state == "deleted"
    assert b.real_name not in env.fake.buckets
    with pytest.raises(ClientError) as e:
        s3.head_bucket(Bucket="doomed")
    assert e.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404


def test_new_buckets_get_lifecycle_rules_and_tags(env):
    b = env.storage.create_bucket(A, "lifecycle", expire_days=30)
    fake = env.fake.buckets[b.real_name]
    assert "rixi-abort-multipart" in fake["lifecycle"] and "<Days>30</Days>" in fake["lifecycle"]
    assert "t-a" in fake["tags"] and b.id in fake["tags"]
    env.storage.set_expiry(A, b.id, None)
    assert "rixi-expire" not in env.fake.buckets[b.real_name]["lifecycle"]


def test_a_refused_upload_does_not_garble_the_next_request_on_the_connection(env):
    ak, secret = key_for(env, A)
    s3 = boto3.client("s3", endpoint_url=env.plain, region_name="fr-par",
                      aws_access_key_id=ak, aws_secret_access_key=secret,
                      config=Config(retries={"max_attempts": 1}))
    s3.create_bucket(Bucket="keepalive")
    for _ in range(3):
        with pytest.raises(ClientError) as e:
            s3.put_object(Bucket="keepalive", Key="k", Body=b"x" * 5000, ACL="public-read")
        assert code(e) == "AccessDenied"
        with pytest.raises(ClientError) as e:
            s3.put_bucket_policy(Bucket="keepalive", Policy="{}")
        assert code(e) == "NotImplemented"
    s3.put_object(Bucket="keepalive", Key="k", Body=b"fine")
    assert s3.get_object(Bucket="keepalive", Key="k")["Body"].read() == b"fine"
