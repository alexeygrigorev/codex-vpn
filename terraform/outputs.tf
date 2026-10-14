output "ip" {
  value = google_compute_address.vpn.address
}

output "ssh_user" {
  value = var.ssh_user
}

output "phone_url" {
  value = var.phone_enabled ? "https://${local.phone_site}" : null
}

output "rdp_enabled" {
  value = length(var.rdp_public_keys) > 0
}
