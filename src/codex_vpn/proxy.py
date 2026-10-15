"""Local HTTP proxy that sends OpenAI traffic through an SSH tunnel to the US VM.

- Listens on 127.0.0.1:<port> for HTTP CONNECT and plain HTTP proxy requests.
- Hosts matching TUNNEL_DOMAINS go through `ssh -D` (SOCKS5) to the VM; everything else goes direct.
- Fail-closed: if the tunnel is down, tunneled hosts get 502 instead of leaking out directly.
- Serves /proxy.pac and /status on the same port.
- Keeps the ssh process alive and checks the exit location every CHECK_INTERVAL seconds;
  if the check keeps failing for RESTART_AFTER seconds, restarts ssh (it can hang while still running).
- POST /restart (with header X-Codex-Vpn: restart) restarts the tunnel's ssh on demand.
"""

import asyncio
import json
import logging
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("codex_vpn")

TUNNEL_DOMAINS = (
    "openai.com",
    "chatgpt.com",
    "oaistatic.com",
    "oaiusercontent.com",
)
CHECK_INTERVAL = 30
RESTART_AFTER = 60  # restart ssh when the tunnel check has failed this long
RELAY_UP_AFTER = 5  # a relay counts as up once its ssh has run this long without exiting
MAX_HEADER = 64 * 1024


def is_tunneled(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in TUNNEL_DOMAINS)


def pac_script(port: int) -> str:
    checks = " ||\n      ".join(f'dnsDomainIs(host, "{d}") || host == "{d}"' for d in TUNNEL_DOMAINS)
    return (
        "function FindProxyForURL(url, host) {\n"
        f"  if ({checks})\n"
        f'    return "PROXY 127.0.0.1:{port}";\n'
        '  return "DIRECT";\n'
        "}\n"
    )


