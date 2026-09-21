# Scaleway provider + DNS against a fake HTTP session (no network, no spend).
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gateway.direct.providers import (  # noqa: E402
    BoxSpec, ProviderError, ScalewayDns, ScalewayProvider)


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.content = b"" if body is None else json.dumps(body).encode()
        self.headers = {"content-type": "application/json"} if body is not None else {}
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeSession:
    """Answers by (method, path-prefix); records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.headers = {}

    def request(self, method, url, timeout=None, **kw):
        path = url.replace("https://api.scaleway.com", "")
        self.calls.append((method, path, kw))
        for (m, prefix), answer in self.routes.items():
            if m == method and path.startswith(prefix):
                return answer(path, kw) if callable(answer) else answer
        return Resp(404, {"message": "not found"})


Z = "/instance/v1/zones/fr-par-2"


def _provider(routes):
    s = FakeSession(routes)
    return ScalewayProvider("secret", "proj-1", session=s, poll_interval=0), s


def test_create_orders_ip_then_dns_then_server_and_tags_everything():
    order = []
    routes = {
        ("GET", "/marketplace/v2/local-images"): Resp(200, {"local_images": [
            {"id": "img-other", "compatible_commercial_types": ["DEV1-S"]},
            {"id": "img-l4", "compatible_commercial_types": ["L4-1-24G"]}]}),
        ("GET", f"{Z}/security_groups"): Resp(200, {"security_groups": [
            {"id": "sg-1", "name": "rixi-direct"}]}),
        ("POST", f"{Z}/ips"): Resp(201, {"ip": {"id": "ip-1", "address": "51.0.0.7"}}),
        ("POST", f"{Z}/servers/"): Resp(202, {}),
        ("POST", f"{Z}/servers"): Resp(201, {"server": {"id": "srv-1"}}),
        ("PATCH", f"{Z}/servers/srv-1/user_data/cloud-init"): Resp(204),
    }
    p, s = _provider(routes)
    created = p.create(BoxSpec("abc", "t-1", "L4-1-24G", "fr-par-2", "ubuntu_noble_gpu_os_12",
                               "#cloud-config\n{}", root_volume_gb=100),
                       on_ip=lambda ip: order.append(("dns", ip)))
    assert created.provider_id == "srv-1" and created.ip == "51.0.0.7"
    methods = [(m, path) for m, path, _ in s.calls]
    assert methods.index(("POST", f"{Z}/ips")) < methods.index(("POST", f"{Z}/servers"))
    assert order == [("dns", "51.0.0.7")]
    server_body = next(kw["json"] for m, path, kw in s.calls
                       if m == "POST" and path == f"{Z}/servers")
    assert server_body["image"] == "img-l4" and server_body["security_group"] == "sg-1"
    assert set(server_body["tags"]) == {"rixi-managed", "rixi-box=abc", "rixi-tenant=t-1"}
    assert server_body["volumes"]["0"]["size"] == 100 * 10**9
    ip_body = next(kw["json"] for m, path, kw in s.calls if path == f"{Z}/ips")
    assert "rixi-box=abc" in ip_body["tags"]          # a leftover IP is findable too
    assert ("POST", f"{Z}/servers/srv-1/action") in methods


def test_security_group_created_once_with_https_in_and_smtp_out_blocked():
    routes = {
        ("GET", f"{Z}/security_groups"): Resp(200, {"security_groups": []}),
        ("POST", f"{Z}/security_groups/sg-9/rules"): Resp(201, {}),
        ("POST", f"{Z}/security_groups"): Resp(201, {"security_group": {"id": "sg-9"}}),
    }
    p, s = _provider(routes)
    assert p.security_group("fr-par-2") == "sg-9"
    assert p.security_group("fr-par-2") == "sg-9"       # cached
    sg = next(kw["json"] for m, path, kw in s.calls if path == f"{Z}/security_groups"
              and m == "POST")
    assert sg["inbound_default_policy"] == "drop"
    rules = [kw["json"] for m, path, kw in s.calls if path.endswith("/rules")]
    assert {(r["direction"], r["action"], r["dest_port_from"]) for r in rules} == {
        ("inbound", "accept", 443), ("inbound", "accept", 80), ("inbound", "accept", 22),
        ("outbound", "drop", 25), ("outbound", "drop", 465), ("outbound", "drop", 587)}


def test_no_compatible_image_is_an_error():
    routes = {("GET", "/marketplace/v2/local-images"): Resp(200, {"local_images": [
        {"id": "x", "compatible_commercial_types": ["DEV1-S"]}]})}
    p, _ = _provider(routes)
    with pytest.raises(ProviderError):
        p.resolve_image("ubuntu_noble", "fr-par-2", "H100-1-80G")


def test_destroy_terminates_waits_and_removes_volumes_and_ip():
    gone = {"n": 0}

    def server_get(path, kw):
        gone["n"] += 1
        return Resp(404) if gone["n"] > 1 else Resp(200, {"server": {"id": "srv-1"}})

    routes = {
        ("GET", f"{Z}/servers/srv-1"): server_get,
        ("GET", f"{Z}/servers"): Resp(200, {"servers": [{
            "id": "srv-1", "state": "running",
            "volumes": {"0": {"id": "vol-1", "volume_type": "sbs_volume"}}}]}),
        ("POST", f"{Z}/servers/srv-1/action"): Resp(202, {}),
        ("DELETE", "/block/v1alpha1/zones/fr-par-2/volumes/vol-1"): Resp(204),
        ("GET", f"{Z}/ips"): Resp(200, {"ips": [{"id": "ip-1"}]}),
        ("DELETE", f"{Z}/ips/ip-1"): Resp(204),
    }
    p, s = _provider(routes)
    p.destroy("abc", "fr-par-2")
    calls = [(m, path) for m, path, _ in s.calls]
    assert ("POST", f"{Z}/servers/srv-1/action") in calls
    assert ("DELETE", "/block/v1alpha1/zones/fr-par-2/volumes/vol-1") in calls
    assert ("DELETE", f"{Z}/ips/ip-1") in calls
    list_params = next(kw["params"] for m, path, kw in s.calls
                       if m == "GET" and path == f"{Z}/servers")
    assert list_params["tags"] == "rixi-box=abc"       # only this box, by tag


def test_list_managed_reports_orphan_ips():
    routes = {
        ("GET", f"{Z}/servers"): Resp(200, {"servers": [{
            "id": "srv-1", "tags": ["rixi-managed", "rixi-box=aaa"],
            "public_ips": [{"id": "ip-1"}]}]}),
        ("GET", f"{Z}/ips"): Resp(200, {"ips": [
            {"id": "ip-1", "tags": ["rixi-managed", "rixi-box=aaa"]},
            {"id": "ip-2", "tags": ["rixi-managed", "rixi-box=bbb"]}]}),
    }
    p, _ = _provider(routes)
    found = {(m.kind, m.box_id) for m in p.list_managed(["fr-par-2"])}
    assert found == {("server", "aaa"), ("ip", "bbb")}


def test_dns_names_are_relative_to_zone_and_stay_inside_it():
    s = FakeSession({("PATCH", "/domain/v2beta1/dns-zones/example.com/records"): Resp(200, {})})
    dns = ScalewayDns("secret", "example.com", session=s)
    dns.set_a("b-abc.run.example.com", "51.0.0.7")
    dns.delete_a("b-abc.run.example.com")
    set_change = s.calls[0][2]["json"]["changes"][0]["set"]
    assert set_change["id_fields"] == {"name": "b-abc.run", "type": "A"}
    assert set_change["records"][0]["data"] == "51.0.0.7"
    assert s.calls[1][2]["json"]["changes"][0]["delete"]["id_fields"]["name"] == "b-abc.run"
    with pytest.raises(ProviderError):
        dns.set_a("evil.example.org", "1.2.3.4")
