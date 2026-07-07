# Kubernetes provider — a rixi "box" as a Pod instead of a VM ("resource != VM"). The pod runs the
# same open bootstrap.sh as a cloud VM (install rixi server + tunnel agent, dial the gateway
# outbound), so a k8s workload joins the gateway exactly like a provisioned VM. Works against any
# cluster the local kubeconfig can reach (e.g. Rancher Desktop / k3s for local testing).
# Satisfies the modules/iface contract; a drop-in alongside providers/scaleway and providers/hetzner.

terraform {
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
  }
}

variable "gateway_ws_url" { type = string }
variable "node_id" { type = string }
variable "tunnel_secret" {
  type      = string
  sensitive = true
}
variable "server_port" {
  type    = number
  default = 9000
}
variable "rixi_ref" {
  type    = string
  default = "main"
}
variable "kdf_salt" {
  type    = string
  default = ""
}
variable "key_secret" {
  type      = string
  default   = ""
  sensitive = true
}
variable "key_secret_uses" {
  type    = number
  default = 0
}
variable "jwt_jwks_url" {
  type    = string
  default = ""
}
variable "jwt_public_key" {
  type      = string
  default   = ""
  sensitive = true
}

# Accepted for iface compatibility (the gateway passes it to every provider), but ignored: a pod is
# not a spot VM. (Preemptible node pools would be a cluster-level concern, not a per-pod flag.)
variable "spot" {
  type    = bool
  default = false
}

# k8s-specific knobs (passed as tofu vars from the catalog; not part of the VM iface).
variable "namespace" {
  type    = string
  default = "default"
}
variable "image" {
  type    = string
  default = "debian:bookworm-slim" # small base with apt; the pod installs curl+git then bootstraps
}
variable "cpu" {
  type    = string
  default = "500m"
}
variable "memory" {
  type    = string
  default = "2Gi" # a rixi box resolves a pixi env + runs tasks; 512Mi is too small
}
variable "kubeconfig" {
  type    = string
  default = "~/.kube/config"
}
variable "kube_context" {
  type    = string
  default = ""
}
variable "repo" {
  type    = string
  default = "https://github.com/KellerKev/rixi"
}

provider "kubernetes" {
  config_path    = var.kubeconfig
  config_context = var.kube_context != "" ? var.kube_context : null
}

locals {
  # k8s object names must be RFC 1123 (lowercase alnum + '-', start/end alnum). node_id may be a
  # one-time claim token (e.g. "rxtok_Ab-…", for reuse=false resources), so derive a valid pod name
  # from it. The tunnel still uses the RAW node_id (RIXI_NODE_ID below) so the gateway matches the
  # box to its claim/resource — only the k8s object name is sanitized.
  pod_name = substr(replace(lower(var.node_id), "/[^a-z0-9-]/", "-"), 0, min(63, length(var.node_id)))

  # Install curl/git, fetch + run the open bootstrap (which backgrounds the server + tunnel), then
  # keep PID 1 alive so the pod stays up while the agent dials the gateway.
  boot_cmd = <<-EOT
    set -e
    if ! command -v curl >/dev/null 2>&1; then
      apt-get update -qq && apt-get install -y -qq --no-install-recommends curl git ca-certificates >/dev/null 2>&1 || true
    fi
    curl -fsSL "${var.repo}/raw/${var.rixi_ref}/bootstrap.sh" -o /tmp/bootstrap.sh
    bash /tmp/bootstrap.sh || echo "[rixi] bootstrap failed; keeping pod alive for diagnostics"
    exec sleep infinity
  EOT
}

resource "kubernetes_pod" "rixi" {
  metadata {
    name      = local.pod_name
    namespace = var.namespace
    labels    = { managed_by = "rixi-gateway", "rixi/node-id" = var.node_id }
  }
  spec {
    restart_policy = "Always"
    container {
      name    = "rixi"
      image   = var.image
      command = ["bash", "-lc", local.boot_cmd]

      env {
        name  = "RIXI_GATEWAY_URL"
        value = var.gateway_ws_url
      }
      env {
        name  = "RIXI_NODE_ID"
        value = var.node_id
      }
      env {
        name  = "RIXI_TUNNEL_SECRET"
        value = var.tunnel_secret
      }
      env {
        name  = "RIXI_TUNNEL_SALT"
        value = var.kdf_salt
      }
      env {
        name  = "RIXI_SERVER_PORT"
        value = tostring(var.server_port)
      }
      env {
        name  = "RIXI_REF"
        value = var.rixi_ref
      }
      env {
        name  = "RIXI_KEY_SECRET"
        value = var.key_secret
      }
      env {
        name  = "RIXI_KEY_SECRET_USES"
        value = tostring(var.key_secret_uses)
      }
      env {
        name  = "RIXI_JWT_JWKS_URL"
        value = var.jwt_jwks_url
      }
      env {
        name  = "RIXI_REPO"
        value = var.repo
      }

      resources {
        requests = { cpu = var.cpu, memory = var.memory }
        limits   = { memory = var.memory }
      }
    }
  }

  timeouts {
    create = "5m"
  }
}

output "node_id" {
  value = var.node_id
}

output "pod_name" {
  value = kubernetes_pod.rixi.metadata[0].name
}
