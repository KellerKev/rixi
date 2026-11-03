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
        ("GET", f"{Z}/security_groups"): Resp(200, {"security_groups": [
            {"id": "sg-box", "tags": ["rixi-managed", "rixi-box=abc"]}]}),
        ("DELETE", f"{Z}/security_groups/sg-box"): Resp(204),
    }
    p, s = _provider(routes)
    p.destroy("abc", "fr-par-2")
    calls = [(m, path) for m, path, _ in s.calls]
    assert ("POST", f"{Z}/servers/srv-1/action") in calls
    assert ("DELETE", "/block/v1alpha1/zones/fr-par-2/volumes/vol-1") in calls
    assert ("DELETE", f"{Z}/ips/ip-1") in calls
    assert calls.index(("DELETE", f"{Z}/security_groups/sg-box")) > \
        calls.index(("POST", f"{Z}/servers/srv-1/action"))   # the group goes after the server
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
        ("GET", f"{Z}/security_groups"): Resp(200, {"security_groups": []}),
    }
    p, _ = _provider(routes)
    found = {(m.kind, m.box_id) for m in p.list_managed(["fr-par-2"])}
    assert found == {("server", "aaa"), ("ip", "bbb")}


def test_list_managed_reports_firewalls_no_server_uses():
    routes = {
        ("GET", f"{Z}/servers"): Resp(200, {"servers": [{
            "id": "srv-1", "tags": ["rixi-managed", "rixi-box=aaa"],
            "security_group": {"id": "sg-aaa"}}]}),
        ("GET", f"{Z}/ips"): Resp(200, {"ips": []}),
        ("GET", f"{Z}/security_groups"): Resp(200, {"security_groups": [
            {"id": "sg-shared", "tags": ["rixi-managed"]},          # the zone-wide group
            {"id": "sg-aaa", "tags": ["rixi-managed", "rixi-box=aaa"]},
            {"id": "sg-ccc", "tags": ["rixi-managed", "rixi-box=ccc"]}]}),
    }
    p, _ = _provider(routes)
    found = {(m.kind, m.box_id, m.id) for m in p.list_managed(["fr-par-2"])}
    assert found == {("server", "aaa", "srv-1"), ("firewall", "ccc", "sg-ccc")}


def _created_with_firewall():
    routes = {
        ("GET", "/marketplace/v2/local-images"): Resp(200, {"local_images": [
            {"id": "img-1", "compatible_commercial_types": ["DEV1-S"]}]}),
        ("PUT", f"{Z}/security_groups/sg-box/rules"): Resp(200, {"rules": []}),
        ("POST", f"{Z}/security_groups"): Resp(201, {"security_group": {"id": "sg-box"}}),
        ("POST", f"{Z}/ips"): Resp(201, {"ip": {"id": "ip-1", "address": "51.0.0.7"}}),
        ("POST", f"{Z}/servers/"): Resp(202, {}),
        ("POST", f"{Z}/servers"): Resp(201, {"server": {"id": "srv-1"}}),
        ("PATCH", f"{Z}/servers/srv-1/user_data/cloud-init"): Resp(204),
    }
    p, s = _provider(routes)
    created = p.create(BoxSpec("abc", "t-1", "DEV1-S", "fr-par-2", "ubuntu_noble", "#cc\n{}",
                               dedicated_firewall=True), on_ip=lambda ip: None)
    return created, s


def test_a_policed_box_gets_its_own_bootstrap_permissive_group():
    created, s = _created_with_firewall()
    assert created.firewall_id == "sg-box"
    sg = next(kw["json"] for m, path, kw in s.calls
              if m == "POST" and path == f"{Z}/security_groups")
    assert sg["name"] == "rixi-box-abc" and "rixi-box=abc" in sg["tags"]
    assert sg["outbound_default_policy"] == "accept"           # bootstrap needs the internet
    server = next(kw["json"] for m, path, kw in s.calls if m == "POST" and path == f"{Z}/servers")
    assert server["security_group"] == "sg-box"
    rules = next(kw["json"]["rules"] for m, path, kw in s.calls if m == "PUT")
    assert {(r["direction"], r["dest_port_from"]) for r in rules} == {
        ("inbound", 80), ("inbound", 443), ("inbound", 22),
        ("outbound", 25), ("outbound", 465), ("outbound", 587)}


