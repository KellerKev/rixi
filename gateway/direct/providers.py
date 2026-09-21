"""Compute providers for direct mode.

A provider creates one box, destroys it, and lists every box it manages. Everything is found by
cloud-side TAGS (`rixi-managed`, `rixi-box=<id>`, `rixi-tenant=<t>`), never by ids kept only in
memory, so a crash mid-create still leaves something the reconciler can find and remove.

`create()` calls `on_ip(ip)` after the public IP exists but before the box boots, so the caller can
publish DNS first — the box requests its TLS certificate on boot and needs its name to resolve.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

MANAGED_TAG = "rixi-managed"


def box_tags(box_id: str, tenant: str) -> List[str]:
    return [MANAGED_TAG, f"rixi-box={box_id}", f"rixi-tenant={tenant}"]


def box_id_from_tags(tags) -> Optional[str]:
    for t in tags or ():
        if t.startswith("rixi-box="):
            return t.split("=", 1)[1]
    return None


class ProviderError(Exception):
    pass


@dataclass
class BoxSpec:
    box_id: str
    tenant: str
    instance_type: str
    zone: str
    image: str
    user_data: str
    root_volume_gb: Optional[int] = None


@dataclass
class Created:
    provider_id: str
    ip: str


@dataclass
class Managed:
    """Something the provider holds for a box — a server, or a leftover IP with no server."""
    box_id: Optional[str]
    zone: str
    kind: str          # "server" | "ip"
    id: str


class DummyProvider:
    """In-memory provider for tests and local runs. Mirrors the tag semantics of a real cloud."""

    name = "dummy"

    def __init__(self, fail_create: bool = False):
        self.fail_create = fail_create
        self.servers: Dict[str, dict] = {}
        self._n = 0
        self._lock = threading.Lock()      # boxes are created from a worker pool

    def create(self, spec: BoxSpec, on_ip: Callable[[str], None]) -> Created:
        with self._lock:
            self._n += 1
            n = self._n
        ip = f"192.0.2.{n}"
        on_ip(ip)
        if self.fail_create:
            raise ProviderError("dummy: create failed")
        sid = f"srv-{n}"
        self.servers[sid] = {"zone": spec.zone, "tags": box_tags(spec.box_id, spec.tenant),
                             "user_data": spec.user_data, "type": spec.instance_type}
        return Created(provider_id=sid, ip=ip)

    def destroy(self, box_id: str, zone: str) -> None:
        for sid in [s for s, v in self.servers.items()
                    if box_id_from_tags(v["tags"]) == box_id and v["zone"] == zone]:
            del self.servers[sid]

    def list_managed(self, zones) -> List[Managed]:
        return [Managed(box_id_from_tags(v["tags"]), v["zone"], "server", sid)
                for sid, v in self.servers.items() if v["zone"] in zones]


class ScalewayProvider:
    """Scaleway Instances over the public API (no OpenTofu, no local state).

    Boxes get a routed IPv4, a zone-wide `rixi-direct` security group (80/443/22 in, outbound SMTP
    blocked), and cloud-init user-data. Use a dedicated Scaleway project with no project SSH keys:
    Scaleway injects every project key into new instances.
    """

    name = "scaleway"
    API = "https://api.scaleway.com"
    SG_NAME = "rixi-direct"

    def __init__(self, secret_key: str, project_id: str, session=None,
                 poll_interval: float = 3.0, poll_timeout: float = 300.0):
        if not secret_key or not project_id:
            raise ValueError("scaleway provider needs secret_key and project_id")
        import requests
        self.project_id = project_id
        self.s = session or requests.Session()
        self.s.headers.update({"X-Auth-Token": secret_key})
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self._sg: Dict[str, str] = {}

    # -- http -------------------------------------------------------------
    def _call(self, method: str, path: str, ok=(200, 201, 202, 204), **kw):
        r = self.s.request(method, self.API + path, timeout=30, **kw)
        if r.status_code not in ok:
            raise ProviderError(f"scaleway {method} {path} → {r.status_code}: {r.text[:300]}")
        return r.json() if r.content and r.headers.get("content-type", "").startswith(
            "application/json") else {}

    def _inst(self, zone: str) -> str:
        return f"/instance/v1/zones/{zone}"

    # -- lookups ----------------------------------------------------------
    def resolve_image(self, label: str, zone: str, instance_type: str) -> str:
        if len(label) == 36 and label.count("-") == 4:
            return label                                   # already an image id
        d = self._call("GET", "/marketplace/v2/local-images",
                       params={"image_label": label, "zone": zone, "type": "instance_sbs"})
        for img in d.get("local_images", []):
            if instance_type in img.get("compatible_commercial_types", []):
                return img["id"]
        raise ProviderError(f"no {label!r} image for {instance_type} in {zone}")

    def security_group(self, zone: str) -> str:
        if zone in self._sg:
            return self._sg[zone]
        d = self._call("GET", f"{self._inst(zone)}/security_groups",
                       params={"name": self.SG_NAME, "project": self.project_id})
        found = [g for g in d.get("security_groups", []) if g.get("name") == self.SG_NAME]
        if found:
            sg = found[0]["id"]
        else:
            sg = self._call("POST", f"{self._inst(zone)}/security_groups", json={
                "name": self.SG_NAME, "project": self.project_id, "stateful": True,
                "inbound_default_policy": "drop", "outbound_default_policy": "accept",
                "description": "rixi direct-mode boxes: https + acme + ssh in, no smtp out",
                "tags": [MANAGED_TAG]})["security_group"]["id"]
            rules = [("inbound", "accept", p) for p in (80, 443, 22)]
            rules += [("outbound", "drop", p) for p in (25, 465, 587)]
            for direction, action, port in rules:
                self._call("POST", f"{self._inst(zone)}/security_groups/{sg}/rules", json={
                    "protocol": "TCP", "direction": direction, "action": action,
                    "ip_range": "0.0.0.0/0", "dest_port_from": port})
        self._sg[zone] = sg
        return sg

    # -- lifecycle --------------------------------------------------------
    def create(self, spec: BoxSpec, on_ip: Callable[[str], None]) -> Created:
        z = self._inst(spec.zone)
        tags = box_tags(spec.box_id, spec.tenant)
        image = self.resolve_image(spec.image, spec.zone, spec.instance_type)
        sg = self.security_group(spec.zone)
        ip = self._call("POST", f"{z}/ips", json={
            "project": self.project_id, "type": "routed_ipv4", "tags": tags})["ip"]
        on_ip(ip["address"])
        body = {"name": f"rixi-{spec.box_id}", "commercial_type": spec.instance_type,
                "image": image, "project": self.project_id, "tags": tags,
                "public_ips": [ip["id"]], "security_group": sg, "dynamic_ip_required": False}
        if spec.root_volume_gb:
            body["volumes"] = {"0": {"size": int(spec.root_volume_gb) * 10**9,
                                     "volume_type": "sbs_volume"}}
        server = self._call("POST", f"{z}/servers", json=body)["server"]
        self._call("PATCH", f"{z}/servers/{server['id']}/user_data/cloud-init",
                   data=spec.user_data.encode(), headers={"Content-Type": "text/plain"})
        self._call("POST", f"{z}/servers/{server['id']}/action", json={"action": "poweron"})
        return Created(provider_id=server["id"], ip=ip["address"])

    def _servers(self, zone: str, tag: str) -> List[dict]:
        out, page = [], 1
        while True:
            d = self._call("GET", f"{self._inst(zone)}/servers",
                           params={"tags": tag, "per_page": 100, "page": page})
            batch = d.get("servers", [])
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def _ips(self, zone: str, tag: str) -> List[dict]:
        d = self._call("GET", f"{self._inst(zone)}/ips",
                       params={"tags": tag, "per_page": 100, "project": self.project_id})
        return d.get("ips", [])

    def destroy(self, box_id: str, zone: str) -> None:
        """Idempotent: removes whatever still exists for this box — server, block volumes, IP."""
        z = self._inst(zone)
        tag = f"rixi-box={box_id}"
        for srv in self._servers(zone, tag):
            sbs = [v["id"] for v in (srv.get("volumes") or {}).values()
                   if v.get("volume_type") == "sbs_volume"]
            if srv.get("state") in ("running", "stopped in place", "starting"):
                self._call("POST", f"{z}/servers/{srv['id']}/action", json={"action": "terminate"},
                           ok=(200, 201, 202, 204, 404))
            else:
                self._call("DELETE", f"{z}/servers/{srv['id']}", ok=(204, 404))
            self._wait_gone(zone, srv["id"])
            for vid in sbs:
                self._call("DELETE", f"/block/v1alpha1/zones/{zone}/volumes/{vid}",
                           ok=(204, 404))
        for ip in self._ips(zone, tag):
            self._call("DELETE", f"{z}/ips/{ip['id']}", ok=(204, 404))

    def _wait_gone(self, zone: str, server_id: str) -> None:
        deadline = time.monotonic() + self.poll_timeout
        while time.monotonic() < deadline:
            r = self.s.request("GET", f"{self.API}{self._inst(zone)}/servers/{server_id}",
                               timeout=30)
            if r.status_code == 404:
                return
            time.sleep(self.poll_interval)
        raise ProviderError(f"server {server_id} still exists after {self.poll_timeout:.0f}s")

    def list_managed(self, zones) -> List[Managed]:
        out: List[Managed] = []
        for zone in zones:
            servers = self._servers(zone, MANAGED_TAG)
            attached = set()
            for srv in servers:
                out.append(Managed(box_id_from_tags(srv.get("tags")), zone, "server", srv["id"]))
                attached.update(ip["id"] for ip in srv.get("public_ips") or [])
            for ip in self._ips(zone, MANAGED_TAG):
                if ip["id"] not in attached:
                    out.append(Managed(box_id_from_tags(ip.get("tags")), zone, "ip", ip["id"]))
        return out


# ── DNS ──────────────────────────────────────────────────────────────────

@dataclass
class NullDns:
    """No DNS management: the operator points a wildcard at nothing and boxes are reached by IP."""
    def set_a(self, fqdn: str, ip: str) -> None:
        return None

    def delete_a(self, fqdn: str) -> None:
        return None


@dataclass
class DummyDns:
    records: Dict[str, str] = field(default_factory=dict)

    def set_a(self, fqdn: str, ip: str) -> None:
        self.records[fqdn] = ip

    def delete_a(self, fqdn: str) -> None:
        self.records.pop(fqdn, None)


class ScalewayDns:
    """A records in a Scaleway Domains zone. `zone` is the DNS zone (e.g. example.com)."""

    API = "https://api.scaleway.com/domain/v2beta1"

    def __init__(self, secret_key: str, zone: str, ttl: int = 60, session=None):
        import requests
        self.zone = zone.strip(".")
        self.ttl = ttl
        self.s = session or requests.Session()
        self.s.headers.update({"X-Auth-Token": secret_key})

    def _name(self, fqdn: str) -> str:
        fqdn = fqdn.strip(".")
        if not fqdn.endswith("." + self.zone):
            raise ProviderError(f"{fqdn} is not inside DNS zone {self.zone}")
        return fqdn[: -len(self.zone) - 1]

    def _patch(self, change: dict) -> None:
        r = self.s.request("PATCH", f"{self.API}/dns-zones/{self.zone}/records", timeout=30,
                           json={"changes": [change], "return_all_records": False})
        if r.status_code not in (200, 204):
            raise ProviderError(f"scaleway dns → {r.status_code}: {r.text[:300]}")

    def set_a(self, fqdn: str, ip: str) -> None:
        name = self._name(fqdn)
        self._patch({"set": {"id_fields": {"name": name, "type": "A"},
                             "records": [{"name": name, "type": "A", "data": ip,
                                          "ttl": self.ttl}]}})

    def delete_a(self, fqdn: str) -> None:
        self._patch({"delete": {"id_fields": {"name": self._name(fqdn), "type": "A"}}})
