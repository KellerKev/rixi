# Scaleway provider (reference)

Provisions a real Scaleway instance and has it dial the gateway. Validated against a live account
(`tofu plan`); the full provision → bootstrap → brokered-connect → deploy loop was exercised
end-to-end on real Scaleway hardware (see **Verified live** below).

## Use

```bash
export SCW_ACCESS_KEY=…  SCW_SECRET_KEY=…  SCW_DEFAULT_PROJECT_ID=…
# (project id is required; fetch it for a key with:
#  curl -H "X-Auth-Token: $SCW_SECRET_KEY" https://api.scaleway.com/iam/v1alpha1/api-keys/$SCW_ACCESS_KEY )
# the gateway normally drives this via the `provision` action; to run by hand:
tofu -chdir=. init
tofu apply -auto-approve \
  -var gateway_ws_url=ws://your-gateway:7100 \
  -var node_id=<one-time-token> \
  -var tunnel_secret=<gateway-secret> \
  -var instance_type=L4-1-24G -var region=fr-par-2
```

On boot, `cloud-init.tftpl` fetches the open `bootstrap.sh` and installs the rixi server + tunnel
agent, which dials `gateway_ws_url` with `node_id` (the token) — the gateway redeems the claim and
wires the waiting client.

> **Secret handling.** `tunnel_secret`/`key_secret` are passed to OpenTofu as `TF_VAR_*` env (never
> written to `terraform.tfvars.json`) and the provisioning workdir is created `0700`/`0600`. Two
> residual exposures are inherent to this flow: (1) OpenTofu records applied values in
> `terraform.tfstate` in that workdir — use encrypted/remote state for stronger guarantees; and
> (2) `cloud-init.tftpl` places these secrets in the instance's **user-data**, which anything running
> on the box can read from the metadata service. Prefer short-lived, single-use secrets and rotate
> after provisioning.

## GPU images & capacity

The default `image = "ubuntu_jammy"` resolves for standard instances. **GPU types (`L4-*`, `H100-*`,
`RENDER-*`) need a GPU-ready image** — the plain `ubuntu_jammy` label won't resolve for them
(`couldn't find a local image for … commercial type`). Pass a GPU OS image, e.g.
`-var image=ubuntu_focal_gpu_os_12` (check `scaleway_marketplace_image` for the current label in your
zone). GPU stock also moves: at the time of writing `L4-1-24G` was `shortage` in `fr-par-1` and
`scarce` in `fr-par-2`, and GPU types may require a quota increase — check
`/instance/v1/zones/<zone>/products/servers/availability` before applying.

## Verified live

The end-to-end flow was run on real Scaleway: a public gateway box + a provisioned server box whose
cloud-init `bootstrap.sh` installed the rixi server + tunnel agent and dialed the gateway; a local
client then reached the rixi server (`GET /health` → `200`) through the brokered tunnel, confirmed in
the server's own access log. The server box ran on a standard instance (the rixi server/tunnel don't
need the GPU); swapping `instance_type`/`image` to a GPU is the only change for GPU hardware.

## Another cloud

Copy this directory, swap the `provider`/`resource` blocks for your cloud, keep the same
`variables` + the `cloud-init.tftpl` user-data, and select it with `provider: "<name>"` in
`request_compute`. The gateway and the rest of the flow are unchanged.
