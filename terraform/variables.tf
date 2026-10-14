variable "project" {
  type        = string
  description = "GCP project id."
}

variable "region" {
  type        = string
  default     = "us-east1"
  description = "Free-tier e2-micro regions: us-west1, us-central1, us-east1."
}

variable "zone" {
  type    = string
  default = "us-east1-b"
}

variable "name" {
  type    = string
  default = "codex-vpn"
}

variable "machine_type" {
  type    = string
  default = "e2-micro"
}

variable "ssh_user" {
  type    = string
  default = "codexvpn"
}

variable "ssh_public_key" {
  type        = string
  description = "OpenSSH public key allowed to open the tunnel."
}

variable "ssh_source_ranges" {
  type    = list(string)
  default = ["0.0.0.0/0"]
}

variable "rdp_public_keys" {
  type        = list(string)
  default     = []
  description = "OpenSSH public keys (one per device) allowed to reach the home PC's Remote Desktop relay."
}

variable "phone_enabled" {
  type        = bool
  default     = false
  description = "Serve the home PC's phone mirror (ws-scrcpy, relayed to the VM's 127.0.0.1:18000) over HTTPS with Caddy."
}

variable "phone_auth_user" {
  type    = string
  default = "phone"

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]+$", var.phone_auth_user))
    error_message = "phone_auth_user may only contain letters, digits, _ and -."
  }
}

variable "phone_auth_hash" {
  type        = string
  default     = ""
  sensitive   = true
  description = "bcrypt hash of the phone mirror password (the password itself never leaves the PC)."

  validation {
    condition     = var.phone_auth_hash == "" || can(regex("^\\$2[aby]\\$[0-9]{2}\\$[./A-Za-z0-9]{53}$", var.phone_auth_hash))
    error_message = "phone_auth_hash must be a bcrypt hash."
  }
}

variable "phone_use_sslip" {
  type        = bool
  default     = false
  description = "Fallback: serve on <ip-with-dashes>.sslip.io (regular 90-day cert) instead of the bare IP (Let's Encrypt short-lived IP cert)."
}
