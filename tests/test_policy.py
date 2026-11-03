"""Network policy: what the schema accepts, and the firewall rules it compiles to."""
import pytest

from rixi.policy import NetworkPolicy, PolicyError, compile_rules, parse
from rixi.policy import resolvers_from as parse_resolvers


def test_empty_is_the_open_default():
    assert parse(None).is_default and parse({}).is_default
    c = compile_rules(parse(None), own_ip="51.0.0.7")
    assert c.outbound_default == "accept" and c.inbound_default == "drop"
    assert any(r.direction == "outbound" and r.action == "drop" and r.port_from == 25
               for r in c.rules)                            # mail is never allowed out


def test_entries_are_normalised_and_deduplicated():
    p = parse({"egress": {"mode": "allowlist",
                          "allow": ["203.0.113.9/24", "198.51.100.7:443", "198.51.100.7:443",
                                    "10.0.0.0/8:8000-9000"]},
               "ingress": {"allow": ["192.0.2.1", "192.0.2.1/32"]}})
    assert [str(d) for d in p.egress_allow] == ["203.0.113.0/24", "198.51.100.7/32:443",
                                                "10.0.0.0/8:8000-9000"]
    assert p.ingress_allow == ("192.0.2.1/32",)
    assert parse(p.to_dict()) == p                          # round-trips


@pytest.mark.parametrize("doc,msg", [
    ({"egress": {"mode": "sometimes"}}, "mode"),
    ({"egress": {"mode": "open", "allow": ["1.2.3.4"]}}, "only applies"),
    ({"egress": {"mode": "allowlist", "allow": ["example.com"]}}, "not an IP"),
    ({"egress": {"mode": "allowlist", "allow": ["1.2.3.4:http"]}}, "port"),
    ({"egress": {"mode": "allowlist", "allow": ["1.2.3.4:70000"]}}, "1-65535"),
    ({"egress": {"mode": "allowlist", "allow": ["1.2.3.4:9-3"]}}, "low to high"),
    ({"ingress": {"allow": ["2001:db8::/32"]}}, "IPv4"),
    ({"egress": {"mode": "allowlist", "allow": ["1.1.1.1"] * 41}}, "at most"),
    ({"firewall": {}}, "unknown"),
    ("block", "object"),
])
def test_invalid_policies_are_refused_with_a_reason(doc, msg):
    with pytest.raises(PolicyError, match=msg):
        parse(doc)


def _out(c):
    return {(r.action, r.protocol, r.cidr, r.port_from) for r in c.rules if r.direction == "outbound"}


def test_block_keeps_only_what_the_box_needs():
    c = compile_rules(parse({"egress": {"mode": "block"}}), own_ip="51.0.0.7",
                      control_plane=["65.21.252.62"], resolvers=["51.159.47.28", "127.0.0.53"])
    assert c.outbound_default == "drop"
    out = _out(c)
    assert ("accept", "UDP", "51.159.47.28/32", 53) in out and ("accept", "TCP", "51.159.47.28/32", 53) in out
    assert ("accept", "TCP", "65.21.252.62/32", 443) in out          # heartbeats + token keys
    assert ("accept", "TCP", "51.0.0.7/32", 443) in out              # its own TLS self-check
    assert not any(cidr.startswith("127.") for _, _, cidr, _ in out)  # loopback is not a resolver
    accepted = {cidr for action, _, cidr, _ in out if action == "accept"}
    assert accepted == {"51.159.47.28/32", "65.21.252.62/32", "51.0.0.7/32"}


def test_allowlist_adds_exactly_the_listed_destinations():
    c = compile_rules(parse({"egress": {"mode": "allowlist",
                                        "allow": ["203.0.113.0/24", "198.51.100.7:443"]}}),
                      own_ip="51.0.0.7", control_plane=["65.21.252.62"], resolvers=[])
    out = _out(c)
    assert ("accept", "ANY", "203.0.113.0/24", None) in out
    assert ("accept", "TCP", "198.51.100.7/32", 443) in out
    assert ("accept", "UDP", "198.51.100.7/32", 443) in out
    assert c.outbound_default == "drop"


def test_ingress_allowlist_restricts_api_and_ssh_but_not_certificates():
    c = compile_rules(parse({"ingress": {"allow": ["192.0.2.0/24"]}}), own_ip="51.0.0.7")
    inbound = {(r.cidr, r.port_from) for r in c.rules if r.direction == "inbound"}
    assert ("0.0.0.0/0", 80) in inbound                              # ACME still works
    assert ("192.0.2.0/24", 443) in inbound and ("192.0.2.0/24", 22) in inbound
    assert ("51.0.0.7/32", 443) in inbound                           # the box can reach itself
    assert ("0.0.0.0/0", 443) not in inbound and ("0.0.0.0/0", 22) not in inbound
    assert c.inbound_default == "drop"


def test_no_ingress_allowlist_means_anyone_may_connect():
    c = compile_rules(NetworkPolicy(), own_ip="51.0.0.7")
    inbound = {(r.cidr, r.port_from) for r in c.rules if r.direction == "inbound"}
    assert ("0.0.0.0/0", 443) in inbound and ("0.0.0.0/0", 22) in inbound


def test_resolvers_are_read_from_resolv_conf():
    text = "# generated\nnameserver 127.0.0.53\nnameserver 51.159.47.28\noptions edns0\n" \
           "nameserver 2001:db8::1\nnameserver 51.159.47.26\n"
    assert parse_resolvers(text) == ["51.159.47.28", "51.159.47.26"]


def test_a_box_without_a_certificate_opens_no_port_80():
    c = compile_rules(parse({"egress": {"mode": "block"}}), own_ip=None, resolvers=["10.0.0.1"],
                      api_ports=(9000, 22), cert_port=None)
    inbound = {(r.cidr, r.port_from) for r in c.rules if r.direction == "inbound"}
    assert inbound == {("0.0.0.0/0", 9000), ("0.0.0.0/0", 22)}
