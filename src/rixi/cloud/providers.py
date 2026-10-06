"""Cloud providers for `rixi up`: create one box, destroy it, list what rixi manages.

Everything a box owns (server, firewall / security group, IP, SSH key) carries the labels
`managed-by=rixi` and `rixi-box=<name>`. Teardown and listing find resources by those labels, never
by ids held only in memory, so `destroy()` is idempotent and a crash mid-create still leaves
something `rixi down` can find and remove.

Providers raise `CapacityError` for a stock-out (the caller then tries the next region) and
`ProviderError` for everything else (bad credentials, quota, invalid input) — those fail fast.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

MANAGED_BY = "rixi"
_CAPACITY_RE = re.compile(
    r"resource_unavailable|placement_error|out of stock|no capacity|insufficient\s*capacity|"
    r"not available in|unavailable in this|stock|InsufficientInstanceCapacity", re.I)


class ProviderError(Exception):
    pass


class CapacityError(ProviderError):
    """The provider has no capacity for this type in this region — try another region."""


def labels_for(name: str) -> Dict[str, str]:
    return {"managed-by": MANAGED_BY, "rixi-box": name}


@dataclass
class BoxSpec:
    name: str                       # profile / box name (lowercase, hostname-safe)
    instance_type: str
    user_data: str
    gpu: bool = False
    ports: List[int] = field(default_factory=lambda: [9000, 22])
    allow_from: List[str] = field(default_factory=lambda: ["0.0.0.0/0"])
    ssh_public_key: Optional[str] = None


@dataclass
class BoxInfo:
    provider: str
    name: str
    box_id: str
    ip: str
    region: str
    instance_type: str


def _ipv6_too(cidrs: List[str]) -> List[str]:
    """Open-to-the-world rules should cover IPv6 as well; a pinned IPv4 /32 should not."""
    return cidrs + ["::/0"] if "0.0.0.0/0" in cidrs and "::/0" not in cidrs else cidrs


# ── Hetzner Cloud ────────────────────────────────────────────────────────────

class HetznerProvider:
    """Hetzner Cloud over its REST API: a server + a per-box firewall (+ an SSH key if given).

    Hetzner Cloud has no GPU instances; `presets.resolve` refuses `sample-gpu` for it.
    """

    name = "hetzner"
    API = "https://api.hetzner.cloud/v1"
    IMAGE = "ubuntu-24.04"

    def __init__(self, token: str, session=None, poll_interval: float = 3.0,
                 poll_timeout: float = 300.0):
        if not token:
            raise ProviderError("hetzner needs an API token (--hetzner-token or HCLOUD_TOKEN)")
        import requests
        self.s = session or requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}"})
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    def _call(self, method: str, path: str, ok=(200, 201, 202, 204), **kw) -> dict:
        r = self.s.request(method, self.API + path, timeout=30, **kw)
        if r.status_code in ok:
            return r.json() if r.content else {}
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        msg = f"hetzner {method} {path} → {r.status_code}: {err.get('code', '')} " \
              f"{err.get('message', r.text[:200])}"
        if _CAPACITY_RE.search(f"{err.get('code', '')} {err.get('message', '')}"):
            raise CapacityError(msg)
        raise ProviderError(msg)

    def _labeled(self, kind: str, name: Optional[str] = None) -> List[dict]:
        sel = f"managed-by={MANAGED_BY}" + (f",rixi-box={name}" if name else "")
        out, page = [], 1
        while True:
            d = self._call("GET", f"/{kind}", params={"label_selector": sel, "page": page,
                                                      "per_page": 50})
            out.extend(d.get(kind, []))
            if not (d.get("meta", {}).get("pagination", {}) or {}).get("next_page"):
                return out
            page += 1

    def create(self, spec: BoxSpec, region: str) -> BoxInfo:
        labels = labels_for(spec.name)
        rules = [{"direction": "in", "protocol": "tcp", "port": str(p),
                  "source_ips": _ipv6_too(list(spec.allow_from)), "description": "rixi up"}
                 for p in spec.ports]
        fw = self._call("POST", "/firewalls", json={
            "name": f"rixi-{spec.name}", "labels": labels, "rules": rules})["firewall"]
        body = {"name": f"rixi-{spec.name}", "server_type": spec.instance_type,
                "image": self.IMAGE, "location": region, "user_data": spec.user_data,
                "labels": labels, "firewalls": [{"firewall": fw["id"]}],
                "start_after_create": True}
        if spec.ssh_public_key:
            # Registering a key also stops Hetzner from e-mailing a root password.
            body["ssh_keys"] = [self._ssh_key(spec)]
        server = self._call("POST", "/servers", json=body)["server"]
        ip = (server.get("public_net", {}).get("ipv4") or {}).get("ip")
        if not ip:
            raise ProviderError("hetzner returned a server without a public IPv4")
        return BoxInfo(self.name, spec.name, str(server["id"]), ip, region, spec.instance_type)

    def _ssh_key(self, spec: BoxSpec) -> int:
        key_body = " ".join(spec.ssh_public_key.split()[:2])
        try:
            d = self._call("POST", "/ssh_keys", json={
                "name": f"rixi-{spec.name}", "public_key": spec.ssh_public_key,
                "labels": labels_for(spec.name)})
            return d["ssh_key"]["id"]
        except ProviderError as exc:
            if "uniqueness" not in str(exc):
                raise
        # The same public key is already registered (e.g. by hand): reuse it, don't delete it.
        page = 1
        while True:
            d = self._call("GET", "/ssh_keys", params={"page": page, "per_page": 50})
            for k in d.get("ssh_keys", []):
                if " ".join(k.get("public_key", "").split()[:2]) == key_body:
                    return k["id"]
            if not (d.get("meta", {}).get("pagination", {}) or {}).get("next_page"):
                raise ProviderError("hetzner: SSH key exists but could not be found")
            page += 1

    def destroy(self, name: str, region: Optional[str] = None) -> None:
        for srv in self._labeled("servers", name):
            self._call("DELETE", f"/servers/{srv['id']}", ok=(200, 202, 204, 404))
            self._wait_gone(f"/servers/{srv['id']}")
        deadline = time.monotonic() + self.poll_timeout
        for fw in self._labeled("firewalls", name):
            while True:      # a firewall stays "in use" for a moment after its server goes
                try:
                    self._call("DELETE", f"/firewalls/{fw['id']}", ok=(200, 204, 404))
                    break
                except ProviderError as exc:
                    if "resource_in_use" not in str(exc) or time.monotonic() > deadline:
                        raise
                    time.sleep(self.poll_interval)
        for key in self._labeled("ssh_keys", name):
            self._call("DELETE", f"/ssh_keys/{key['id']}", ok=(200, 204, 404))

    def _wait_gone(self, path: str) -> None:
        deadline = time.monotonic() + self.poll_timeout
        while time.monotonic() < deadline:
            r = self.s.request("GET", self.API + path, timeout=30)
            if r.status_code == 404:
                return
            time.sleep(self.poll_interval)
        raise ProviderError(f"hetzner: {path} still exists after {self.poll_timeout:.0f}s")

    def list_managed(self) -> List[BoxInfo]:
        out = []
        for srv in self._labeled("servers"):
            ip = (srv.get("public_net", {}).get("ipv4") or {}).get("ip", "")
            loc = (srv.get("datacenter", {}) or {}).get("location", {}).get("name", "")
            out.append(BoxInfo(self.name, srv.get("labels", {}).get("rixi-box", ""),
                               str(srv["id"]), ip, loc, srv.get("server_type", {}).get("name", "")))
        return out


# ── Scaleway ─────────────────────────────────────────────────────────────────

class ScalewayProvider:
    """Scaleway Instances over the public API: a server, a routed IPv4, a per-box security group.

    Adapted from gateway/direct/providers.py. Tip: use a Scaleway project without project-level
    SSH keys — Scaleway injects every project key into new instances.
    """

    name = "scaleway"
    API = "https://api.scaleway.com"
    ZONES = ("fr-par-1", "fr-par-2", "fr-par-3", "nl-ams-1", "nl-ams-2", "pl-waw-1")
    CPU_IMAGE = "ubuntu_noble"
    GPU_IMAGE = "ubuntu_jammy_gpu_os_12"

    def __init__(self, secret_key: str, project_id: Optional[str] = None,
                 access_key: Optional[str] = None, session=None, poll_interval: float = 3.0,
                 poll_timeout: float = 300.0):
        if not secret_key:
            raise ProviderError("scaleway needs a secret key (--scw-secret-key or SCW_SECRET_KEY)")
        import requests
        self.s = session or requests.Session()
        self.s.headers.update({"X-Auth-Token": secret_key})
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        if not project_id:
            if not access_key:
                raise ProviderError("scaleway needs a project id (--scw-project-id / "
                                    "SCW_DEFAULT_PROJECT_ID) or an access key to look it up")
            project_id = self._call("GET", f"/iam/v1alpha1/api-keys/{access_key}").get(
                "default_project_id")
            if not project_id:
                raise ProviderError("scaleway: could not resolve the API key's default project")
        self.project_id = project_id

    def _call(self, method: str, path: str, ok=(200, 201, 202, 204), **kw) -> dict:
        r = self.s.request(method, self.API + path, timeout=30, **kw)
        if r.status_code in ok:
            return r.json() if r.content and r.headers.get("content-type", "").startswith(
                "application/json") else {}
        msg = f"scaleway {method} {path} → {r.status_code}: {r.text[:300]}"
        if _CAPACITY_RE.search(r.text or ""):
            raise CapacityError(msg)
        raise ProviderError(msg)

    @staticmethod
    def _inst(zone: str) -> str:
        return f"/instance/v1/zones/{zone}"

    @staticmethod
    def _tags(name: str) -> List[str]:
        return [f"{k}={v}" for k, v in labels_for(name).items()]

    def _image(self, label: str, zone: str, itype: str):
        """Return (image_id, volume_type) for the first compatible image flavor."""
        for img_type, vol in (("instance_sbs", "sbs_volume"), ("instance_local", "l_ssd")):
            d = self._call("GET", "/marketplace/v2/local-images",
                           params={"image_label": label, "zone": zone, "type": img_type})
            for img in d.get("local_images", []):
                if itype in img.get("compatible_commercial_types", []):
                    return img["id"], vol
        raise ProviderError(f"scaleway: no {label!r} image for {itype} in {zone}")

    def _security_group(self, spec: BoxSpec, zone: str) -> str:
        z = self._inst(zone)
        sg = self._call("POST", f"{z}/security_groups", json={
            "name": f"rixi-{spec.name}", "project": self.project_id, "stateful": True,
            "inbound_default_policy": "drop", "outbound_default_policy": "accept",
            "description": "rixi up: rixi + ssh in, no smtp out",
            "tags": self._tags(spec.name)})["security_group"]["id"]
        rules = [("inbound", "accept", port, cidr)
                 for port in spec.ports for cidr in _ipv6_too(list(spec.allow_from))]
        rules += [("outbound", "drop", p, "0.0.0.0/0") for p in (25, 465, 587)]
        for direction, action, port, cidr in rules:
            self._call("POST", f"{z}/security_groups/{sg}/rules", json={
                "protocol": "TCP", "direction": direction, "action": action,
                "ip_range": cidr, "dest_port_from": port})
        return sg

    def create(self, spec: BoxSpec, region: str) -> BoxInfo:
        zone, z, tags = region, self._inst(region), self._tags(spec.name)
        image, vol_type = self._image(self.GPU_IMAGE if spec.gpu else self.CPU_IMAGE, zone,
                                      spec.instance_type)
        sg = self._security_group(spec, zone)
        ip = self._call("POST", f"{z}/ips", json={
            "project": self.project_id, "type": "routed_ipv4", "tags": tags})["ip"]
        body = {"name": f"rixi-{spec.name}", "commercial_type": spec.instance_type,
                "image": image, "project": self.project_id, "tags": tags,
                "public_ips": [ip["id"]], "security_group": sg, "dynamic_ip_required": False}
        if vol_type == "sbs_volume":
            size_gb = 100 if spec.gpu else 30
            body["volumes"] = {"0": {"size": size_gb * 10**9, "volume_type": "sbs_volume"}}
        server = self._call("POST", f"{z}/servers", json=body)["server"]
        self._call("PATCH", f"{z}/servers/{server['id']}/user_data/cloud-init",
                   data=spec.user_data.encode(), headers={"Content-Type": "text/plain"})
        self._call("POST", f"{z}/servers/{server['id']}/action", json={"action": "poweron"})
        return BoxInfo(self.name, spec.name, server["id"], ip["address"], zone,
                       spec.instance_type)

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

    def destroy(self, name: str, region: Optional[str] = None) -> None:
        """Idempotent: removes the server, its block volumes, its IP, and its security group."""
        zones = [region] if region else list(self.ZONES)
        tag = f"rixi-box={name}"
        for zone in zones:
            z = self._inst(zone)
            for srv in self._servers(zone, tag):
                sbs = [v["id"] for v in (srv.get("volumes") or {}).values()
                       if v.get("volume_type") == "sbs_volume"]
                if srv.get("state") in ("running", "stopped in place", "starting"):
                    self._call("POST", f"{z}/servers/{srv['id']}/action",
                               json={"action": "terminate"}, ok=(200, 201, 202, 204, 404))
                else:
                    self._call("DELETE", f"{z}/servers/{srv['id']}", ok=(204, 404))
                self._wait_gone(zone, srv["id"])
                for vid in sbs:
                    self._call("DELETE", f"/block/v1alpha1/zones/{zone}/volumes/{vid}",
                               ok=(204, 404))
            for ip in self._call("GET", f"{z}/ips", params={
                    "tags": tag, "project": self.project_id}).get("ips", []):
                self._call("DELETE", f"{z}/ips/{ip['id']}", ok=(204, 404))
            for sg in self._call("GET", f"{z}/security_groups", params={
                    "name": f"rixi-{name}", "project": self.project_id}).get(
                    "security_groups", []):
                if sg.get("name") == f"rixi-{name}":
                    self._call("DELETE", f"{z}/security_groups/{sg['id']}", ok=(204, 404))

    def _wait_gone(self, zone: str, server_id: str) -> None:
        deadline = time.monotonic() + self.poll_timeout
        while time.monotonic() < deadline:
            r = self.s.request("GET", f"{self.API}{self._inst(zone)}/servers/{server_id}",
                               timeout=30)
            if r.status_code == 404:
                return
            time.sleep(self.poll_interval)
        raise ProviderError(f"scaleway: server {server_id} still exists after "
                            f"{self.poll_timeout:.0f}s")

    def list_managed(self) -> List[BoxInfo]:
        out = []
        for zone in self.ZONES:
            for srv in self._servers(zone, f"managed-by={MANAGED_BY}"):
                name = next((t.split("=", 1)[1] for t in srv.get("tags", [])
                             if t.startswith("rixi-box=")), "")
                ips = srv.get("public_ips") or []
                out.append(BoxInfo(self.name, name, srv["id"],
                                   ips[0].get("address", "") if ips else "", zone,
                                   srv.get("commercial_type", "")))
        return out


# ── AWS (experimental) ───────────────────────────────────────────────────────

class AwsProvider:
    """EC2 via boto3 — EXPERIMENTAL: implemented against the API docs, not yet run against a live
    account. Uses the default VPC, a per-box security group, and Canonical's Ubuntu AMI (or the AWS
    Deep Learning base AMI with NVIDIA drivers for GPU types), looked up from public SSM parameters.
    Install with `pip install 'rixi[aws]'`.
    """

    name = "aws"
    _CPU_AMI = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
    _GPU_AMI = ("/aws/service/deeplearning/ami/x86_64/"
                "base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id")

    def __init__(self, access_key_id: Optional[str] = None,
                 secret_access_key: Optional[str] = None, session=None,
                 poll_interval: float = 5.0, poll_timeout: float = 600.0):
        if session is None:
            try:
                import boto3
            except ImportError as exc:
                raise ProviderError("the aws provider needs boto3: pip install 'rixi[aws]'") \
                    from exc
            session = boto3.Session(aws_access_key_id=access_key_id,
                                    aws_secret_access_key=secret_access_key)
        self.session = session
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    def _ec2(self, region: str):
        return self.session.client("ec2", region_name=region)

    @staticmethod
    def _tags(name: str) -> List[dict]:
        return [{"Key": k, "Value": v} for k, v in labels_for(name).items()] + [
            {"Key": "Name", "Value": f"rixi-{name}"}]

    def create(self, spec: BoxSpec, region: str) -> BoxInfo:
        ec2 = self._ec2(region)
        ssm = self.session.client("ssm", region_name=region)
        ami = ssm.get_parameter(Name=self._GPU_AMI if spec.gpu else self._CPU_AMI)[
            "Parameter"]["Value"]
        vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
        if not vpc:
            raise ProviderError(f"aws: no default VPC in {region}")
        sg = ec2.create_security_group(
            GroupName=f"rixi-{spec.name}", Description="rixi up", VpcId=vpc[0]["VpcId"],
            TagSpecifications=[{"ResourceType": "security-group",
                                "Tags": self._tags(spec.name)}])["GroupId"]
        ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": p, "ToPort": p,
             "IpRanges": [{"CidrIp": c} for c in spec.allow_from if ":" not in c],
             "Ipv6Ranges": [{"CidrIpv6": c} for c in _ipv6_too(list(spec.allow_from))
                            if ":" in c]}
            for p in spec.ports])
        try:
            inst = ec2.run_instances(
                ImageId=ami, InstanceType=spec.instance_type, MinCount=1, MaxCount=1,
                UserData=spec.user_data, SecurityGroupIds=[sg],
                BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {
                    "VolumeSize": 100 if spec.gpu else 30, "VolumeType": "gp3",
                    "DeleteOnTermination": True}}],
                TagSpecifications=[{"ResourceType": "instance", "Tags": self._tags(spec.name)}],
            )["Instances"][0]
        except Exception as exc:  # botocore ClientError
            if _CAPACITY_RE.search(str(exc)):
                raise CapacityError(f"aws: {exc}") from exc
            raise ProviderError(f"aws: {exc}") from exc
        iid = inst["InstanceId"]
        deadline = time.monotonic() + self.poll_timeout
        while time.monotonic() < deadline:
            d = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
            if d.get("PublicIpAddress"):
                return BoxInfo(self.name, spec.name, iid, d["PublicIpAddress"], region,
                               spec.instance_type)
            time.sleep(self.poll_interval)
        raise ProviderError(f"aws: instance {iid} got no public IP in time")

    def destroy(self, name: str, region: Optional[str] = None) -> None:
        if not region:
            raise ProviderError("aws destroy needs the region the box was created in")
        ec2 = self._ec2(region)
        filters = [{"Name": "tag:rixi-box", "Values": [name]},
                   {"Name": "tag:managed-by", "Values": [MANAGED_BY]}]
        ids = [i["InstanceId"] for r in ec2.describe_instances(Filters=filters + [
            {"Name": "instance-state-name",
             "Values": ["pending", "running", "stopping", "stopped"]}])["Reservations"]
               for i in r["Instances"]]
        if ids:
            ec2.terminate_instances(InstanceIds=ids)
            ec2.get_waiter("instance_terminated").wait(InstanceIds=ids)
        deadline = time.monotonic() + self.poll_timeout
        for sg in ec2.describe_security_groups(Filters=filters)["SecurityGroups"]:
            while True:      # a group stays referenced briefly after its instance terminates
                try:
                    ec2.delete_security_group(GroupId=sg["GroupId"])
                    break
                except Exception as exc:
                    if "DependencyViolation" not in str(exc) or time.monotonic() > deadline:
                        raise
                    time.sleep(self.poll_interval)

    def list_managed(self, regions: Optional[List[str]] = None) -> List[BoxInfo]:
        out = []
        for region in regions or ["eu-central-1"]:
            res = self._ec2(region).describe_instances(Filters=[
                {"Name": "tag:managed-by", "Values": [MANAGED_BY]},
                {"Name": "instance-state-name", "Values": ["pending", "running"]}])
            for r in res["Reservations"]:
                for i in r["Instances"]:
                    tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
                    out.append(BoxInfo(self.name, tags.get("rixi-box", ""), i["InstanceId"],
                                       i.get("PublicIpAddress", ""), region, i["InstanceType"]))
        return out


def make_provider(provider: str, creds: Dict[str, Optional[str]]):
    if provider == "hetzner":
        return HetznerProvider(creds.get("token") or "")
    if provider == "scaleway":
        return ScalewayProvider(creds.get("secret_key") or "", creds.get("project_id"),
                                creds.get("access_key"))
    if provider == "aws":
        return AwsProvider(creds.get("access_key_id"), creds.get("secret_access_key"))
    raise ProviderError(f"unknown provider {provider!r}")
