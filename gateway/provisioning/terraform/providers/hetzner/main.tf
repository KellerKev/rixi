# Hetzner Cloud provider — provisions a CPU instance and has it dial the gateway via the canonical
# cloud-init (which fetches the open bootstrap.sh). Credentials via the standard HCLOUD_TOKEN env
# (project-scoped). NOTE: Hetzner Cloud has no GPU instances — use it for cheap CPU boxes, compile
# jobs, tests, and cost-tracking; GPUs come from other providers (scaleway, later lambda/runpod).
# Satisfies the modules/iface contract, so it's a drop-in alongside providers/scaleway.

terraform {
  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.45"
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
variable "instance_type" {
  type    = string
  default = "cx23" # cheapest shared-vCPU x86 (2 vCPU / 4 GB); CPU only
}
variable "region" {
  type    = string
  default = "nbg1" # Nuremberg; also fsn1 (Falkenstein), hel1 (Helsinki), ash/hil (US)
}

# Accepted for iface compatibility (the gateway passes it to every provider), but ignored:
# Hetzner Cloud has no spot/preemptible product.
variable "spot" {
  type    = bool
  default = false
}
variable "image" {
  type    = string
  default = "ubuntu-24.04"
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
variable "kdf_salt" {
  type    = string
  default = ""
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

# Token is read from HCLOUD_TOKEN in the environment (set by the gateway from the resource creds).
provider "hcloud" {}

locals {
  cloud_init = templatefile("${path.module}/cloud-init.tftpl", {
    gateway_ws_url  = var.gateway_ws_url
    node_id         = var.node_id
    tunnel_secret   = var.tunnel_secret
    server_port     = var.server_port
    rixi_ref        = var.rixi_ref
    key_secret      = var.key_secret
    key_secret_uses = var.key_secret_uses
    kdf_salt        = var.kdf_salt
    jwt_jwks_url    = var.jwt_jwks_url
    jwt_public_key  = var.jwt_public_key
  })
}

resource "hcloud_server" "rixi" {
  name        = var.node_id
  server_type = var.instance_type
  image       = var.image
  location    = var.region
  user_data   = local.cloud_init

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }

  labels = {
    managed_by = "rixi-gateway"
  }
}

output "node_id" {
  value = var.node_id
}

output "public_ip" {
  value = hcloud_server.rixi.ipv4_address
}
