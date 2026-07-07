# Hetzner Cloud provider

Provisions a Hetzner Cloud instance and has it dial the gateway via the canonical cloud-init
(which fetches the open `bootstrap.sh`). A drop-in alongside `providers/scaleway` — it satisfies the
same [`modules/iface`](../../modules/iface/README.md) contract, so the gateway treats it identically.

> **CPU only.** Hetzner Cloud has no GPU instances. Use this provider for cheap CPU boxes — compile
> jobs, test runners, data/ETL, and cost-tracking. GPUs come from other providers (`scaleway` today;
> `lambda`/`runpod`/`vast` planned).

## Credentials

A single **project-scoped** API token, via the standard env var:

```bash
export HCLOUD_TOKEN=…      # create in the Hetzner Cloud console → project → Security → API Tokens
```

In the gateway catalog (`rixi.toml`) it's supplied as the resource's `api_key`:

```toml
[resource.hetzner-cpu.credentials]
api_key = "${env:HCLOUD_TOKEN}"
```

## Vars (defaults)

| tofu var | default | notes |
|---|---|---|
| `instance_type` → `server_type` | `cx23` | cheapest shared x86 (2 vCPU / 4 GB) |
| `region` → `location` | `nbg1` | also `fsn1`, `hel1`, `ash`, `hil` |
| `image` | `ubuntu-24.04` | |
| `server_port` | `9000` | rixi server port |

Other iface vars (`gateway_ws_url`, `node_id`, `tunnel_secret`, `key_secret`, `jwt_*`, `kdf_salt`)
are passed by the gateway. The `hcloud` provider auto-installs on `tofu init`.

## By hand

```bash
export HCLOUD_TOKEN=…
tofu -chdir=. init
tofu apply -auto-approve \
  -var gateway_ws_url=ws://your-gateway:7100 \
  -var node_id=<one-time-token> \
  -var tunnel_secret=<gateway-secret> \
  -var instance_type=cx23 -var region=nbg1
tofu destroy -auto-approve      # always tear down when done
```

The gateway normally drives this via the `provision` action; provisioning state is encrypted at
rest (see [`provisioning/tofu.py`](../../tofu.py)).