def test_apply_firewall_replaces_rules_then_sets_defaults():
    from rixi.policy import compile_rules, parse
    routes = {
        ("GET", f"{Z}/security_groups"): Resp(200, {"security_groups": [
            {"id": "sg-box", "tags": ["rixi-managed", "rixi-box=abc"]}]}),
        ("PUT", f"{Z}/security_groups/sg-box/rules"): Resp(200, {"rules": []}),
        ("PATCH", f"{Z}/security_groups/sg-box"): Resp(200, {}),
    }
    p, s = _provider(routes)
    compiled = compile_rules(parse({"egress": {"mode": "allowlist",
                                               "allow": ["203.0.113.0/24", "198.51.100.7:8000-9000"]}}),
                             own_ip="51.0.0.7", control_plane=["65.21.252.62"],
                             resolvers=["51.159.47.28"])
    p.apply_firewall("abc", "fr-par-2", compiled)
    calls = [(m, path) for m, path, _ in s.calls]
    assert calls.index(("PUT", f"{Z}/security_groups/sg-box/rules")) < \
        calls.index(("PATCH", f"{Z}/security_groups/sg-box"))   # rules before the default drop
    rules = next(kw["json"]["rules"] for m, path, kw in s.calls if m == "PUT")
    assert [r["position"] for r in rules] == list(range(1, len(rules) + 1))
    anyrule = next(r for r in rules if r["ip_range"] == "203.0.113.0/24")
    assert anyrule["protocol"] == "ANY" and "dest_port_from" not in anyrule
    ranged = next(r for r in rules if r["ip_range"] == "198.51.100.7/32")
    assert (ranged["dest_port_from"], ranged["dest_port_to"]) == (8000, 9000)
    defaults = next(kw["json"] for m, path, kw in s.calls if m == "PATCH")
    assert defaults == {"inbound_default_policy": "drop", "outbound_default_policy": "drop"}


def test_apply_firewall_without_a_group_is_an_error():
    p, _ = _provider({("GET", f"{Z}/security_groups"): Resp(200, {"security_groups": []})})
    from rixi.policy import NetworkPolicy, compile_rules
    with pytest.raises(ProviderError, match="no firewall"):
        p.apply_firewall("abc", "fr-par-2", compile_rules(NetworkPolicy(), own_ip=None))


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


class FakeDns:
    """Stands in for the dnspython module inside wait_published."""

    def __init__(self, answers):
        self.answers = answers          # {nameserver_ip: [addresses]} ; None = query fails
        self.queries = []

        class Resolver:
            def __init__(inner, configure=True):
                inner.nameservers = []
                inner.lifetime = inner.timeout = 0

            def resolve(inner, name, rdtype):
                self.queries.append((inner.nameservers[0], name))
                got = self.answers.get(inner.nameservers[0])
                if got is None:
                    raise RuntimeError("no answer")
                return got
        self.Resolver = Resolver

    def resolve(self, name, rdtype):
        if rdtype == "NS":
            return [type("R", (), {"target": "ns%d.example." % i})() for i in range(2)]
        return ["10.0.0.%d" % int(name[2])]      # ns0.example -> 10.0.0.0, ns1 -> 10.0.0.1


def _with_fake_dns(fake, fn):
    import sys, types
    mod = types.ModuleType("dns")
    mod.resolver = fake
    old = sys.modules.get("dns"), sys.modules.get("dns.resolver")
    sys.modules["dns"], sys.modules["dns.resolver"] = mod, fake
    try:
        return fn()
    finally:
        for k, v in zip(("dns", "dns.resolver"), old):
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_wait_published_requires_every_nameserver():
    """A wildcard makes a missing record look like a wrong one, so all nameservers must agree."""
    dns = ScalewayDns("secret", "example.com", session=FakeSession({}))
    fake = FakeDns({"10.0.0.0": ["51.0.0.7"], "10.0.0.1": ["65.9.9.9"]})   # ns1 still wildcard
    assert _with_fake_dns(fake, lambda: dns.wait_published("b-x.run.example.com", "51.0.0.7",
                                                           timeout=0.1)) is False
    fake_ok = FakeDns({"10.0.0.0": ["51.0.0.7"], "10.0.0.1": ["51.0.0.7"]})
    assert _with_fake_dns(fake_ok, lambda: dns.wait_published("b-x.run.example.com", "51.0.0.7",
                                                              timeout=5)) is True


def test_wait_published_is_not_fatal_when_dns_cannot_be_queried():
    dns = ScalewayDns("secret", "example.com", session=FakeSession({}))
    fake = FakeDns({"10.0.0.0": None, "10.0.0.1": None})
    assert _with_fake_dns(fake, lambda: dns.wait_published("b-x.run.example.com", "51.0.0.7",
                                                           timeout=0.1)) is False


B = "/block/v1alpha1/zones/fr-par-2"


