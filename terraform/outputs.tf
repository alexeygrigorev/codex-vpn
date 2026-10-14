output "ip" {
  value = google_compute_address.vpn.address
}

output "ssh_user" {
  value = var.ssh_user
}
