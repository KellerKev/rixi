"""An in-memory S3 that only answers requests signed with the operator's key.

Enough of S3 for the proxy tests: buckets with tagging and lifecycle, objects (with copy, range,
tagging), listings, multipart uploads, batch delete. Every request's sigv4 is checked with
gateway.direct.sigv4, so a test passing means the proxy really re-signed it — and an
aws-chunked body with a checksum trailer is decoded the way S3 would.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Dict
from urllib.parse import unquote

from gateway.direct import sigv4

NS = "http://s3.amazonaws.com/doc/2006-03-01/"


def _x(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unchunk(data: bytes) -> bytes:
    """aws-chunked with an unsigned trailer → the object bytes."""
    out, i = b"", 0
    while True:
        j = data.index(b"\r\n", i)
        size = int(data[i:j].split(b";")[0], 16)
        i = j + 2
        if size == 0:
            return out
        out += data[i:i + size]
        i += size + 2


class FakeS3:
    def __init__(self, access_key: str, secret_key: str):
        self.access_key = access_key
        self.secret_key = secret_key
        self.buckets: Dict[str, dict] = {}
        self.requests = []          # (method, path, query) as received

    def _err(self, status, code, msg=""):
        body = f"<Error><Code>{code}</Code><Message>{msg}</Message></Error>".encode()
        return status, {"content-type": "application/xml"}, body

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                m = await receive()
                if m["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        body = b""
        while True:
            m = await receive()
            if m["type"] == "http.disconnect":
                return              # an unfinished body is never stored, as in S3
            body += m.get("body", b"")
            if not m.get("more_body"):
                break
        headers = {}
        for k, v in scope["headers"]:
            headers[k.decode().lower()] = v.decode()
        raw_path = scope["raw_path"].decode()
        raw_query = scope["query_string"].decode()
        self.requests.append((scope["method"], raw_path, raw_query))
        try:
            signed = sigv4.parse(raw_query, headers)
            if signed is None or signed.access_key != self.access_key:
                status, h, out = self._err(403, "InvalidAccessKeyId")
            else:
                sigv4.verify(signed, self.secret_key, scope["method"], raw_path, headers)
                status, h, out = self.handle(scope["method"], raw_path, raw_query, headers, body)
        except sigv4.SigError as e:
            status, h, out = self._err(e.status, e.code, e.message)
        h = dict(h)
        h["content-length"] = str(len(out)) if scope["method"] != "HEAD" else \
            h.get("content-length", str(len(out)))
        await send({"type": "http.response.start", "status": status,
                    "headers": [(k.encode(), v.encode()) for k, v in h.items()]})
        await send({"type": "http.response.body", "body": out if scope["method"] != "HEAD"
                    else b""})

    def handle(self, method, raw_path, raw_query, headers, body):
        q = dict(sigv4.query_pairs(raw_query))
        name, _, key_raw = raw_path[1:].partition("/")
        key = unquote(key_raw)
        if headers.get("content-encoding", "").startswith("aws-chunked"):
            body = unchunk(body)
        if not name:
            items = "".join(f"<Bucket><Name>{n}</Name></Bucket>" for n in self.buckets)
            return 200, {}, f'<ListAllMyBucketsResult xmlns="{NS}"><Buckets>{items}</Buckets>' \
                            f"</ListAllMyBucketsResult>".encode()
        b = self.buckets.get(name)
        if not key:
            if method == "PUT" and not q:
                if b is not None:
                    return self._err(409, "BucketAlreadyOwnedByYou")
                self.buckets[name] = {"objects": {}, "uploads": {}, "tags": None,
                                      "lifecycle": None}
                return 200, {}, b""
            if b is None:
                return self._err(404, "NoSuchBucket", name)
            if method == "PUT" and "tagging" in q:
                b["tags"] = body.decode()
                return 200, {}, b""
            if method == "PUT" and "lifecycle" in q:
                b["lifecycle"] = body.decode()
                return 200, {}, b""
            if method == "GET" and "lifecycle" in q:
                return 200, {}, (b["lifecycle"] or "").encode()
            if method == "DELETE" and not q:
                if b["objects"] or b["uploads"]:
                    return self._err(409, "BucketNotEmpty")
                del self.buckets[name]
                return 204, {}, b""
            if method == "HEAD":
                return 200, {}, b""
            if method == "POST" and "delete" in q:
                keys = [unquote(k) for k in _between(body.decode(), "<Key>", "</Key>")]
                for k in keys:
                    b["objects"].pop(k.replace("&amp;", "&").replace("&lt;", "<"), None)
                return 200, {}, f'<DeleteResult xmlns="{NS}"></DeleteResult>'.encode()
            if method == "GET" and "uploads" in q:
                ups = "".join(f"<Upload><Key>{_x(u['key'])}</Key><UploadId>{uid}</UploadId>"
                              f"</Upload>" for uid, u in b["uploads"].items())
                return 200, {}, f'<ListMultipartUploadsResult xmlns="{NS}"><Bucket>{name}' \
                                f"</Bucket>{ups}<IsTruncated>false</IsTruncated>" \
                                f"</ListMultipartUploadsResult>".encode()
            if method == "GET":
                prefix = q.get("prefix", "")
                items = "".join(
                    f"<Contents><Key>{_x(k)}</Key><Size>{len(o['data'])}</Size>"
                    f"<ETag>\"{o['etag']}\"</ETag><StorageClass>{o['class']}</StorageClass>"
                    f"</Contents>" for k, o in sorted(b["objects"].items()) if k.startswith(prefix))
                return 200, {"content-type": "application/xml"}, \
                    f'<ListBucketResult xmlns="{NS}"><Name>{name}</Name><Prefix>{_x(prefix)}' \
                    f"</Prefix><IsTruncated>false</IsTruncated>{items}</ListBucketResult>".encode()
            return self._err(501, "NotImplemented")
        if b is None:
            return self._err(404, "NoSuchBucket", name)
        objs = b["objects"]
        if method == "POST" and "uploads" in q:
            uid = secrets.token_hex(8)
            b["uploads"][uid] = {"key": key, "parts": {}}
            return 200, {}, f'<InitiateMultipartUploadResult xmlns="{NS}"><Bucket>{name}</Bucket>' \
                            f"<Key>{_x(key)}</Key><UploadId>{uid}</UploadId>" \
                            f"</InitiateMultipartUploadResult>".encode()
        if method == "PUT" and "uploadId" in q:
            up = b["uploads"].get(q["uploadId"])
            if up is None:
                return self._err(404, "NoSuchUpload")
            up["parts"][int(q["partNumber"])] = body
            return 200, {"etag": f'"{hashlib.md5(body).hexdigest()}"'}, b""
        if method == "POST" and "uploadId" in q:
            up = b["uploads"].pop(q["uploadId"], None)
            if up is None:
                return self._err(404, "NoSuchUpload")
            data = b"".join(up["parts"][n] for n in sorted(up["parts"]))
            objs[key] = {"data": data, "etag": hashlib.md5(data).hexdigest(), "class": "STANDARD",
                         "meta": {}}
            host = headers["host"]
            return 200, {}, f'<CompleteMultipartUploadResult xmlns="{NS}"><Location>' \
                            f"http://{host}/{name}/{key_raw}</Location><Bucket>{name}</Bucket>" \
                            f"<Key>{_x(key)}</Key><ETag>\"x\"</ETag>" \
                            f"</CompleteMultipartUploadResult>".encode()
        if method == "DELETE" and "uploadId" in q:
            b["uploads"].pop(q["uploadId"], None)
            return 204, {}, b""
        if method == "GET" and "uploadId" in q:
            up = b["uploads"].get(q["uploadId"])
            parts = "".join(f"<Part><PartNumber>{n}</PartNumber><Size>{len(d)}</Size></Part>"
                            for n, d in sorted((up or {"parts": {}})["parts"].items()))
            return 200, {}, f'<ListPartsResult xmlns="{NS}"><Bucket>{name}</Bucket>{parts}' \
                            f"<IsTruncated>false</IsTruncated></ListPartsResult>".encode()
        if "tagging" in q:
            o = objs.get(key)
            if o is None:
                return self._err(404, "NoSuchKey")
            if method == "PUT":
                o["tags"] = body.decode()
                return 200, {}, b""
            return 200, {}, (o.get("tags") or "<Tagging><TagSet/></Tagging>").encode()
        if method == "PUT":
            src = headers.get("x-amz-copy-source")
            if src:
                sb, _, sk = src.lstrip("/").partition("/")
                so = self.buckets.get(sb, {}).get("objects", {}).get(unquote(sk.split("?")[0]))
                if so is None:
                    return self._err(404, "NoSuchKey")
                objs[key] = dict(so)
                return 200, {}, f"<CopyObjectResult><ETag>\"{so['etag']}\"</ETag>" \
                                f"</CopyObjectResult>".encode()
            meta = {k: v for k, v in headers.items() if k.startswith("x-amz-meta-")}
            objs[key] = {"data": body, "etag": hashlib.md5(body).hexdigest(),
                         "class": headers.get("x-amz-storage-class", "STANDARD"), "meta": meta,
                         "headers": dict(headers), "at": time.time()}
            return 200, {"etag": f"\"{objs[key]['etag']}\""}, b""
        o = objs.get(key)
        if method == "DELETE":
            objs.pop(key, None)
            return 204, {}, b""
        if o is None:
            return self._err(404, "NoSuchKey", key)
        h = {"etag": f"\"{o['etag']}\"", "content-type": "application/octet-stream",
             **o["meta"]}
        data = o["data"]
        if method == "HEAD":
            h["content-length"] = str(len(data))
            return 200, h, b""
        rng = headers.get("range")
        if rng and rng.startswith("bytes="):
            a, _, z = rng[6:].partition("-")
            start = int(a) if a else max(0, len(data) - int(z))
            end = int(z) if a and z else len(data) - 1
            part = data[start:end + 1]
            h["content-range"] = f"bytes {start}-{start + len(part) - 1}/{len(data)}"
            return 206, h, part
        return 200, h, data


def _between(s: str, a: str, z: str):
    out, i = [], 0
    while True:
        i = s.find(a, i)
        if i < 0:
            return out
        j = s.index(z, i)
        out.append(s[i + len(a):j])
        i = j
