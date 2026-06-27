# RIXI Reverse Tunnel

Reach a **firewalled** rixi server that can't accept inbound connections. The firewalled host dials
**outbound** to a reachable host over an AES-256-GCM-encrypted WebSocket; the reachable host exposes
a local TCP port that is forwarded — multiplexed — back to the rixi server. Point a normal rixi
client at the local port and everything works, because the tunnel forwards **raw TCP** and is
protocol-agnostic (upload, streaming, the AES handshake, the MCP back-channel all pass through
untouched). It's a tiny, self-hosted, PSK-encrypted reverse tunnel pointed at the rixi server.

## How it connects

```
  FIREWALLED HOST                                  REACHABLE HOST
  ───────────────                                  ─────────────
  rixi_server  :9000                               rixi-tunnel listen
       ▲                                             • ws-bind :7000  ◀─── outbound wss ───┐
       │ raw TCP (localhost)                         • local TCP :9100                     │
  rixi-tunnel connect ──── dials out, AES-GCM frames, mux by session_id ────────────────────┘
                                                          ▲
                                                          │  http://127.0.0.1:9100
                                                     rixi_client  (talks to the server through the tunnel)
```

## Run

On the **reachable** host (e.g. your laptop or a small VPS the server can dial):

```bash
cd tunnel && pixi install
RIXI_TUNNEL_SECRET=change-me pixi run listen -- --bind 127.0.0.1:9100 --ws-bind 0.0.0.0:7000
```

On the **firewalled** host (next to the rixi server):

```bash
RIXI_TUNNEL_SECRET=change-me pixi run connect -- --to ws://REACHABLE-HOST:7000 --target 127.0.0.1:9000
```

Then drive the firewalled server as if it were local:

```bash
rixi_client.py --server http://127.0.0.1:9100 --task hello
```

## Flags

| Role | Flag | Purpose |
|------|------|---------|
| both | `--secret` / `RIXI_TUNNEL_SECRET` | shared secret; the AES key is derived from it (PBKDF2) |
| `listen` | `--bind` | local TCP port to expose (default `127.0.0.1:9100`, loopback) |
| `listen` | `--ws-bind` | address the firewalled server dials into (default `0.0.0.0:7000`) |
| `connect` | `--to` | listener WebSocket URL, e.g. `ws://host:7000` |
| `connect` | `--target` | the local service to forward (the rixi server, default `127.0.0.1:9000`) |

## Security

- Every frame is **AES-256-GCM** with a key derived from `--secret` via PBKDF2-HMAC-SHA256. On
  connect, the listener sends an encrypted challenge and requires an HMAC proof — a wrong secret
  can't decrypt it and is rejected.
- The **`--ws-bind` side is the exposed surface**; the PSK + AES-GCM is what protects it. Keep
  `--bind` (the forwarded port) on **loopback** so only local processes use it. For an extra layer,
  terminate the WebSocket behind TLS (`wss://`) at a reverse proxy.
- This is the open-core connectivity primitive. A central **gateway** (registration, routing, and
  firing actions like provisioning a GPU server) builds on top of it.
