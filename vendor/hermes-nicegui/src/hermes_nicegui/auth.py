"""Local username/password auth: a single admin account.

No new dependency: password hashing is stdlib ``hashlib.scrypt`` (memory-hard,
no compiled wheel needed -- unlike bcrypt/argon2, which matters since this
app already targets varied hosts, including plain SSH remotes -- see
``hermes_nicegui.executor``), and the user store is a small stdlib
``sqlite3`` file.

``AuthMiddleware`` must be registered via ``app.add_middleware(AuthMiddleware)``
**before** ``ui.run(...)`` is called (see ``hermes_nicegui.app.main``) --
``ui.run(storage_secret=...)`` adds Starlette's own ``SessionMiddleware`` as a
side effect, and ``Starlette.add_middleware`` prepends each call, so the
*last*-added middleware ends up outermost/first-to-run. Registered any other
way, ``app.storage.user`` either isn't populated yet when ``dispatch()`` runs,
or (worse, if attempted after startup) raises ``RuntimeError``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

from nicegui import app
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

# scrypt cost parameters: N=2**14, r=8, p=1 is the "interactive login" profile
# from the scrypt paper / OWASP guidance -- a fraction of a second per call,
# appropriate for a login form (not a batch job).
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
_SALT_BYTES = 16
_KEY_LEN = 32


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_LEN
    )
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt_hex, _, _digest_hex = stored.partition("$")
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    expected = hash_password(password, salt)
    return secrets.compare_digest(expected, stored)


class UserStore:
    """A single admin account (modeled as a table, not a single row, so a
    later multi-user extension is schema-compatible rather than a rewrite).

    Synchronous on purpose -- a local single-row SQLite read/write is
    sub-millisecond, but callers still run it via ``asyncio.to_thread`` to
    keep it off the event loop, matching this codebase's async-only rule.
    ``check_same_thread=False`` since ``asyncio.to_thread`` may hand the call
    to a different worker thread each time.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "username TEXT PRIMARY KEY, password_hash TEXT NOT NULL)"
        )
        self._conn.commit()
        with contextlib.suppress(PermissionError):
            os.chmod(db_path, 0o600)

    def has_any_user(self) -> bool:
        return self._conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def create_user(self, username: str, password: str) -> None:
        self._conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, hash_password(password)),
        )
        self._conn.commit()

    def verify(self, username: str, password: str) -> bool:
        row = self._conn.execute(
            "SELECT password_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
        return bool(row) and verify_password(password, row[0])


class ReadStateStore:
    """Server-side read-state storage: per-user session-read timestamps.

    Replaces the per-browser ``app.storage.user`` dict so read state
    survives device switches.  A single ``read_state`` table in the same
    ``users.db`` file keeps things simple — one DB, one file, one
    permission boundary.

    Schema::

        CREATE TABLE IF NOT EXISTS read_state (
            username TEXT NOT NULL,
            key     TEXT NOT NULL,
            ts      REAL NOT NULL,
            PRIMARY KEY (username, key)
        )

    ``key`` is either a session id or the reserved ``__all__`` marker
    (see ``logic.ALL_READ_KEY``).
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS read_state ("
            "username TEXT NOT NULL, key TEXT NOT NULL, ts REAL NOT NULL,"
            " PRIMARY KEY (username, key))"
        )
        self._conn.commit()
        with contextlib.suppress(PermissionError):
            os.chmod(db_path, 0o600)

    def load(self, username: str) -> dict[str, float]:
        """Return ``{key: timestamp}`` for *username*."""
        rows = self._conn.execute(
            "SELECT key, ts FROM read_state WHERE username = ?", (username,)
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def save(self, username: str, state: dict[str, float]) -> None:
        """Replace the full read-state map for *username*."""
        self._conn.execute("DELETE FROM read_state WHERE username = ?", (username,))
        self._conn.executemany(
            "INSERT INTO read_state (username, key, ts) VALUES (?, ?, ?)",
            [(username, k, v) for k, v in state.items()],
        )
        self._conn.commit()

    def set_key(self, username: str, key: str, ts: float) -> None:
        """Upsert a single key (convenience for ``_mark_read``)."""
        self._conn.execute(
            "INSERT INTO read_state (username, key, ts) VALUES (?, ?, ?)"
            " ON CONFLICT(username, key) DO UPDATE SET ts = excluded.ts",
            (username, key, ts),
        )
        self._conn.commit()

    def delete_key(self, username: str, key: str) -> None:
        """Remove a single key (used when a session is deleted)."""
        self._conn.execute(
            "DELETE FROM read_state WHERE username = ? AND key = ?",
            (username, key),
        )
        self._conn.commit()


def load_or_create_secret(path: Path) -> str:
    """The ``ui.run(storage_secret=...)`` value, generated once and persisted.

    Replaces a hardcoded secret (which would let anyone forge NiceGUI's
    encrypted session cookies) with 32 random bytes, reused across restarts
    so sessions -- and the auth gate itself -- survive one.
    """
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32)
    path.write_text(secret)
    with contextlib.suppress(PermissionError):
        os.chmod(path, 0o600)
    return secret


_PUBLIC_PATHS = {"/login"}
_PUBLIC_PREFIX = "/_nicegui"


def is_public_path(path: str) -> bool:
    """Paths ``AuthMiddleware`` lets through with no session check at all.

    Pulled out as a pure function so the path-matching rule is unit-testable
    without a running NiceGUI app / request cycle -- ``/login`` itself, plus
    NiceGUI's own static assets and ``/_nicegui_ws/`` websocket transport
    (confirmed against ``nicegui/nicegui.py``), needed for the login page
    itself to render and stay interactive.

    Deliberately *not* built from ``Client.page_routes`` (NiceGUI's own
    "which paths are pages" registry): the ``files`` plugin also registers
    plain ``@app.get``/``@app.post`` routes directly for raw downloads/
    uploads (``/files/raw/...``, ``/files/archive/...``,
    ``/files/upload-tree/...``), which aren't in that registry at all -- an
    allowlist keyed on it would silently leave the single most sensitive
    surface in the app unauthenticated.
    """
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIX)


class AuthMiddleware(BaseHTTPMiddleware):
    """Deny-by-default: everything except :func:`is_public_path` requires
    ``app.storage.user["authenticated"]``."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if is_public_path(request.url.path):
            return await call_next(request)
        if not app.storage.user.get("authenticated", False):
            return RedirectResponse("/login")
        return await call_next(request)