@dataclass
class Tunnel:
    host: str
    user: str
    key: Path
    known_hosts: Path
    socks_port: int
    ssh: str = r"C:\Windows\System32\OpenSSH\ssh.exe"
    name: str = "ssh"
    forwards: list[str] = field(default_factory=list)
    ssh_port: int = 22
    up: bool = False
    exit_info: dict = field(default_factory=dict)
    last_check: float = 0.0
    last_error: str = ""
    restarts: int = 0
    started: float = 0.0
    proc: asyncio.subprocess.Process | None = None

    def command(self) -> list[str]:
        forwards = self.forwards or ["-D", f"127.0.0.1:{self.socks_port}"]
        return [
            self.ssh, "-N", "-T",
            *forwards,
            "-p", str(self.ssh_port),
            "-i", str(self.key),
            "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            f"{self.user}@{self.host}",
        ]

    async def supervise(self) -> None:
        delay = 1
        while True:
            started = self.started = time.monotonic()
            log.info("%s: starting %s to %s@%s", self.name, " ".join(self.command()[3:-1][:2]), self.user, self.host)
            self.proc = await asyncio.create_subprocess_exec(
                *self.command(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            assert self.proc.stderr
            async for line in self.proc.stderr:
                text = line.decode(errors="replace").strip()
                if text:
                    self.last_error = text
                    log.warning("%s: %s", self.name, text)
            code = await self.proc.wait()
            self.up = False
            self.restarts += 1
            log.error("%s: exited with code %s", self.name, code)
            delay = 1 if time.monotonic() - started > 60 else min(delay * 2, 30)
            await asyncio.sleep(delay)

    def restart(self, why: str) -> bool:
        """Kill the ssh process; supervise() starts a new one."""
        if self.proc is None or self.proc.returncode is not None:
            return False
        log.warning("%s: restarting (%s)", self.name, why)
        self.proc.kill()
        return True

    async def check(self) -> None:
        """Fetch Cloudflare's trace through the tunnel to confirm it works and where it exits."""
        failing_since: float | None = None
        while True:
            try:
                reader, writer = await asyncio.wait_for(socks_connect(self.socks_port, "chatgpt.com", 80), 10)
                writer.write(b"GET /cdn-cgi/trace HTTP/1.1\r\nHost: chatgpt.com\r\nConnection: close\r\n\r\n")
                body = (await asyncio.wait_for(reader.read(), 10)).decode(errors="replace")
                writer.close()
                info = dict(line.split("=", 1) for line in body.split("\r\n\r\n", 1)[-1].splitlines() if "=" in line)
                self.exit_info = {k: info.get(k) for k in ("ip", "loc", "colo")}
                if not self.up:
                    log.info("tunnel: up, exit %s", self.exit_info)
                self.up = True
                failing_since = None
            except Exception as e:
                if self.up:
                    log.error("tunnel: check failed: %r", e)
                self.up = False
                self.last_error = repr(e)
                now = time.monotonic()
                failing_since = failing_since or now
                if now - failing_since >= RESTART_AFTER and self.restart(f"check failing for {now - failing_since:.0f}s"):
                    failing_since = now
            self.last_check = time.time()
            await asyncio.sleep(CHECK_INTERVAL if self.up else 5)

    def relay_status(self) -> dict:
        """Relays have no check loop: "up" means the ssh process has been running for RELAY_UP_AFTER seconds."""
        running = self.proc is not None and self.proc.returncode is None
        return {
            "up": running and time.monotonic() - self.started > RELAY_UP_AFTER,
            "forwards": " ".join(self.forwards),
            "restarts": self.restarts,
            "last_error": self.last_error,
        }

    def status(self) -> dict:
        return {
            "tunnel_up": self.up,
            "vm": f"{self.user}@{self.host}",
            "exit": self.exit_info,
            "last_check": time.strftime("%H:%M:%S", time.localtime(self.last_check)) if self.last_check else None,
            "ssh_restarts": self.restarts,
            "last_error": self.last_error,
            "tunnel_domains": list(TUNNEL_DOMAINS),
        }


async def socks_connect(socks_port: int, host: str, port: int):
    reader, writer = await asyncio.open_connection("127.0.0.1", socks_port)
    writer.write(b"\x05\x01\x00")
    if await reader.readexactly(2) != b"\x05\x00":
        raise ConnectionError("socks: auth negotiation failed")
    name = host.encode("idna")
    writer.write(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack(">H", port))
    reply = await reader.readexactly(4)
    if reply[1] != 0:
        raise ConnectionError(f"socks: connect to {host}:{port} failed with code {reply[1]}")
    atyp = reply[3]
    skip = {1: 4, 4: 16}.get(atyp)
    if skip is None:
        skip = (await reader.readexactly(1))[0]
    await reader.readexactly(skip + 2)
    return reader, writer


async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> int:
    total = 0
    try:
        while data := await src.read(65536):
            total += len(data)
            dst.write(data)
            await dst.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass
    return total


class Proxy:
    def __init__(self, tunnel: Tunnel | None, port: int, relays: list[Tunnel] = ()):
        self.tunnel = tunnel
        self.port = port
        self.relays = list(relays)

    def status(self) -> dict:
        info = self.tunnel.status() if self.tunnel else {"mode": "home-exit"}
        return {**info, "relays": {r.name: r.relay_status() for r in self.relays}}

    async def respond(self, writer, status: str, body: str, ctype: str = "text/plain") -> None:
        data = body.encode()
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(data)}\r\n"
            "Connection: close\r\n\r\n".encode() + data
        )
        await writer.drain()
        writer.close()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        request_line, _, rest = head.partition(b"\r\n")
        try:
            method, target, version = request_line.decode("latin-1").split(" ", 2)
        except ValueError:
            await self.respond(writer, "400 Bad Request", "bad request line\n")
            return

        if target.startswith("/"):
            if target.startswith("/proxy.pac"):
                script = pac_script(self.port) if self.tunnel else (
                    f'function FindProxyForURL(url, host) {{ return "PROXY 127.0.0.1:{self.port}"; }}\n'
                )
                await self.respond(writer, "200 OK", script, "application/x-ns-proxy-autoconfig")
            elif target.startswith("/restart") and method == "POST":
                if self.tunnel is None:
                    await self.respond(writer, "404 Not Found", "home proxy has no outgoing tunnel\n")
                    return
                # The custom header keeps web pages from triggering this (it would need a CORS preflight).
                if b"\r\nx-codex-vpn: restart" not in head.lower():
                    await self.respond(writer, "403 Forbidden", "missing X-Codex-Vpn: restart header\n")
                elif self.tunnel.restart("requested via /restart"):
                    await self.respond(writer, "200 OK", "tunnel ssh restarting\n")
                else:
                    await self.respond(writer, "409 Conflict", "tunnel ssh is not running (already restarting)\n")
            elif target.startswith("/status"):
                await self.respond(writer, "200 OK", json.dumps(self.status(), indent=2) + "\n", "application/json")
            else:
                await self.respond(writer, "404 Not Found", "try /status or /proxy.pac\n")
            return

        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            port = int(port or 443)
            forward = b""
        else:
            url = urlsplit(target)
            host, port = url.hostname or "", url.port or 80
            path = (url.path or "/") + (f"?{url.query}" if url.query else "")
            headers = [h for h in rest.split(b"\r\n") if h and not h.lower().startswith((b"proxy-connection", b"connection"))]
            forward = f"{method} {path} {version}\r\n".encode() + b"\r\n".join(headers) + b"\r\nConnection: close\r\n\r\n"
        host = host.strip("[]")

        route = "tunnel" if self.tunnel and is_tunneled(host) else "direct"
        started = time.monotonic()
        try:
            if route == "tunnel":
                assert self.tunnel is not None
                # Always try the tunnel, even when the last check failed: a check can time out while
                # the tunnel still works. Tunneled hosts never fall back to direct, so this stays fail-closed.
                up_reader, up_writer = await asyncio.wait_for(socks_connect(self.tunnel.socks_port, host, port), 15)
            else:
                up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(host, port), 15)
        except Exception as e:
            log.warning("%-6s %s:%s FAILED %r", route, host, port, e)
            await self.respond(writer, "502 Bad Gateway", f"codex-vpn: {route} {host}:{port} failed: {e!r}\n")
            return

        if method == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        else:
            up_writer.write(forward)

        sent, received = await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
        level = logging.INFO if route == "tunnel" or self.tunnel is None else logging.DEBUG
        log.log(level, "%-6s %s:%s up=%d down=%d %.1fs", route, host, port, sent, received, time.monotonic() - started)


