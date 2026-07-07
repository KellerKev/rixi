# RIXI Gateway — design

Status: **implemented and tested** (see `gateway/tests/`). Builds on the open reverse-tunnel
primitive in [`rixi/tunnel/`](tunnel/) (wire format vendored in `protocol.py`). This document
describes the control plane: registry, brokered routing, on-demand OpenTofu provisioning, JWT
RBAC + tighten-only policy floor, and a DuckDB/OTLP audit trail.

## What it is

The **rixi gateway** is a central, authenticated rendezvous + control plane. Where the open
`rixi-tunnel` connects *one* firewalled server to *one* client peer-to-peer, the gateway is the
multi-tenant version: many rixi **servers** and **clients** dial in and **register**, the gateway
keeps a **registry**, **routes/brokers** client↔server connections over their registered tunnels,
and can **fire actions** (e.g. provision a GPU server on demand and wire it in).

It exists so you can: register a client → fire an action that spins up a Scaleway GPU server →
that server installs rixi and dials **outbound** back to the gateway → the gateway connects the
waiting client to it — with no inbound firewall holes anywhere.

```
   clients ──register──▶┌───────────────────────┐◀──register── rixi servers
                        │     rixi gateway       │              (dial OUT, like
   "connect me to S"───▶│  registry · routing ·  │               rixi-tunnel connect)
   "fire action X"─────▶│  actions (provisioning)│
                        └───────────┬────────────┘
                                    │ fire action: provision Scaleway GPU,
                                    ▼ install rixi (install-rixi.sh / image)
                          new rixi server ──register (outbound)──▲ wire client⇄server
```

## Guiding principle

The gateway **never forks the execution layer**:
- **reuses the open tunnel protocol** (`rixi/tunnel/rixi_tunnel.py`: AES-GCM frames, PSK-derived
  key, `session_open/data/close` multiplexing) as its data plane — it is essentially the
  `listen` side generalized to many authenticated agents keyed by `node_id`;
- **drives rixi over its public HTTP API** (upload / `/task/*` / stream) — no changes to the core.

## Components

- **`registry.py`** — `Registry`: `node_id → {kind: server|client, tunnel conn, capabilities,
  identity, last_seen}`. Authenticated registration; presence/heartbeat; lookup.
- **`server.py`** — the gateway service: a multi-agent WebSocket listener (the open tunnel's
  `listen` role, per-node), a registration/auth handshake (per-node tokens → identity, JWT
  session), a **routing** layer that bridges a client session to a target server's tunnel, and an
  authenticated **control API** (register, list, `connect`, `fire_action`).
- **`actions/`** — pluggable "fire actions". First action: **`scaleway.py`** — provision a Scaleway
  GPU instance (API), install rixi via [`install-rixi.sh`](../install-rixi.sh) **or** a prebaked
  image, pass the gateway URL + a one-time registration token via cloud-init, then wait for the
  server to register outbound and return its `node_id`.

## Security model

- **Per-node identity:** each server/client authenticates with its own token → an identity the
  gateway records (audit + policy), reusing the tunnel's AES-GCM channel for confidentiality.
- **Isolation:** clients only reach servers they're authorized for (per-tenant namespaces); the
  gateway brokers — a client never gets another tenant's tunnel.
- **One-time provisioning tokens:** a fired action hands the new server a short-lived registration
  token (single use, TTL) so a provisioned box can register exactly once.
- **Outbound-only everywhere:** servers and clients dial the gateway; nothing needs inbound holes.

## Resolved / open questions

- Control API transport — **resolved:** a small FastAPI admin API (`management.py`) runs alongside
  the tunnel WS broker.
- Registry persistence — currently in-memory (`registry.py`); a durable store for restarts is still
  open.
- Multi-gateway / HA and where tenancy/policy lives are still open; policy today is a single-process
  tighten-only engine (`policy.py`).
