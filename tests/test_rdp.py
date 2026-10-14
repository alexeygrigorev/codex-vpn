import time
from pathlib import Path

import pytest

import codex_vpn
from codex_vpn import proxy

IP = "203.0.113.7"
KEYGEN = Path(codex_vpn.SSH_KEYGEN)
HOSTKEY = f"{IP} ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


def test_make_relays():
    out = {"ip": IP, "ssh_user": "codexvpn"}
    assert codex_vpn.make_relays(out) == []
    assert codex_vpn.make_relays({**out, "rdp_enabled": False}) == []
    [relay] = codex_vpn.make_relays({**out, "rdp_enabled": True})
    assert relay.name == "rdp-relay"
    cmd = relay.command()
    assert cmd[3:5] == ["-R", "127.0.0.1:13389:127.0.0.1:3389"]
    assert "-D" not in cmd and cmd[-1] == f"codexvpn@{IP}"


def test_relay_status_and_proxy_status():
    tunnel = proxy.Tunnel(host=IP, user="codexvpn", key=Path("k"), known_hosts=Path("kh"), socks_port=1)
    [relay] = codex_vpn.make_relays({"ip": IP, "ssh_user": "codexvpn", "rdp_enabled": True})
    status = proxy.Proxy(tunnel, 8899, [relay]).status()
    assert status["tunnel_up"] is False
    assert status["relays"]["rdp-relay"]["up"] is False
    assert proxy.Proxy(tunnel, 8899).status()["relays"] == {}

    class Running:
        returncode = None

    relay.proc = Running()
    relay.started = time.monotonic() - 60
    assert relay.relay_status()["up"] is True
    relay.started = time.monotonic()
    assert relay.relay_status()["up"] is False  # not yet 5 s
    relay.proc.returncode = 255
    relay.started = time.monotonic() - 60
    assert relay.relay_status()["up"] is False  # exited


def test_host_key_lines(tmp_path):
    kh = tmp_path / "known_hosts"
    kh.write_text(f"{HOSTKEY}\n10.0.0.1 ssh-rsa AAAA\n[{IP}]:22 ssh-rsa BBBB\n")
    assert codex_vpn.host_key_lines(IP, kh) == [HOSTKEY, f"[{IP}]:22 ssh-rsa BBBB"]
    assert codex_vpn.host_key_lines(IP, tmp_path / "missing") == []


@pytest.mark.skipif(not KEYGEN.exists(), reason="Windows OpenSSH ssh-keygen not available")
def test_bundle_create_list_remove(tmp_path):
    clients = tmp_path / "rdp-clients"
    kh = tmp_path / "known_hosts"
    kh.write_text(f"{HOSTKEY}\n")
    b = codex_vpn.create_rdp_bundle("phone", IP, clients, kh)
    codex_vpn.create_rdp_bundle("laptop", IP, clients, tmp_path / "missing")
    files = sorted(p.name for p in b.iterdir())
    assert files == ["README.txt", "connect-rdp.cmd", "connect-rdp.sh", "id_ed25519", "id_ed25519.pub", "known_hosts"]
    pub = (b / "id_ed25519.pub").read_text()
    assert pub.startswith("ssh-ed25519 ") and pub.strip().endswith("codex-vpn-rdp-phone")
    assert (b / "known_hosts").read_bytes() == f"{HOSTKEY}\n".encode()

    cmd = (b / "connect-rdp.cmd").read_bytes().decode()
    assert "\r\n" in cmd and "-L 13389:127.0.0.1:13389" in cmd and f"rdp@{IP}" in cmd
    assert 'UserKnownHostsFile="%~dp0known_hosts"' in cmd and "StrictHostKeyChecking=yes" in cmd
    assert "mstsc /v:localhost:13389" in cmd
    sh = (b / "connect-rdp.sh").read_bytes().decode()
    assert "\r" not in sh and sh.startswith("#!/bin/sh\n") and f"rdp@{IP}" in sh
    readme = (b / "README.txt").read_text()
    assert "Termius" in readme and "localhost:13389" in readme and "SHA256:" in readme
    assert "accept-new" in (clients / "laptop" / "connect-rdp.cmd").read_text()

    keys = codex_vpn.rdp_public_keys(clients)
    assert len(keys) == 2 and keys[0].endswith("codex-vpn-rdp-laptop") and keys[1].endswith("codex-vpn-rdp-phone")

    with pytest.raises(SystemExit):
        codex_vpn.create_rdp_bundle("phone", IP, clients, kh)  # never overwrite a key
    with pytest.raises(SystemExit):
        codex_vpn.create_rdp_bundle("../evil", IP, clients, kh)
    codex_vpn.remove_rdp_bundle("phone", clients)
    assert codex_vpn.rdp_public_keys(clients) == keys[:1]
    with pytest.raises(SystemExit):
        codex_vpn.remove_rdp_bundle("phone", clients)
    assert codex_vpn.rdp_public_keys(tmp_path / "none") == []
