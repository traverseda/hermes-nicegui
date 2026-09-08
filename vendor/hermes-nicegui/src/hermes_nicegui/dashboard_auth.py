"""Cookie-auth HTTP primitive for the Hermes dashboard web server.

The dashboard (the Hermes CLI's own web UI process -- cookie/password login,
not the gateway's bearer token) backs kanban's mutations
(``HermesExecutor``'s ``create_kanban_task``/``update_kanban_task``/etc) --
the one plugin whose writes have no CLI/direct-SQLite equivalent. Cron used
to share this (dashboard REST for job listing/updates) but no longer does:
both cron's reads (`cron/jobs.json`) and writes (`hermes cron edit`/...) now
go straight at the daemon (see ``hermes_nicegui.store``).
"""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger


class DashboardError(Exception):
    """Raised when the dashboard API returns a non-2xx response."""


class DashboardSession:
    """A cookie session against the Hermes dashboard.

    Auth is a session cookie obtained via ``POST /auth/password-login``. The
    login call is lazy (made on first request) and retried once on a
    ``401`` in case the session cookie expired mid-session.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._logged_in = False
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=5.0),
        )
        logger.debug("DashboardSession -> {}", self.base_url)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _login(self) -> None:
        resp = await self._client.post(
            "/auth/password-login",
            json={"provider": "basic", "username": self._username, "password": self._password},
        )
        if resp.status_code >= 400:
            raise DashboardError(f"login failed: {resp.status_code}: {resp.text[:200]}")
        self._logged_in = True

    async def request(
        self, method: str, path: str, *, json: dict | None = None, params: dict | None = None
    ) -> Any:
        if not self._logged_in:
            await self._login()
        resp = await self._client.request(method, path, json=json, params=params)
        if resp.status_code == 401:
            await self._login()
            resp = await self._client.request(method, path, json=json, params=params)
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise DashboardError(f"{method} {path} -> {resp.status_code}: {body}")
        return resp.json()
