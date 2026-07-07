"""rixi gateway — brokered control plane.

A rendezvous/broker that lets clients reach registered rixi nodes behind firewalls,
provisions on-demand compute (OpenTofu), and enforces JWT RBAC + a tighten-only policy
floor with a DuckDB/OTLP audit trail. Builds on the open reverse-tunnel wire format
(vendored in protocol.py, byte-compatible with rixi/tunnel). See DESIGN.md.
"""

__all__ = ["Registry", "RegisteredNode", "Gateway"]

from .registry import Registry, RegisteredNode
from .server import Gateway
