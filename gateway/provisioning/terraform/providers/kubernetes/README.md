# Kubernetes provider — a rixi box as a Pod ("resource ≠ VM")

Provisions a rixi box as a **Kubernetes Pod** instead of a cloud VM. The pod runs the *same* open
[`bootstrap.sh`](../../../../bootstrap.sh) as a VM (install the rixi server + tunnel agent, dial the
gateway outbound), so a k8s workload joins the gateway exactly like a provisioned VM. It satisfies
the [`modules/iface`](../../modules/iface/README.md) contract — a drop-in alongside
`providers/scaleway` and `providers/hetzner`.

> **No cloud credentials.** It uses your local **kubeconfig**, so any reachable cluster works —
> including **Rancher Desktop / k3s** for local, free development. Verified end-to-end on Rancher
> Desktop: `tofu apply` creates the pod, `tofu destroy` removes it.

## Catalog entry

```toml
[resource.k8s-box]
provider = "kubernetes"
reuse    = true
teardown = "on_release"
[resource.k8s-box.vars]
namespace    = "default"
kube_context = "rancher-desktop"   # optional; omit to use the current kubeconfig context
image        = "debian:bookworm-slim"
```

## Vars (defaults)

| tofu var | default | notes |
|---|---|---|
| `namespace` | `default` | namespace for the pod |
| `image` | `debian:bookworm-slim` | base image; the pod installs curl+git then runs `bootstrap.sh` |
| `cpu` / `memory` | `500m` / `512Mi` | pod resource requests |
| `kubeconfig` | `~/.kube/config` | path to the kubeconfig |
| `kube_context` | current context | kube context to use |

Other iface vars (`gateway_ws_url`, `node_id`, `tunnel_secret`, `key_secret`, `jwt_*`, `kdf_salt`)
are passed by the gateway. The `hashicorp/kubernetes` provider auto-installs on `tofu init`.

## By hand

```bash
tofu -chdir=. init
tofu apply -auto-approve \
  -var gateway_ws_url=ws://your-gateway:7100 \
  -var node_id=<one-time-token> \
  -var tunnel_secret=<gateway-secret> \
  -var kube_context=rancher-desktop
kubectl get pod <node_id>
tofu destroy -auto-approve -var gateway_ws_url=… -var node_id=… -var tunnel_secret=… -var kube_context=rancher-desktop
```

## Reaching the gateway (validated recipe)

The pod dials the gateway **outbound** (like any rixi box), so the gateway must advertise a
dial-back URL the pod can reach — not `ws://127.0.0.1`. Start the gateway with `--public-ws-url`
pointing at a host address reachable from pods. On Rancher Desktop / Docker Desktop that's
`host.docker.internal`:

```bash
python -m gateway --secret "$S" --config rixi.toml \
  --ws-bind 0.0.0.0:7100 --public-ws-url ws://host.docker.internal:7100
# then, from a client:
python -m gateway.client --to ws://127.0.0.1:7100 --secret "$S" --resource k8s-box --bind 127.0.0.1:9200
```

**Validated end-to-end on Rancher Desktop (arm64 k3s):** the gateway provisions the pod, the pod
resolves its pixi env, dials back, and registers (~40 s); routing to it and `GET /health` succeed
through the brokered tunnel; teardown removes the pod. (Requires the box components to support ARM
Linux — `linux-aarch64` is in their pixi platforms.) For Metaflow `@rixi(resource="k8s-box")` runs
the pod also needs to reach the S3 datastore (e.g. a MinIO on the host at
`http://host.docker.internal:9100`). The pod itself needs no inbound access.