def ignore_proactor_resets(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Windows' proactor loop logs a traceback when a peer resets a socket that is already
    closing (ConnectionResetError in _call_connection_lost). It is harmless; keep the log readable."""
    if isinstance(context.get("exception"), ConnectionResetError):
        return
    loop.default_exception_handler(context)


async def serve(tunnel: Tunnel, port: int, relays: list[Tunnel] = ()) -> None:
    """Run the proxy, its tunnel, and any extra relays (separate ssh processes, so a relay
    failure never takes the Codex tunnel down)."""
    proxy = Proxy(tunnel, port, relays)
    asyncio.get_running_loop().set_exception_handler(ignore_proactor_resets)
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", port, limit=MAX_HEADER)
    log.info("proxy: listening on 127.0.0.1:%d, tunneling %s", port, ", ".join(TUNNEL_DOMAINS))
    async with server:
        await asyncio.gather(
            server.serve_forever(), tunnel.supervise(), tunnel.check(), *(r.supervise() for r in relays)
        )


async def serve_home(port: int, relays: list[Tunnel]) -> None:
    """A separate proxy whose outgoing connections always originate on this PC."""
    home = Proxy(None, port, relays)
    server = await asyncio.start_server(home.handle, "127.0.0.1", port, limit=MAX_HEADER)
    log.info("home proxy: listening on 127.0.0.1:%d", port)
    async with server:
        await asyncio.gather(server.serve_forever(), *(r.supervise() for r in relays))
