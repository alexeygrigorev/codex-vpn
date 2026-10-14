#!/bin/bash
# Runs on every boot (and after `codex-vpn up`); idempotent.
set -euo pipefail

# The "rdp" user only relays the home PC's Remote Desktop port that the home PC
# publishes on the VM's loopback (ssh -R 127.0.0.1:13389). No shell, no other ports.
cat > /etc/ssh/sshd_config.d/60-codex-vpn.conf <<'CONF'
ClientAliveInterval 30
ClientAliveCountMax 3

Match User rdp
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:13389
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
    ForceCommand /usr/sbin/nologin
CONF

# Keep the forward bound to loopback only.
grep -q '^GatewayPorts no' /etc/ssh/sshd_config.d/60-codex-vpn.conf || echo 'GatewayPorts no' >> /etc/ssh/sshd_config.d/60-codex-vpn.conf
sshd -t && systemctl reload ssh
