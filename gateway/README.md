# gateway/ — rixi gateway

Control plane: a central, authenticated rendezvous where rixi **servers** and **clients**
register, the gateway **routes** client↔server connections over their registered tunnels, and
**fires actions** (e.g. provision a Scaleway GPU server on demand and wire it in).

It builds on the **open** reverse-tunnel primitive ([`rixi/tunnel`](../tunnel/)) as its data plane
and drives rixi over its public HTTP API — never forking the execution layer.

## Status

**MVP working: authenticated server registration + per-node routing + a registry.** Many
firewalled rixi servers dial in with the **unmodified open** `rixi-tunnel connect` (same wire
format, see [`protocol.py`](protocol.py)); each is identified by its `node_id` at auth, registered,
and given a dedicated local TCP port that reverse-proxies to it.

```bash
# gateway (reachable host)
RIXI_GATEWAY_SECRET=… pixi run serve -- --ws-bind 0.0.0.0:7100

# each firewalled rixi server (uses the public core's tunnel CLI, unchanged):
rixi-tunnel connect --to ws://GATEWAY:7100 --target 127.0.0.1:9000 --node-id gpu-1 --secret …
#   gateway logs:  ✅ registered 'gpu-1' → reach it at 127.0.0.1:<port>
#   → point a rixi client at 127.0.0.1:<port>
```

Verified end-to-end: two servers register and route independently; the public `rixi-tunnel connect`
registers a real rixi server and a full `examples/hello` deploy runs through the gateway.

Brokered routing (client agents that also dial in), the control API, and OpenTofu provisioning
(dummy + Scaleway, verified live) are now implemented; see [`client.py`](client.py),
[`actions/provision.py`](actions/provision.py), and [`provisioning/`](provisioning/).

## Two modes

| | **Tunnel mode** (`python -m gateway`) | **Direct mode** (`python -m gateway.direct`) |
|---|---|---|
| Data path | client → gateway → box (brokered) | client → box, TLS ending on the box |
| Reaches firewalled boxes | yes, boxes dial out | no, boxes need a public IP |
| Gateway sees workload traffic | yes (tunnel frames) | never, only heartbeats (task count, readiness) |
| Tenancy | single operator, one shared tunnel secret | per-tenant boxes, scoped tokens, 404 across tenants |
| State | in memory | durable (SQLite or Postgres, via sqladal) |
| HTTP stack | FastAPI admin API | websaw-ng |

Use tunnel mode for your own firewalled or air-gapped machines. Use direct mode to offer boxes to
several tenants: nobody's traffic crosses the gateway, and a box can only be driven with a token
minted for that box and its tenant.

## Direct mode — a control plane only

`python -m gateway.direct --config rixi-direct.toml` (see
[`rixi-direct.toml.example`](rixi-direct.toml.example)). A claim creates one box for the caller's
tenant:

1. The box row is written to the store **before** anything exists at the cloud, and every cloud
   resource is tagged `rixi-managed` / `rixi-box=<id>` / `rixi-tenant=<t>`, so a crash at any step
   leaves something the reaper or reconciler can finish.
2. The public IP is allocated, `b-<id>.<box_domain>` is pointed at it, then the box boots with
   user-data carrying only its own id, tenant, hostname and heartbeat secret (stored hashed).
3. [`box/bootstrap-direct.sh`](../box/bootstrap-direct.sh), from a pinned release, installs the rixi
   server (loopback, `--audience box-<id> --required-claim tenant=<t> --revoked-jti-file`), Caddy
   (Let's Encrypt for the box's name) and a heartbeat agent.
4. The box is `ready` once its public name serves a valid certificate. The reaper releases it on
   TTL, idle (no running task), lost heartbeat, a failed or interrupted create, or on request.
   The reconciler destroys tagged cloud resources the store does not own and stops billing boxes
   the cloud no longer has.

