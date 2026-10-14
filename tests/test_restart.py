import asyncio
from pathlib import Path

from codex_vpn import proxy


class FakeProc:
    returncode = None

    def kill(self):
        self.returncode = 1


async def request(port: int, raw: bytes) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    data = await reader.read()
    writer.close()
    return data.decode()


def test_restart_endpoint():
    async def main():
        tunnel = proxy.Tunnel(host="203.0.113.7", user="u", key=Path("k"), known_hosts=Path("kh"), socks_port=1)
        assert tunnel.restart("no process") is False
        p = proxy.Proxy(tunnel, 0)
        server = await asyncio.start_server(p.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            tunnel.proc = FakeProc()
            no_header = await request(port, b"POST /restart HTTP/1.1\r\nHost: x\r\n\r\n")
            assert no_header.startswith("HTTP/1.1 403") and tunnel.proc.returncode is None
            ok = await request(port, b"POST /restart HTTP/1.1\r\nHost: x\r\nX-Codex-Vpn: restart\r\n\r\n")
            assert ok.startswith("HTTP/1.1 200") and tunnel.proc.returncode == 1
            again = await request(port, b"POST /restart HTTP/1.1\r\nHost: x\r\nX-Codex-Vpn: restart\r\n\r\n")
            assert again.startswith("HTTP/1.1 409")

    asyncio.run(main())
