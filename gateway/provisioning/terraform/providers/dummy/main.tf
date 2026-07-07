# Dummy provider — a REAL OpenTofu module that provisions nothing in the cloud. It stands up, on
# the local machine, a tiny echo "server" + the open `rixi-tunnel connect` dialing the gateway with
# the one-time token. This exercises the whole orchestration (claim → tofu apply → server dials in
# → gateway bridges client) end-to-end, offline and free. `destroy` tears the processes down.

# Uses the built-in terraform_data resource (no external provider) so `tofu init` needs no network.

variable "gateway_ws_url" { type = string }
variable "node_id" { type = string }
variable "rixi_dir" { type = string }
variable "tunnel_secret" {
  type      = string
  sensitive = true
}
variable "python_bin" {
  type    = string
  default = "python3"
}
variable "kdf_salt" {
  type    = string
  default = ""
}
# Spot / preemptible simulation: when spot=true and spot_ttl>0, the dummy box self-terminates after
# spot_ttl seconds (its tunnel drops), so the gateway's interruption handler can be exercised
# locally and deterministically. A real cloud module would instead request actual spot capacity.
variable "spot" {
  type    = bool
  default = false
}
variable "spot_ttl" {
  type    = number
  default = 0
}

resource "terraform_data" "box" {
  triggers_replace = [var.node_id]

  provisioner "local-exec" {
    command = "bash ${path.module}/provision.sh"
    environment = {
      PYTHON      = var.python_bin
      RIXI_DIR    = var.rixi_dir
      GATEWAY_WS  = var.gateway_ws_url
      NODE_ID     = var.node_id
      SECRET      = var.tunnel_secret
      SALT        = var.kdf_salt
      SPOT        = var.spot ? "true" : "false"
      SPOT_TTL    = tostring(var.spot_ttl)
    }
  }

  provisioner "local-exec" {
    when    = destroy
    command = "bash ${path.module}/destroy.sh"
  }
}

output "node_id" {
  value = var.node_id
}
