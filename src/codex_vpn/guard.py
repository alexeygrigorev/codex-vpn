"""Guard: Codex must never talk to OpenAI without the tunnel.

Every POLL seconds:
- if the proxy does not answer, start it and close Codex right away
  (PAC-based apps fall back to direct when the proxy is gone);
- if the proxy is up but the tunnel has been down for GRACE seconds, close Codex;
- if a codex.exe process holds a direct (non-loopback) TCP connection, it is
  bypassing the proxy (e.g. started without HTTPS_PROXY) - close it.
"""

import json
import logging
import subprocess
import time
import urllib.request
from pathlib import Path

import psutil

log = logging.getLogger("codex_vpn.guard")

POLL = 2
GRACE = 6
RESTART_EVERY = 15
NOTICE_EVERY = 30

# Executables that make up Codex (desktop app, its app-server, the npm CLI).
CODEX_NAMES = {"chatgpt.exe", "codex.exe"}
CODEX_PATH_MARKERS = ("\\windowsapps\\openai.codex_", "\\openai\\codex\\", "@openai\\codex")
# Processes whose own traffic must always go through the proxy (they honor HTTPS_PROXY).
PROXY_ONLY_NAMES = {"codex.exe"}
LOOPBACK = ("127.", "::1")


def codex_processes() -> list[psutil.Process]:
    found = []
    for p in psutil.process_iter(["name", "exe"]):
        name = (p.info["name"] or "").lower()
        exe = (p.info["exe"] or "").lower()
        if name in CODEX_NAMES and any(m in exe for m in CODEX_PATH_MARKERS):
            found.append(p)
    return found


def direct_connections(p: psutil.Process) -> list[str]:
    try:
        conns = p.net_connections(kind="inet")
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []
    return [
        f"{c.raddr.ip}:{c.raddr.port}"
        for c in conns
        if c.raddr and c.status == psutil.CONN_ESTABLISHED and not c.raddr.ip.startswith(LOOPBACK)
    ]


def kill(procs: list[psutil.Process]) -> int:
    killed = 0
    for p in procs:
        try:
            p.kill()
            killed += 1
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            log.error("cannot kill pid %s (access denied)", p.pid)
    return killed


def notify(message: str) -> None:
    script = f"(New-Object -ComObject WScript.Shell).Popup('{message}', 15, 'codex-vpn guard', 48)"
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def proxy_status(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def run(status_url: str, start_proxy, dry_run: bool = False) -> None:
    log.info("guard: started (dry_run=%s)", dry_run)
    down_since: float | None = None
    last_restart = 0.0
    last_notice = 0.0

    while True:
        now = time.monotonic()
        status = proxy_status(status_url)
        if status is None and now - last_restart > RESTART_EVERY:
            log.warning("guard: proxy not answering, starting it")
            if not dry_run:
                start_proxy()
            last_restart = now

        healthy = bool(status and status.get("tunnel_up"))
        down_since = None if healthy else (down_since or now)
        procs = codex_processes()

        reason = ""
        victims: list[psutil.Process] = []
        if procs and status is None:
            # No proxy at all: PAC-based apps would silently fall back to direct.
            reason = "the codex-vpn proxy is not running"
            victims = procs
        elif procs and down_since is not None and now - down_since >= GRACE:
            # Proxy up but tunnel down: requests already fail closed (502); close after a grace period.
            reason = "the codex-vpn tunnel is down"
            victims = procs
        else:
            for p in procs:
                if p.info["name"].lower() in PROXY_ONLY_NAMES:
                    direct = direct_connections(p)
                    if direct:
                        log.warning("guard: pid %s %s bypasses the proxy: %s", p.pid, p.info["exe"], ", ".join(direct[:5]))
                        victims.append(p)
            if victims:
                reason = "a Codex process bypassed the proxy (restart it from a new terminal)"

        if victims:
            log.warning("guard: closing %d Codex process(es): %s", len(victims), reason)
            if not dry_run:
                kill(victims)
                if now - last_notice > NOTICE_EVERY:
                    notify(f"Codex was closed because {reason}. Check: uv run codex-vpn status")
                    last_notice = now
        time.sleep(POLL)
