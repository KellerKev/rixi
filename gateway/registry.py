"""Registry of nodes (rixi servers + clients) connected to the gateway.

Node registry for the gateway broker. See DESIGN.md.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RegisteredNode:
    node_id: str
    kind: str                       # "server" | "client"
    identity: str                   # authenticated identity (for audit / policy)
    capabilities: List[str] = field(default_factory=list)
    conn: Any = None                # the open-tunnel connection for this node
    port: Optional[int] = None      # local TCP port the gateway exposes for this node (servers)
    auth: Any = None                # verified Identity (auth.py) for clients; None for server agents
    registered_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    meta: Dict[str, Any] = field(default_factory=dict)


class Registry:
    """node_id → RegisteredNode. Authenticated registration, presence, lookup, routing."""

    def __init__(self) -> None:
        self._nodes: Dict[str, RegisteredNode] = {}

    def register(self, node: RegisteredNode) -> None:
        self._nodes[node.node_id] = node

    def unregister(self, node_id: str) -> None:
        self._nodes.pop(node_id, None)

    def get(self, node_id: str) -> Optional[RegisteredNode]:
        return self._nodes.get(node_id)

    def list(self, kind: Optional[str] = None) -> List[RegisteredNode]:
        return [n for n in self._nodes.values() if kind is None or n.kind == kind]

    def touch(self, node_id: str) -> None:
        n = self._nodes.get(node_id)
        if n is not None:
            n.last_seen = time.time()
