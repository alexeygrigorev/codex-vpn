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
