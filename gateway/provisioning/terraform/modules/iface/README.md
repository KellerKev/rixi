# Provider interface

The standard contract every `providers/<name>` module implements, so providers are swappable and
the gateway stays cloud-agnostic:

- **`variables.tf`** — the inputs the tofu driver passes (`gateway_ws_url`, `node_id` (the one-time
  token), `tunnel_secret`, `server_port`, `rixi_ref`, `instance_type`, `region`, `spot`).
- **`cloud-init.tftpl`** — the canonical user-data: on first boot the box fetches the **open**
  `bootstrap.sh` from the public rixi repo and runs it with the env above, which installs the rixi
  server + tunnel agent and dials the gateway with the token as `node_id`.
- **output `node_id`** — every provider returns the node_id so the gateway can correlate the
  registration.

A real provider (see `providers/scaleway`) renders `cloud-init.tftpl` into its instance's user-data.
The `providers/dummy` module skips cloud-init and stands the pieces up locally for tests.

## Spot / preemptible (`spot`)

`spot` (bool, default false) requests cheaper interruptible capacity. A provider that supports it
(a future AWS module via `instance_market_options`, or GCP via `scheduling.provisioning_model =
"SPOT"`) consumes the flag; the gateway then:

- tries spot first and **falls back to on-demand** on a capacity error (capacity-retry loop in
  `actions/provision.py`; audited `spot.fallback`),
- **re-provisions** the box if the cloud preempts it (the dropped tunnel is treated as a
  preemption in `server.py`; audited `spot.interrupted`),
- prices the resource at its **spot rate** (`offerings.spot_eur_per_hour`) for capability
  scheduling, so a spot box wins on cost.

**No shipped provider offers spot** — Hetzner and Scaleway have no spot product, and a pod is not a
spot VM, so `providers/{hetzner,scaleway,kubernetes}` declare `spot` but accept-and-ignore it. The
`providers/dummy` module *simulates* a preemption (`spot_ttl` seconds → the box self-terminates) so
the interruption path can be tested locally. Adding real spot = a new AWS/GCP module that consumes
`spot`, plus a `spot_eur_per_hour` in `offerings.toml`.
