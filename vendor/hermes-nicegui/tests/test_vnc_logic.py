"""Pure-logic tests for the VNC plugin: tokens, the WS<->TCP bridge, and the
state/password file helpers. No NiceGUI page or browser involved."""

from __future__ import annotations

import asyncio
import time

from hermes_nicegui.auth import is_public_path
from hermes_nicegui.plugins.vnc.logic import (
    TokenManager,
    clear_help_requested,
    pipe_ws_tcp,
    read_password,
    read_state,
    write_state,
)

# -- TokenManager -------------------------------------------------------------


def test_token_single_use() -> None:
    manager = TokenManager()
    token = manager.mint()
    assert token
    assert manager.consume(token) is True
    assert manager.consume(token) is False


def test_consume_rejects_unknown_and_empty() -> None:
    manager = TokenManager()
    assert manager.consume("bogus") is False
    assert manager.consume("") is False


def test_token_expires() -> None:
    manager = TokenManager(ttl=0.01)
    token = manager.mint()
    time.sleep(0.02)
    assert manager.consume(token) is False


def test_expired_tokens_do_not_grow_memory() -> None:
    manager = TokenManager(ttl=0.01)
    manager.mint()
    time.sleep(0.02)
    manager.mint()
    assert len(manager) <= 1


def test_max_tokens_evicts_oldest() -> None:
    manager = TokenManager(max_tokens=4)
    for _ in range(9):
        manager.mint()
    assert len(manager) == 4


def test_vnc_routes_are_not_public() -> None:
    assert is_public_path("/vnc/token") is False
    assert is_public_path("/vnc/ws") is False


# -- state / password helpers --------------------------------------------------


def test_read_state_missing(tmp_path) -> None:
    assert read_state(tmp_path / "state.json") == {}
    clear_help_requested(tmp_path / "state.json")  # must be a no-op


def test_write_and_read_state(tmp_path) -> None:
    path = tmp_path / "state.json"
    write_state(path, {"help_requested": True, "message": "x"})
    assert read_state(path) == {"help_requested": True, "message": "x"}


def test_clear_help_requested_keeps_other_keys(tmp_path) -> None:
    path = tmp_path / "state.json"
    write_state(path, {"help_requested": True, "message": "x", "url": "https://example.com"})
    clear_help_requested(path)
    data = read_state(path)
    assert data["help_requested"] is False
    assert data["message"] == "x"
    assert data["url"] == "https://example.com"


def test_read_password_missing(tmp_path) -> None:
    assert read_password(tmp_path / "passwd") is None


def test_read_password_strips(tmp_path) -> None:
    path = tmp_path / "passwd"
    path.write_text("abc\n")
    assert read_password(path) == "abc"


# -- pipe_ws_tcp bridge -------------------------------------------------------


class FakeWS:
    """Duck-typed stand-in for ``starlette.websockets.WebSocket``."""

    def __init__(self, queue: asyncio.Queue, raise_on_receive: bool = False) -> None:
        self.queue = queue
        self.raise_on_receive = raise_on_receive
        self.sent: list[bytes] = []
        self.closed = False

    async def receive_bytes(self) -> bytes:
        if self.raise_on_receive:
            raise ConnectionError("ws closed")
        data = await self.queue.get()
        if data is None:
            raise ConnectionError("ws closed")
        return data

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


async def test_pipe_ws_tcp_forwards_client_bytes_to_tcp() -> None:
    received: list[bytes] = []

    async def handler(reader, writer) -> None:
        data = await reader.read(65536)
        received.append(data)
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(b"hello")
        fake = FakeWS(queue)
        await asyncio.wait_for(pipe_ws_tcp(fake, "127.0.0.1", port), 5)
        assert received == [b"hello"]
    finally:
        server.close()
        await server.wait_closed()


async def test_pipe_ws_tcp_forwards_tcp_bytes_to_ws() -> None:
    async def handler(reader, writer) -> None:
        writer.write(b"RFB 003.008\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        fake = FakeWS(asyncio.Queue())
        await asyncio.wait_for(pipe_ws_tcp(fake, "127.0.0.1", port), 5)
        assert fake.sent == [b"RFB 003.008\n"]
        assert fake.closed
    finally:
        server.close()
        await server.wait_closed()


async def test_pipe_ws_tcp_tcp_eof_closes_ws() -> None:
    async def handler(reader, writer) -> None:
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        fake = FakeWS(asyncio.Queue())
        await asyncio.wait_for(pipe_ws_tcp(fake, "127.0.0.1", port), 5)
        assert fake.closed
    finally:
        server.close()
        await server.wait_closed()


async def test_pipe_ws_tcp_ws_eof_closes_tcp() -> None:
    tcp_eof = asyncio.Event()

    async def handler(reader, writer) -> None:
        await reader.read(65536)  # returns b"" once the bridge closes its writer
        tcp_eof.set()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        fake = FakeWS(asyncio.Queue(), raise_on_receive=True)
        await asyncio.wait_for(pipe_ws_tcp(fake, "127.0.0.1", port), 5)
        await asyncio.wait_for(tcp_eof.wait(), 2)
    finally:
        server.close()
        await server.wait_closed()
