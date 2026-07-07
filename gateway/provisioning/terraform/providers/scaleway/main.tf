# Scaleway reference provider — provisions a real GPU/instance and has it dial the gateway via the
# canonical cloud-init (which fetches the open bootstrap.sh). Credentials via the standard Scaleway
# env (SCW_ACCESS_KEY / SCW_SECRET_KEY / SCW_DEFAULT_PROJECT_ID / SCW_DEFAULT_ZONE). Not exercised in
# CI — `tofu validate` only; run manually with creds. Swap this module for any other provider that
# satisfies the modules/iface contract.

terraform {
  required_providers {
    scaleway = {
      source  = "scaleway/scaleway"
      version = "~> 2.0"
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
  default = "L4-1-24G"
}
variable "region" {
  type    = string
  default = "fr-par-1"
}

# Accepted for iface compatibility (the gateway passes it to every provider), but ignored:
# Scaleway has no spot/preemptible instance product.
variable "spot" {
  type    = bool
  default = false
}
variable "image" {
  type    = string
  default = "ubuntu_jammy"
}
variable "root_volume_gb" {
  type    = number
  default = 50
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

provider "scaleway" {
  zone = var.region
}

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

resource "scaleway_instance_ip" "ip" {
  zone = var.region
}

resource "scaleway_instance_server" "rixi" {
  type  = var.instance_type
  image = var.image
  zone  = var.region
  ip_id = scaleway_instance_ip.ip.id

  root_volume {
    size_in_gb = var.root_volume_gb
  }

  user_data = {
    cloud-init = local.cloud_init
  }
}

output "node_id" {
  value = var.node_id
}

output "public_ip" {
  value = scaleway_instance_ip.ip.address
}
