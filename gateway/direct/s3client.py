"""The few S3 calls the control plane makes with the operator's key: create and remove buckets,
set their lifecycle, and measure what they hold. Path-style, signed with sigv4, on `requests`.
"""
from __future__ import annotations

import base64
import hashlib
import xml.etree.ElementTree as ET
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote, urlparse

import requests

from . import sigv4

NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class S3Error(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code


def _find(el, name: str) -> Optional[str]:
    x = el.find(NS + name)
    if x is None:
        x = el.find(name)
    return x.text if x is not None else None


def _all(el, name: str):
    return el.findall(NS + name) or el.findall(name)


class S3Client:
    def __init__(self, upstream: str, access_key: str, secret_key: str, timeout: float = 60.0):
        self.upstream = upstream               # e.g. https://s3.{region}.scw.cloud
        self.access_key = access_key
        self.secret_key = secret_key
        self.timeout = timeout
        self.http = requests.Session()

    def endpoint(self, region: str) -> str:
        return self.upstream.replace("{region}", region).rstrip("/")

    def host(self, region: str) -> str:
        return urlparse(self.endpoint(region)).netloc

    def call(self, method: str, region: str, bucket: str, key: str = "", query: str = "",
             body: bytes = b"", headers: Optional[Dict[str, str]] = None,
             ok: Tuple[int, ...] = (200, 204)) -> requests.Response:
        path = "/" + bucket + ("/" + quote(key, safe="/~") if key else "")
        h = dict(headers or {})
        if body:
            h["content-md5"] = base64.b64encode(hashlib.md5(body).digest()).decode()
        signed = sigv4.sign(method, self.host(region), path, query, h, self.access_key,
                            self.secret_key, region, payload_hash=hashlib.sha256(body).hexdigest())
        url = self.endpoint(region) + path + ("?" + query if query else "")
        r = self.http.request(method, url, data=body or None, headers=signed,
                              timeout=self.timeout)
        if r.status_code not in ok:
            code, msg = "Error", r.text[:300]
            try:
                root = ET.fromstring(r.content)
                code, msg = _find(root, "Code") or code, _find(root, "Message") or msg
            except ET.ParseError:
                pass
            raise S3Error(r.status_code, code, msg)
        return r

    # -- buckets ----------------------------------------------------------
    def create_bucket(self, region: str, bucket: str, tags: Dict[str, str]) -> None:
        self.call("PUT", region, bucket)
        tagset = "".join(f"<Tag><Key>{k}</Key><Value>{v}</Value></Tag>" for k, v in tags.items())
        self.call("PUT", region, bucket, query="tagging",
                  body=f"<Tagging><TagSet>{tagset}</TagSet></Tagging>".encode())

    def put_lifecycle(self, region: str, bucket: str, expire_days: Optional[int],
                      abort_multipart_days: int = 7) -> None:
        """Our rules only: unfinished uploads are aborted (they cost like stored data), and,
        when asked, objects expire."""
        rules = [f"<Rule><ID>rixi-abort-multipart</ID><Status>Enabled</Status>"
                 f"<Filter><Prefix></Prefix></Filter><AbortIncompleteMultipartUpload>"
                 f"<DaysAfterInitiation>{int(abort_multipart_days)}</DaysAfterInitiation>"
                 f"</AbortIncompleteMultipartUpload></Rule>"]
        if expire_days:
            rules.append(f"<Rule><ID>rixi-expire</ID><Status>Enabled</Status>"
                         f"<Filter><Prefix></Prefix></Filter>"
                         f"<Expiration><Days>{int(expire_days)}</Days></Expiration></Rule>")
        body = f"<LifecycleConfiguration>{''.join(rules)}</LifecycleConfiguration>".encode()
        self.call("PUT", region, bucket, query="lifecycle", body=body)

    def delete_bucket(self, region: str, bucket: str) -> None:
        self.call("DELETE", region, bucket, ok=(204, 200, 404))

    def list_buckets(self, region: str) -> List[str]:
        signed = sigv4.sign("GET", self.host(region), "/", "", {}, self.access_key,
                            self.secret_key, region, payload_hash=sigv4.EMPTY_SHA256)
        r = self.http.get(self.endpoint(region) + "/", headers=signed, timeout=self.timeout)
        if r.status_code != 200:
            raise S3Error(r.status_code, "ListBuckets", r.text[:200])
        root = ET.fromstring(r.content)
        return [_find(b, "Name") for b in root.iter(NS + "Bucket")]

    # -- contents ---------------------------------------------------------
    def objects(self, region: str, bucket: str, prefix: str = "") -> Iterator[Tuple[str, int, str]]:
        """(key, size, storage class) of every object (under `prefix`)."""
        token = None
        while True:
            q = "list-type=2&max-keys=1000" + (
                "&prefix=" + quote(prefix, safe="") if prefix else "") + (
                "&continuation-token=" + quote(token, safe="") if token else "")
            root = ET.fromstring(self.call("GET", region, bucket, query=q).content)
            for c in _all(root, "Contents"):
                yield _find(c, "Key"), int(_find(c, "Size") or 0), _find(c, "StorageClass") or \
                    "STANDARD"
            if (_find(root, "IsTruncated") or "false") != "true":
                return
            token = _find(root, "NextContinuationToken")

    def page(self, region: str, bucket: str, prefix: str = "", token: Optional[str] = None,
             max_keys: int = 200) -> dict:
        """One page of a folder view: {folders, objects, next_token}. Folders are the common
        prefixes under `prefix` (split at "/"); objects carry key, size, modified, class."""
        q = f"list-type=2&delimiter=%2F&max-keys={int(max_keys)}" + (
            "&prefix=" + quote(prefix, safe="") if prefix else "") + (
            "&continuation-token=" + quote(token, safe="") if token else "")
        root = ET.fromstring(self.call("GET", region, bucket, query=q).content)
        return {"folders": [_find(c, "Prefix") for c in _all(root, "CommonPrefixes")],
                "objects": [{"key": _find(c, "Key"), "size": int(_find(c, "Size") or 0),
                             "modified": _find(c, "LastModified"),
                             "storage_class": _find(c, "StorageClass") or "STANDARD"}
                            for c in _all(root, "Contents")],
                "next_token": _find(root, "NextContinuationToken")
                if (_find(root, "IsTruncated") or "false") == "true" else None}

    def delete_keys(self, region: str, bucket: str, keys: List[str]) -> None:
        """Delete up to 1000 objects in one request."""
        items = "".join(f"<Object><Key>{_xml(k)}</Key></Object>" for k in keys)
        self.call("POST", region, bucket, query="delete",
                  body=f"<Delete><Quiet>true</Quiet>{items}</Delete>".encode())

    def uploads(self, region: str, bucket: str) -> Iterator[Tuple[str, str]]:
        """(key, upload id) of every unfinished multipart upload."""
        km = um = None
        while True:
            q = "uploads" + (f"&key-marker={quote(km, safe='')}" if km else "") + \
                (f"&upload-id-marker={quote(um, safe='')}" if um else "")
            root = ET.fromstring(self.call("GET", region, bucket, query=q).content)
            for u in _all(root, "Upload"):
                yield _find(u, "Key"), _find(u, "UploadId")
            if (_find(root, "IsTruncated") or "false") != "true":
                return
            km, um = _find(root, "NextKeyMarker"), _find(root, "NextUploadIdMarker")

    def parts_size(self, region: str, bucket: str, key: str, upload_id: str) -> int:
        total, marker = 0, None
        while True:
            q = "uploadId=" + quote(upload_id, safe="") + (
                f"&part-number-marker={marker}" if marker else "")
            root = ET.fromstring(self.call("GET", region, bucket, key=key, query=q).content)
            total += sum(int(_find(p, "Size") or 0) for p in _all(root, "Part"))
            if (_find(root, "IsTruncated") or "false") != "true":
                return total
            marker = _find(root, "NextPartNumberMarker")

    def measure(self, region: str, bucket: str) -> Dict[str, float]:
        """Bytes held, by storage class (unfinished uploads count as STANDARD), and objects."""
        out = {"STANDARD": 0.0, "ONEZONE_IA": 0.0, "objects": 0}
        for _key, size, cls in self.objects(region, bucket):
            out["ONEZONE_IA" if cls == "ONEZONE_IA" else "STANDARD"] += size
            out["objects"] += 1
        for key, upload_id in self.uploads(region, bucket):
            out["STANDARD"] += self.parts_size(region, bucket, key, upload_id)
        return out

    def empty(self, region: str, bucket: str) -> int:
        """Delete everything in a bucket (objects and unfinished uploads); returns how many."""
        n = 0
        for key, upload_id in list(self.uploads(region, bucket)):
            self.call("DELETE", region, bucket, key=key,
                      query="uploadId=" + quote(upload_id, safe=""), ok=(204, 200, 404))
        while True:
            batch = [k for k, _s, _c in self._first_page(region, bucket)]
            if not batch:
                return n
            self.delete_keys(region, bucket, batch)
            n += len(batch)

    def _first_page(self, region: str, bucket: str):
        root = ET.fromstring(self.call("GET", region, bucket,
                                       query="list-type=2&max-keys=1000").content)
        return [(_find(c, "Key"), 0, "") for c in _all(root, "Contents")]


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
