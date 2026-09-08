"""VNC bridge logic: single-use tokens, the WS<->TCP pipe, state helpers.

No NiceGUI page code lives here -- everything is unit-testable without a
browser (``TokenManager``, ``pipe_ws_tcp``, ``display_online``, the state
helpers), and the two raw routes that *can't* be a NiceGUI page are registered
here against the FastAPI app: the authenticated ``/vnc/token`` POST and the
``/vnc/ws`` websocket, in the same style ``plugins/files/ui.py`` registers its
raw ``@app.get`` routes.

Security model (see the module docstring in ``__init__.py``): ``/vnc`` pages
and the ``/vnc/token`` POST are gated by ``AuthMiddleware`` like every other
HTTP path. The websocket is *not* covered by that middleware (Starlette's
``BaseHTTPMiddleware`` doesn't wrap websockets), so it validates a single-use,
60-second token itself: the client sends ``{"token": "<token>"}`` as its first
frame, the server consumes it once, and anything invalid closes the socket
with code 1008. The token and the VNC password never appear in a URL or in
any log line.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse
from nicegui import app
from starlette.websockets import WebSocket

if TYPE_CHECKING:
    from hermes_nicegui.plugins.vnc import VncPlugin


class TokenManager:
    """In-memory, single-use, TTL tokens authorizing the ``/vnc/ws`` bridge.

    A token is minted by the authenticated ``/vnc/token`` POST, sent as the
    websocket's first frame, and can be consumed exactly once before it
    expires. ``secrets.compare_digest`` keeps the key search constant-time.
    """

    def __init__(self, ttl: float = 60.0, max_tokens: int = 64) -> None:
        self.ttl = ttl
        self.max_tokens = max_tokens
        self._tokens: dict[str, float] = {}

    def __len__(self) -> int:
        """Number of live (unexpired) tokens, pruning expired ones first."""
        self._prune()
        return len(self._tokens)

    def _prune(self) -> None:
        now = time.monotonic()
        for token, expires in list(self._tokens.items()):
            if expires <= now:
                del self._tokens[token]

    def mint(self) -> str:
        """Return a fresh token, evicting expired and then the oldest entries.

        ``max_tokens`` bounds memory: once the store is full, the oldest
        token is dropped to make room (an old-but-unexpired token is never
        worth keeping over a brand-new one).
        """
        self._prune()
        while len(self._tokens) >= self.max_tokens:
            del self._tokens[next(iter(self._tokens))]
        token = secrets.token_hex(32)
        self._tokens[token] = time.monotonic() + self.ttl
        return token

    def consume(self, token: str) -> bool:
        """Consume ``token`` if known and unexpired; True at most once.

        Unknown, expired, already-consumed, empty, or non-str tokens all
        return False.
        """
        if not isinstance(token, str) or not token:
            return False
        self._prune()
        for candidate in list(self._tokens):
            if secrets.compare_digest(candidate.encode(), token.encode()):
                del self._tokens[candidate]
                return True
        return False


async def pipe_ws_tcp(ws, host: str, port: int) -> None:
    """Bridge a websocket and a TCP connection, byte-for-byte, both ways.

    ``ws`` is duck-typed to FastAPI's ``starlette.websockets.WebSocket``:
    it must expose ``receive_bytes()``, ``send_bytes(bytes)``, and
    ``close()``. Two pump tasks move data (client->server and server->
    client); the moment either direction ends or errors, both tasks are
    cancelled, the TCP side is closed, and ``ws.close()`` is attempted.
    Never raises out of this function.
    """
    reader, writer = await asyncio.open_connection(host, port)

    async def _ws_to_tcp() -> None:
        while True:
            data = await ws.receive_bytes()
            writer.write(data)
            await writer.drain()

    async def _tcp_to_ws() -> None:
        while True:
            data = await reader.read(65536)
            if not data:  # EOF from the VNC server -> drop the bridge
                return
            await ws.send_bytes(data)

    tasks = {asyncio.create_task(_ws_to_tcp()), asyncio.create_task(_tcp_to_ws())}
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        with contextlib.suppress(Exception):
            await ws.close()


async def display_online(host: str, port: int, timeout: float = 0.5) -> bool:
    """Probe whether something is listening on ``host:port``.

    Returns True if the connection succeeds (closed immediately afterwards),
    False on any error or timeout. Used by the status card.
    """
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except Exception:
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


def read_state(path: Path) -> dict:
    """Load the bot's help-request state; missing/corrupt file -> {}."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def write_state(path: Path, data: dict) -> None:
    """Write state atomically-ish: a temp file, then ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def clear_help_requested(path: Path) -> None:
    """Set ``help_requested`` False, keeping the rest of the state.

    Missing file is a no-op (nothing to clear).
    """
    if not path.exists():
        return
    data = read_state(path)
    data["help_requested"] = False
    write_state(path, data)


def read_password(path: Path) -> str | None:
    """Read the VNC password; None if the file is missing/unreadable."""
    try:
        return path.read_text().strip()
    except OSError:
        return None


def register_routes(plugin: VncPlugin) -> None:
    """Register the ``/vnc/token`` POST and ``/vnc/ws`` websocket.

    A single ``TokenManager`` is shared by all requests (module-level state
    per process). The VNC password is read from disk on every ``/vnc/token``
    call so a freshly-rotated per-start password is picked up without a
    restart.
    """
    token_manager = TokenManager()
    logger = plugin.logger

    @app.post("/vnc/token")
    async def vnc_token() -> JSONResponse:
        pw = read_password(Path(plugin.settings.vnc_password_file))
        if pw is None:
            return JSONResponse({"detail": "display offline"}, status_code=503)
        logger.info("vnc: token minted")
        return JSONResponse({"token": token_manager.mint(), "password": pw})

    @app.websocket("/vnc/ws")
    async def vnc_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            raw = await websocket.receive_text()
            token = json.loads(raw).get("token")
        except Exception:
            await websocket.close(code=1008, reason="unauthorized")
            return
        if not isinstance(token, str) or not token_manager.consume(token):
            logger.warning("vnc: websocket bridge rejected (invalid token)")
            await websocket.close(code=1008, reason="unauthorized")
            return
        logger.info("vnc: bridge up")
        await pipe_ws_tcp(websocket, plugin.settings.vnc_host, plugin.settings.vnc_port)
        logger.info("vnc: bridge down")
