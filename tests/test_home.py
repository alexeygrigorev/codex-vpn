import asyncio
from codex_vpn import home, proxy


def test_home_relays(tmp_path):
    config = tmp_path / "home.json"
    assert home.make_relays(config, "ssh") == []
    home.save(config, {"hetzner": {"host": "203.0.113.9", "user": "relay",
                                 "key": str(tmp_path / "key"), "ssh_port": 2222,
                                 "remote_port": 18900}})
    [relay] = home.make_relays(config, "ssh")
    cmd = relay.command()
    assert cmd[3:5] == ["-R", "127.0.0.1:18900:127.0.0.1:8900"]
    assert cmd[5:7] == ["-p", "2222"]
    assert cmd[-1] == "relay@203.0.113.9"
    assert "-D" not in cmd


def test_home_connect_always_exits_locally(monkeypatch):
    async def main():
        original = asyncio.open_connection
        calls = []

        async def echo(reader, writer):
            data = await reader.readexactly(4)
            writer.write(data)
            await writer.drain()
            writer.close()

        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        upstream_port = upstream.sockets[0].getsockname()[1]

        async def local_connection(host, port):
            calls.append((host, port))
            return await original("127.0.0.1", upstream_port)

        async def forbidden_socks(*args):
            raise AssertionError("home traffic must never use the GCP SOCKS tunnel")

        monkeypatch.setattr(asyncio, "open_connection", local_connection)
        monkeypatch.setattr(proxy, "socks_connect", forbidden_socks)
        server = await asyncio.start_server(proxy.Proxy(None, 0).handle, "127.0.0.1", 0)
        async with upstream, server:
            for host in ("chatgpt.com", "example.com"):
                reader, writer = await original("127.0.0.1", server.sockets[0].getsockname()[1])
                writer.write(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
                await writer.drain()
                assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
                writer.write(b"ping")
                await writer.drain()
                assert await reader.readexactly(4) == b"ping"
                writer.close()
                await writer.wait_closed()
        assert calls == [("chatgpt.com", 443), ("example.com", 443)]

    asyncio.run(main())


def test_home_failed_connection_returns_502(monkeypatch):
    async def fail(*args):
        raise ConnectionError("home connection unavailable")

    async def main():
        original = asyncio.open_connection
        monkeypatch.setattr(asyncio, "open_connection", fail)
        server = await asyncio.start_server(proxy.Proxy(None, 0).handle, "127.0.0.1", 0)
        async with server:
            reader, writer = await original("127.0.0.1", server.sockets[0].getsockname()[1])
            writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n")
            await writer.drain()
            assert (await reader.read()).startswith(b"HTTP/1.1 502")
            writer.close()
            await writer.wait_closed()

    asyncio.run(main())