Billing stays outside: `GET /api/usage?since=` returns each box's rate (snapshotted at claim) and
its billable interval, and an optional authorizer URL must approve every claim. A tenant can be
stopped with `POST /api/tenants/<t>/stop`.

## Resource catalog — declare compute in TOML (no OpenTofu required)

Instead of passing raw provisioning specs, declare named resources in a gateway-side
[`rixi.toml`](rixi.toml.example) (gitignored; copy from `rixi.toml.example`). Each entry picks a
provider, server type, region, credentials, and a **lifecycle policy**; the gateway translates it
into the existing OpenTofu actions. Clients just ask for a box **by name**:

```bash
# gateway loads the catalog:
python -m gateway --secret "$RIXI_GATEWAY_SECRET" --config rixi.toml

# client requests a box by name — provision-or-reuse, then route:
python -m gateway.client --to ws://GATEWAY:7100 --secret S --resource gpu-box --bind 127.0.0.1:9100
python -m gateway.client --to ws://GATEWAY:7100 --secret S --teardown gpu-box   # tofu destroy
```

```toml
[resource.gpu-box]
provider     = "scaleway"
type         = "L4-1-24G"      # → instance_type
region       = "fr-par-2"
reuse        = true            # reuse if already up · false = a fresh box per request
teardown     = "idle"          # manual | on_release | idle | ttl
idle_timeout = "30m"
[resource.gpu-box.credentials]
api_key = "${env:SCW_ACCESS_KEY}"   # ${env:}/${file:} kept out of git; mapped to the provider env
secret  = "${env:SCW_SECRET_KEY}"
```

**Two knobs:** `reuse` (route to an already-up box vs. always provision fresh) and `teardown`
(`manual` / `on_release` / `idle` / `ttl`). Because OpenTofu is idempotent, "reuse" is just a stable
per-resource state dir (`~/.rixi/resources/<name>`); "fresh" is a throwaway one. Experts can set
`module = "./my-tofu"` on a resource to run their own OpenTofu (contract: it accepts
`gateway_ws_url`/`node_id`/`tunnel_secret`). See [`rixi.toml.example`](rixi.toml.example) and
[`config.py`](config.py).

## State encryption & the state passphrase

OpenTofu records applied values (including the tunnel/handshake secrets) in `terraform.tfstate`, so
the gateway **encrypts state and plan files at rest** by default (OpenTofu 1.7+ AES-GCM). The
encryption key is derived (PBKDF2) from a passphrase:

- `RIXI_STATE_PASSPHRASE` — the passphrase. If unset, the gateway **auto-generates** one and stores
  it at `$RIXI_STATE_DIR/state.key` (default `~/.rixi/state.key`, mode `0600`).

> **Set `RIXI_STATE_PASSPHRASE` explicitly in production.** The auto-generated key lives on that one
> gateway host: rebuild or move the gateway, or run more than one, and it won't be shared — existing
> encrypted state becomes **undecryptable**, which means you can't `tofu destroy` those boxes through
> the gateway anymore (you'd fall back to the provider's console/API to clean them up). Use a long
> passphrase (PBKDF2 wants ≥16 chars; use 32+), keep it in a secret manager, and back it up. Losing
> it is equivalent to losing the state.

For an HA gateway (or to keep state off the gateway host entirely), point state at a remote backend
with `RIXI_STATE_BACKEND` (JSON) or a catalog `[state.backend]` block — see `rixi.toml.example`. To
disable encryption (not recommended), set `RIXI_STATE_ENCRYPTION=off`.

## Costs

Provisioned boxes are priced from [`offerings.toml`](offerings.toml) (per-provider instance specs +
EUR rate; `python -m gateway.offerings` prints it). The gateway records an **estimated cost per box
lifetime** into the audit trail on teardown, and surfaces spend via `GET /api/resources` (live rate +
running cost) and `GET /api/costs` (totals by resource / identity). Prices are static estimates —
refresh `offerings.toml` from the provider pricing APIs as they change.
