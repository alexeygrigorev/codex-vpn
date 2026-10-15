"""Configuration for private reverse relays to the home's HTTP proxy."""

import json
from pathlib import Path

from codex_vpn import proxy

PORT = 8900
REMOTE_PORT = 18900


def settings(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def make_relays(path: Path, ssh: str) -> list[proxy.Tunnel]:
    return [proxy.Tunnel(
        name=f"home-{name}", host=target["host"], user=target["user"],
        key=Path(target["key"]), known_hosts=path.parent / "home_known_hosts",
        socks_port=0, ssh=ssh, ssh_port=target["ssh_port"],
        forwards=["-R", f"127.0.0.1:{target['remote_port']}:127.0.0.1:{PORT}"],
    ) for name, target in sorted(settings(path).items())]
