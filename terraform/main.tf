terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project
  region  = var.region
  zone    = var.zone
}

resource "google_compute_address" "vpn" {
  name         = "${var.name}-ip"
  region       = var.region
  network_tier = "PREMIUM"
}

# The tunnel is plain SSH with key-only auth (GCP images disable password login).
resource "google_compute_firewall" "ssh" {
  name          = "${var.name}-ssh"
  network       = "default"
  direction     = "INGRESS"
  source_ranges = var.ssh_source_ranges
  target_tags   = [var.name]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

locals {
  ip         = google_compute_address.vpn.address
  phone_site = var.phone_use_sslip ? "${replace(local.ip, ".", "-")}.sslip.io" : local.ip
}

# Phone mirror: Caddy serves HTTPS on 443 (behind a password) and needs 80 for the
# Let's Encrypt HTTP-01 challenge and the redirect to HTTPS.
resource "google_compute_firewall" "phone" {
  count         = var.phone_enabled ? 1 : 0
  name          = "${var.name}-phone"
  network       = "default"
  direction     = "INGRESS"
  source_ranges = ["0.0.0.0/0"]
  target_tags   = [var.name]

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
}

resource "google_compute_instance" "vpn" {
  name         = var.name
  machine_type = var.machine_type
  zone         = var.zone
  tags         = [var.name]

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 10
      type  = "pd-standard"
    }
  }

  network_interface {
    network = "default"
    access_config {
      nat_ip       = google_compute_address.vpn.address
      network_tier = "PREMIUM"
    }
  }

  metadata = {
    ssh-keys = join("\n", concat(
      ["${var.ssh_user}:${var.ssh_public_key}"],
      [for k in var.rdp_public_keys : "rdp:${k}"],
    ))
    block-project-ssh-keys = "true"
    startup-script         = file("${path.module}/startup.sh")
    # Read by startup.sh to configure Caddy; phone-enabled=false stops it.
    phone-enabled   = tostring(var.phone_enabled && var.phone_auth_hash != "")
    phone-site      = local.phone_site
    phone-auth-user = var.phone_auth_user
    phone-auth-hash = var.phone_auth_hash
  }

  scheduling {
    automatic_restart   = true
    on_host_maintenance = "MIGRATE"
  }
}