def test_a_volume_is_created_tagged_and_waited_for():
    polls = {"n": 0}

    def get_vol(path, kw):
        polls["n"] += 1
        return Resp(200, {"id": "sbs-1", "status": "available" if polls["n"] > 1 else "creating"})
    p, s = _provider({
        ("POST", f"{B}/volumes"): Resp(200, {"id": "sbs-1", "status": "creating"}),
        ("GET", f"{B}/volumes/sbs-1"): get_vol,
    })
    assert p.create_volume("v1", "t-1", "data", "fr-par-2", 50) == "sbs-1"
    body = next(kw["json"] for m, path, kw in s.calls if m == "POST")
    assert body["from_empty"] == {"size": 50 * 10**9} and body["perf_iops"] == 5000
    assert "rixi-volume=v1" in body["tags"] and "rixi-tenant=t-1" in body["tags"]


def test_volumes_are_attached_after_the_root_disk():
    routes = {
        ("GET", "/marketplace/v2/local-images"): Resp(200, {"local_images": [
            {"id": "img-1", "compatible_commercial_types": ["DEV1-S"]}]}),
        ("GET", f"{Z}/security_groups"): Resp(200, {"security_groups": [
            {"id": "sg-1", "name": "rixi-direct"}]}),
        ("POST", f"{Z}/ips"): Resp(201, {"ip": {"id": "ip-1", "address": "51.0.0.7"}}),
        ("POST", f"{Z}/servers/"): Resp(202, {}),
        ("POST", f"{Z}/servers"): Resp(201, {"server": {"id": "srv-1"}}),
        ("PATCH", f"{Z}/servers/srv-1/user_data/cloud-init"): Resp(204),
    }
    p, s = _provider(routes)
    p.create(BoxSpec("abc", "t-1", "DEV1-S", "fr-par-2", "ubuntu_noble", "#cc\n{}",
                     root_volume_gb=20, volumes=["sbs-a", "sbs-b"]), on_ip=lambda ip: None)
    vols = next(kw["json"]["volumes"] for m, path, kw in s.calls
                if m == "POST" and path == f"{Z}/servers")
    assert vols["0"]["size"] == 20 * 10**9
    assert vols["1"] == {"id": "sbs-a", "volume_type": "sbs_volume"}
    assert vols["2"] == {"id": "sbs-b", "volume_type": "sbs_volume"}


def test_destroy_detaches_a_persistent_volume_before_deleting_the_server():
    state = {"srv": "running", "gets": 0}

    def server_get(path, kw):
        state["gets"] += 1
        if state["srv"] == "gone":
            return Resp(404)
        return Resp(200, {"server": {"id": "srv-1", "state": state["srv"], "volumes": {}}})

    def action(path, kw):
        if kw["json"]["action"] == "poweroff":
            state["srv"] = "stopped"
        return Resp(202, {})

    def delete_server(path, kw):
        state["srv"] = "gone"
        return Resp(204)
    routes = {
        ("GET", f"{Z}/servers/srv-1"): server_get,
        ("GET", f"{Z}/servers"): Resp(200, {"servers": [{
            "id": "srv-1", "state": "running", "volumes": {
                "0": {"id": "root-1", "volume_type": "sbs_volume"},
                "1": {"id": "keep-1", "volume_type": "sbs_volume"}}}]}),
        ("GET", f"{B}/volumes/root-1"): Resp(200, {"id": "root-1", "tags": ["rixi-box=abc"]}),
        ("GET", f"{B}/volumes/keep-1"): Resp(200, {"id": "keep-1", "tags": ["rixi-volume=v1"]}),
        ("POST", f"{Z}/servers/srv-1/action"): action,
        ("POST", f"{Z}/servers/srv-1/detach-volume"): Resp(200, {}),
        ("DELETE", f"{Z}/servers/srv-1"): delete_server,
        ("DELETE", f"{B}/volumes/root-1"): Resp(204),
        ("GET", f"{Z}/ips"): Resp(200, {"ips": []}),
        ("GET", f"{Z}/security_groups"): Resp(200, {"security_groups": []}),
    }
    p, s = _provider(routes)
    p.destroy("abc", "fr-par-2")
    calls = [(m, path) for m, path, _ in s.calls]
    detach = next(kw["json"] for m, path, kw in s.calls if path.endswith("detach-volume"))
    assert detach == {"volume_id": "keep-1"}
    assert calls.index(("POST", f"{Z}/servers/srv-1/detach-volume")) < \
        calls.index(("DELETE", f"{Z}/servers/srv-1"))
    assert ("DELETE", f"{B}/volumes/root-1") in calls
    assert ("DELETE", f"{B}/volumes/keep-1") not in calls          # the data survives
    actions = [kw["json"]["action"] for m, path, kw in s.calls if path.endswith("/action")]
    assert actions == ["poweroff"]                                 # stopped, then deleted
