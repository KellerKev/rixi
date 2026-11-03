"""Network policy for a sandboxed environment — what it may reach, and who may reach it.

One schema for every way a box is made (the hosted gateway, `rixi up`), compiled into plain
firewall rules that each provider applies to the box's *own* cloud firewall. That placement is the
point: anything inside the box, the agent can undo as root; the cloud firewall it cannot touch.

    {"egress":  {"mode": "open" | "allowlist" | "block",
                 "allow": ["203.0.113.0/24", "198.51.100.7:443", "0.0.0.0/0:443", "10.0.0.0/8:8000-9000"]},
     "ingress": {"allow": ["192.0.2.0/24"]}}

- egress `open`: anything goes (outbound mail ports are still blocked by the provider).
- egress `allowlist`: only the listed destinations, plus what the box needs to stay a box.
- egress `block`: nothing but what the box needs to stay a box.
- ingress `allow`: who may reach the box's API (443) and SSH (22); empty = anyone.

"What the box needs to stay a box" is always allowed, so a policy can never strand it: DNS to its
own resolvers, HTTPS to the control plane (heartbeats, token keys), HTTPS to its own address (its
TLS self-check), and port 80 inbound for certificate challenges. IPv4 only for now.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple

EGRESS_MODES = ("open", "allowlist", "block")
MAX_EGRESS_RULES = 40      # Scaleway allows about 100 rules per group; leave room for the fixed ones
MAX_INGRESS_RULES = 20


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Destination:
    cidr: str
    port_from: Optional[int] = None      # None = every port
    port_to: Optional[int] = None

    def __str__(self) -> str:
        if self.port_from is None:
            return self.cidr
        ports = str(self.port_from) if self.port_to == self.port_from else \
            f"{self.port_from}-{self.port_to}"
        return f"{self.cidr}:{ports}"


@dataclass(frozen=True)
class Rule:
    direction: str            # inbound | outbound
    action: str               # accept | drop
    protocol: str             # TCP | UDP | ANY
    cidr: str
    port_from: Optional[int] = None
    port_to: Optional[int] = None


@dataclass(frozen=True)
class NetworkPolicy:
    egress_mode: str = "open"
    egress_allow: Tuple[Destination, ...] = ()
    ingress_allow: Tuple[str, ...] = ()

    @property
    def is_default(self) -> bool:
        return self.egress_mode == "open" and not self.egress_allow and not self.ingress_allow

    def to_dict(self) -> dict:
        return {"egress": {"mode": self.egress_mode, "allow": [str(d) for d in self.egress_allow]},
                "ingress": {"allow": list(self.ingress_allow)}}


@dataclass
class Compiled:
    inbound_default: str
    outbound_default: str
    rules: List[Rule] = field(default_factory=list)


def _cidr(value: str) -> str:
    try:
        net = ipaddress.ip_network(value.strip(), strict=False)
    except ValueError:
        raise PolicyError(f"{value!r} is not an IP address or CIDR range")
    if net.version != 4:
        raise PolicyError(f"{value!r}: only IPv4 is supported for now")
    return str(net)


def _destination(entry: str) -> Destination:
    if not isinstance(entry, str) or not entry.strip():
        raise PolicyError("allow entries must be strings like 203.0.113.0/24 or 198.51.100.7:443")
    host, sep, ports = entry.strip().rpartition(":") if ":" in entry else (entry, "", "")
    cidr = _cidr(host)
    if not sep:
        return Destination(cidr)
    try:
        lo, _, hi = ports.partition("-")
        p_from, p_to = int(lo), int(hi or lo)
    except ValueError:
        raise PolicyError(f"{entry!r}: the port must be a number or a range like 8000-9000")
    if not (1 <= p_from <= p_to <= 65535):
        raise PolicyError(f"{entry!r}: ports must be 1-65535, low to high")
    return Destination(cidr, p_from, p_to)


def parse(data: Optional[dict]) -> NetworkPolicy:
    """Validate a policy document. `None` or `{}` is the open default."""
    if not data:
        return NetworkPolicy()
    if not isinstance(data, dict):
        raise PolicyError("policy must be an object with 'egress' and/or 'ingress'")
    unknown = set(data) - {"egress", "ingress"}
    if unknown:
        raise PolicyError(f"unknown policy keys: {sorted(unknown)}")
    egress = data.get("egress") or {}
    ingress = data.get("ingress") or {}
    if not isinstance(egress, dict) or not isinstance(ingress, dict):
        raise PolicyError("'egress' and 'ingress' must be objects")
    mode = egress.get("mode", "open")
    if mode not in EGRESS_MODES:
        raise PolicyError(f"egress mode must be one of {EGRESS_MODES}")
    allow = egress.get("allow") or []
    if not isinstance(allow, list):
        raise PolicyError("egress.allow must be a list")
    if allow and mode != "allowlist":
        raise PolicyError("egress.allow only applies with mode 'allowlist'")
    if len(allow) > MAX_EGRESS_RULES:
        raise PolicyError(f"at most {MAX_EGRESS_RULES} egress entries")
    ingress_allow = ingress.get("allow") or []
    if not isinstance(ingress_allow, list):
        raise PolicyError("ingress.allow must be a list")
    if len(ingress_allow) > MAX_INGRESS_RULES:
        raise PolicyError(f"at most {MAX_INGRESS_RULES} ingress entries")
    return NetworkPolicy(
        egress_mode=mode,
        egress_allow=tuple(dict.fromkeys(_destination(e) for e in allow)),
        ingress_allow=tuple(dict.fromkeys(_cidr(c) for c in ingress_allow)))


def _hosts(addresses: Iterable[str]) -> List[str]:
    out = []
    for a in addresses:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.version == 4 and not ip.is_loopback:
            out.append(f"{ip}/32")
    return list(dict.fromkeys(out))


def compile_rules(policy: NetworkPolicy, *, own_ip: Optional[str],
                  control_plane: Iterable[str] = (), resolvers: Iterable[str] = (),
                  api_ports: Tuple[int, ...] = (443, 22)) -> Compiled:
    """Turn a policy into firewall rules for one box.

    `control_plane` and `resolvers` are addresses the box must keep reaching; `own_ip` is the
    box's public address (its TLS self-check goes out and comes back through the firewall).
    """
    rules: List[Rule] = []
    # inbound: certificate challenges always; the API and SSH from the allowlist (or anyone)
    rules.append(Rule("inbound", "accept", "TCP", "0.0.0.0/0", 80, 80))
    sources = list(policy.ingress_allow) or ["0.0.0.0/0"]
    if policy.ingress_allow and own_ip:
        sources += _hosts([own_ip])
    for port in api_ports:
        for src in dict.fromkeys(sources):
            rules.append(Rule("inbound", "accept", "TCP", src, port, port))

    # outbound: mail never; everything else per mode
    for port in (25, 465, 587):
        rules.append(Rule("outbound", "drop", "TCP", "0.0.0.0/0", port, port))
    if policy.egress_mode == "open":
        return Compiled("drop", "accept", rules)

    for r in _hosts(resolvers):
        rules.append(Rule("outbound", "accept", "UDP", r, 53, 53))
        rules.append(Rule("outbound", "accept", "TCP", r, 53, 53))
    for h in _hosts(list(control_plane) + ([own_ip] if own_ip else [])):
        rules.append(Rule("outbound", "accept", "TCP", h, 443, 443))
    if policy.egress_mode == "allowlist":
        for d in policy.egress_allow:
            if d.port_from is None:
                rules.append(Rule("outbound", "accept", "ANY", d.cidr))
            else:
                rules.append(Rule("outbound", "accept", "TCP", d.cidr, d.port_from, d.port_to))
                rules.append(Rule("outbound", "accept", "UDP", d.cidr, d.port_from, d.port_to))
    return Compiled("drop", "drop", rules)
