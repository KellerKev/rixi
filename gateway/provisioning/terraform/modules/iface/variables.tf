# Standard variable interface every rixi provider module accepts. Provider modules
# (providers/<name>) implement this contract; the tofu driver passes these in a
# terraform.tfvars.json. Keeping the contract here makes providers swappable.

variable "gateway_ws_url" {
  type        = string
  description = "WebSocket URL the provisioned server dials back to (the gateway)."
}

variable "node_id" {
  type        = string
  description = "One-time registration token; the server presents it as its tunnel node_id."
}

variable "tunnel_secret" {
  type        = string
  sensitive   = true
  description = "Shared tunnel secret (the gateway's). Channel encryption + auth."
}

variable "server_port" {
  type        = number
  default     = 9000
  description = "Loopback port the rixi server binds on the box."
}

variable "rixi_ref" {
  type        = string
  default     = "main"
  description = "git ref of the public rixi repo to install via bootstrap.sh."
}

variable "instance_type" {
  type        = string
  default     = ""
  description = "Provider-specific instance/GPU type."
}

variable "region" {
  type        = string
  default     = ""
  description = "Provider region/zone."
}

variable "spot" {
  type        = bool
  default     = false
  description = "Request cheaper interruptible (spot/preemptible) capacity. Providers that support it (e.g. a future AWS/GCP module: instance_market_options / provisioning_model = \"SPOT\") consume this; the gateway falls back to on-demand on a capacity error and re-provisions on preemption. Providers without a spot product accept-and-ignore it."
}

variable "key_secret" {
  type        = string
  default     = ""
  sensitive   = true
  description = "rixi server handshake secret (env RIXI_KEY_SECRET). Empty = open mode; set = the box runs with the client↔server AES key handshake enabled (true end-to-end through the gateway)."
}

variable "key_secret_uses" {
  type        = number
  default     = 0
  description = "Max successful handshakes for key_secret (0 = unlimited; needed when several clients reuse one warm box)."
}

variable "kdf_salt" {
  type        = string
  default     = ""
  description = "Per-deployment tunnel KDF salt (v2 crypto). MUST match the gateway's --kdf-salt, or the box's tunnel derives different keys and cannot authenticate. Reaches the box as RIXI_TUNNEL_SALT."
}

variable "jwt_jwks_url" {
  type        = string
  default     = ""
  description = "If set, the box runs the rixi server with --jwks-url so it requires a valid JWT (enforced auth)."
}

variable "jwt_public_key" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Alternative to jwt_jwks_url: a PEM public key the box uses for --public-key (air-gapped; multi-line is fragile in cloud-init — prefer jwt_jwks_url)."
}
