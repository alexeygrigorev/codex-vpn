#!/bin/bash
# Runs on every boot (and after `codex-vpn up`); idempotent.
set -euo pipefail

# The "rdp" user only relays the home PC's Remote Desktop port that the home PC
# publishes on the VM's loopback (ssh -R 127.0.0.1:13389). No shell, no other ports.
cat > /etc/ssh/sshd_config.d/60-codex-vpn.conf <<'CONF'
ClientAliveInterval 30
ClientAliveCountMax 3
# Remote forwards (ssh -R) stay bound to the VM's loopback.
GatewayPorts no

Match User rdp
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:13389
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
    ForceCommand /usr/sbin/nologin
CONF

sshd -t && systemctl reload ssh

# The guest agent puts every metadata ssh-keys user into google-sudoers. The rdp user
# can't run commands anyway (nologin), but deny sudo explicitly too. "zz-" sorts last,
# so this rule wins over /etc/sudoers.d/google_sudoers.
echo 'rdp ALL=(ALL:ALL) !ALL' > /etc/sudoers.d/zz-codex-vpn-rdp
chmod 0440 /etc/sudoers.d/zz-codex-vpn-rdp
visudo -cf /etc/sudoers.d/zz-codex-vpn-rdp >/dev/null || rm -f /etc/sudoers.d/zz-codex-vpn-rdp

# ---- Phone mirror: Caddy (HTTPS + password) in front of the home PC's ws-scrcpy, which the
# home PC publishes on the VM's loopback (ssh -R 127.0.0.1:18000). Settings come from instance
# metadata written by terraform (phone-*).
meta() {
    curl -sf -H 'Metadata-Flavor: Google' \
        "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1" || true
}

setup_phone() {
local PHONE_SITE PHONE_USER PHONE_HASH GLOBAL TLS
if [ "$(meta phone-enabled)" != "true" ]; then
    if systemctl list-unit-files caddy.service >/dev/null 2>&1; then
        systemctl disable --now caddy || true
    fi
    return 0
fi

PHONE_SITE=$(meta phone-site)
PHONE_USER=$(meta phone-auth-user)
PHONE_HASH=$(meta phone-auth-hash)
# Refuse anything that could inject Caddyfile syntax.
[[ "$PHONE_SITE" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$|^[0-9-]+\.sslip\.io$ ]] || { echo "bad phone-site"; return 1; }
[[ "$PHONE_USER" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "bad phone-auth-user"; return 1; }
[[ "$PHONE_HASH" =~ ^\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}$ ]] || { echo "bad phone-auth-hash"; return 1; }

# Let's Encrypt IP-address certificates (short-lived profile) need a recent Caddy; install or
# upgrade from the official apt repo only when needed, so normal boots stay fast.
if ! dpkg --compare-versions "$(dpkg-query -W -f='${Version}' caddy 2>/dev/null || echo 0)" ge 2.11.0; then
    apt-get update -q
    apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https curl gnupg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
        | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -q
    apt-get install -y -q caddy
fi

if [[ "$PHONE_SITE" == *.sslip.io ]]; then
    # Hostname: ordinary 90-day certificate, browsers send SNI.
    GLOBAL=""
    TLS=""
else
    # Bare IP: Let's Encrypt only issues IP certificates with the 6-day "shortlived" profile
    # (Caddy renews them automatically). Browsers send no SNI for an IP, and on GCP the VM only
    # sees its internal address, so tell Caddy which certificate to use for SNI-less handshakes.
    GLOBAL="default_sni $PHONE_SITE"
    TLS="tls {
		issuer acme {
			profile shortlived
		}
	}"
fi

cat > /etc/caddy/Caddyfile.new <<CADDY
# Written by codex-vpn startup.sh; edits are overwritten on boot.
{
	$GLOBAL
}

$PHONE_SITE {
	$TLS
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options nosniff
		X-Frame-Options DENY
		Referrer-Policy no-referrer
		Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()"
		Cache-Control no-store
		-Server
	}
	basic_auth {
		$PHONE_USER $PHONE_HASH
	}
	# ws-scrcpy (HTTP + WebSockets for video and input), relayed from the home PC.
	reverse_proxy 127.0.0.1:18000
}
CADDY
caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile.new
if ! cmp -s /etc/caddy/Caddyfile.new /etc/caddy/Caddyfile; then
    mv /etc/caddy/Caddyfile.new /etc/caddy/Caddyfile
    systemctl enable caddy
    systemctl restart caddy
else
    rm /etc/caddy/Caddyfile.new
    systemctl enable --now caddy
fi
}
setup_phone
