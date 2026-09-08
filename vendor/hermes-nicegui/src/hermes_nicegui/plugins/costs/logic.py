"""Provider abstractions and data access for the costs plugin."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx


@dataclass
class CostWindow:
    """A usage-limit window (label, used, limit, remaining)."""

    label: str
    used: float | None = None
    limit: float | None = None
    remaining: float | None = None
    resets_at: datetime | None = None
    period: timedelta | None = None


@dataclass
class CostSummary:
    """Aggregated spend for a provider."""

    provider: str
    label: str
    used: float | None = None
    total: float | None = None
    remaining: float | None = None
    funds_total: float | None = None
    funds_remaining: float | None = None
    currency: str = "USD"
    as_of: datetime | None = None
    note: str | None = None
    degraded: bool = False
    windows: list[CostWindow] = field(default_factory=list)


@dataclass
class CostEntry:
    """A single provider usage record."""

    when: datetime | None = None
    label: str = ""
    cost: float = 0.0
    model: str | None = None
    provider: str | None = None
    tokens_input: int | None = None
    tokens_output: int | None = None


class CostProviderError(RuntimeError):
    """Raised when a provider cannot retrieve its costs."""


class CostProvider(ABC):
    """Base interface implemented by each costs data source."""

    name: str
    label: str
    available: bool = False

    @abstractmethod
    async def summary(self) -> CostSummary:
        """Return the provider's aggregate spend."""

    async def usage(self, limit: int = 50) -> list[CostEntry]:
        """Return detailed usage, when the provider supports it."""
        return []


class OpenRouterProvider(CostProvider):
    """Read period usage from OpenRouter."""

    name = "openrouter"
    label = "OpenRouter"

    def __init__(
        self,
        env_file: str | Path = "/run/agenix/hermes-env",
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.env_file = Path(env_file).expanduser()
        self.key = os.environ.get("OPENROUTER_API_KEY") or self._read_env_file()
        self.available = bool(self.key)
        self.client = client

    def _read_env_file(self) -> str | None:
        """Read only the OpenRouter key from a KEY=value secrets file."""
        try:
            lines = self.env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "OPENROUTER_API_KEY":
                return value.strip().strip("\"'") or None
        return None

    async def _get(self, url: str) -> dict[str, Any]:
        """Fetch a JSON object using either the injected or a real client."""
        headers = {"Authorization": f"Bearer {self.key}"}
        if self.client is not None:
            response = await self.client.get(url, headers=headers)
            return self._response_json(response)
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, headers=headers)
        return self._response_json(response)

    def _response_json(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code < 200 or response.status_code >= 300:
            raise CostProviderError(f"OpenRouter returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise CostProviderError("OpenRouter returned invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise CostProviderError("OpenRouter returned an invalid response")
        return payload

    async def summary(self) -> CostSummary:
        """Return current OpenRouter period usage and prepaid balance."""
        if not self.available or self.key is None:
            raise CostProviderError("OpenRouter is not configured")
        key_result, credits_result = await asyncio.gather(
            self._get("https://openrouter.ai/api/v1/key"),
            self._get("https://openrouter.ai/api/v1/credits"),
            return_exceptions=True,
        )
        if isinstance(key_result, Exception):
            raise CostProviderError("OpenRouter usage response is incomplete") from key_result
        data = key_result["data"]
        try:
            usage_monthly = float(data["usage_monthly"])
            usage_weekly = float(data["usage_weekly"])
            usage_daily = float(data["usage_daily"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CostProviderError("OpenRouter usage response is incomplete") from exc
        summary = CostSummary(
            provider=self.name,
            label=self.label,
            used=usage_monthly,
            as_of=datetime.now(UTC),
            windows=[
                CostWindow("This month", used=usage_monthly),
                CostWindow("This week", used=usage_weekly),
                CostWindow("Today", used=usage_daily),
            ],
        )
        self._apply_funds(summary, credits_result)
        return summary

    def _apply_funds(self, summary: CostSummary, result: object) -> None:
        """Populate prepaid-balance fields from the credits endpoint, if available."""
        if isinstance(result, Exception) or not isinstance(result, dict):
            return
        data = result.get("data")
        if not isinstance(data, dict):
            return
        total_credits = data.get("total_credits")
        if total_credits is not None:
            try:
                total = float(total_credits)
            except (TypeError, ValueError):
                return
            try:
                used = float(data.get("total_usage") or 0)
            except (TypeError, ValueError):
                used = 0.0
            summary.funds_total = total
            summary.funds_remaining = max(0.0, total - used)
            return
        if summary.note is None and data.get("has_payment_method") is True:
            summary.note = "Pay-as-you-go account — no prepaid balance"

    async def usage(self, limit: int = 50) -> list[CostEntry]:
        """Return OpenRouter period usage totals."""
        del limit
        if not self.available or self.key is None:
            raise CostProviderError("OpenRouter is not configured")
        data = (await self._get("https://openrouter.ai/api/v1/key"))["data"]
        masked = self._masked_key(self.key)
        periods = (
            ("usage_monthly", f"API key {masked} — this month"),
            ("usage_weekly", f"API key {masked} — this week"),
            ("usage_daily", f"API key {masked} — today"),
        )
        try:
            return [
                CostEntry(label=label, cost=float(data[field]), provider=self.name)
                for field, label in periods
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise CostProviderError("OpenRouter usage response is incomplete") from exc

    @staticmethod
    def _masked_key(key: str) -> str:
        """Mask an API key while retaining a useful identifier."""
        prefix = "sk-or-v1-"
        if key.startswith(prefix):
            body = key[len(prefix) :]
            return f"{prefix}{body[:8]}…{body[-4:]}"
        return f"{key[:8]}…{key[-4:]}"


class OpenCodeLocalProvider(CostProvider):
    """Read per-session costs from OpenCode's local SQLite database."""

    name = "opencode"
    label = "OpenCode Go"

    def __init__(
        self,
        db_path: str = "~/.local/share/opencode/opencode-stable.db",
        *,
        monthly_limit: float = 60.0,
        weekly_limit: float = 30.0,
        five_hour_limit: float = 12.0,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self.available = self.db_path.is_file()
        self.monthly_limit = monthly_limit
        self.weekly_limit = weekly_limit
        self.five_hour_limit = five_hour_limit

    def _query_summary(self, month_start_ms: int) -> list[tuple[Any, ...]]:
        with sqlite3.connect(self.db_path) as connection:
            return connection.execute(
                "SELECT model, cost, time_created FROM session "
                "WHERE cost IS NOT NULL AND cost > 0 AND time_created >= ?",
                (month_start_ms,),
            ).fetchall()

    @staticmethod
    def _model_provider(model: Any) -> str | None:
        """Extract a provider ID from OpenCode's JSON model value."""
        if not isinstance(model, str):
            return None
        try:
            parsed_model = json.loads(model)
        except (TypeError, ValueError):
            return None
        if isinstance(parsed_model, dict):
            provider_id = parsed_model.get("providerID")
            return provider_id if isinstance(provider_id, str) else None
        return None

    def _query_usage(self, limit: int) -> list[tuple[Any, ...]]:
        with sqlite3.connect(self.db_path) as connection:
            return connection.execute(
                "SELECT title, model, cost, tokens_input, tokens_output, time_created "
                "FROM session WHERE cost IS NOT NULL AND cost > 0 "
                "ORDER BY time_created DESC LIMIT ?",
                (limit,),
            ).fetchall()

    async def summary(self, *, now: datetime | None = None) -> CostSummary:
        """Estimate OpenCode Go budget usage from recent local sessions."""
        if not self.available:
            raise CostProviderError("OpenCode database is not available")
        now = now or datetime.now(UTC)
        month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
        resets_at = datetime(
            now.year + (1 if now.month == 12 else 0),
            (now.month % 12) + 1,
            1,
            tzinfo=UTC,
        )
        try:
            rows = await asyncio.to_thread(
                self._query_summary, int(month_start.timestamp() * 1000)
            )
        except sqlite3.Error as exc:
            raise CostProviderError("Could not read the OpenCode database") from exc
        month_used = 0.0
        week_used = 0.0
        five_hour_used = 0.0
        for model, cost, time_created in rows:
            if self._model_provider(model) != "opencode-go":
                continue
            try:
                created = datetime.fromtimestamp(float(time_created) / 1000, tz=UTC)
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            amount = float(cost)
            age = (now - created).total_seconds()
            month_used += amount
            if age <= 7 * 86400:
                week_used += amount
            if age <= 5 * 3600:
                five_hour_used += amount
        return CostSummary(
            provider=self.name,
            label=self.label,
            used=month_used,
            total=self.monthly_limit,
            remaining=self.monthly_limit - month_used,
            as_of=now,
            note="Estimated from local session records — console usage may differ",
            windows=[
                CostWindow(
                    "This month",
                    used=month_used,
                    limit=self.monthly_limit,
                    remaining=self.monthly_limit - month_used,
                    resets_at=resets_at,
                    period=resets_at - month_start,
                ),
                CostWindow(
                    "This week",
                    used=week_used,
                    limit=self.weekly_limit,
                    remaining=self.weekly_limit - week_used,
                    resets_at=now + timedelta(days=7),
                    period=timedelta(days=7),
                ),
                CostWindow(
                    "5-hour window",
                    used=five_hour_used,
                    limit=self.five_hour_limit,
                    remaining=self.five_hour_limit - five_hour_used,
                    resets_at=now + timedelta(hours=5),
                    period=timedelta(hours=5),
                ),
            ],
        )

    async def usage(self, limit: int = 50) -> list[CostEntry]:
        """Return recent local OpenCode session costs."""
        if not self.available:
            raise CostProviderError("OpenCode database is not available")
        try:
            rows = await asyncio.to_thread(self._query_usage, limit)
        except sqlite3.Error as exc:
            raise CostProviderError("Could not read the OpenCode database") from exc
        entries: list[CostEntry] = []
        for title, model, cost, tokens_input, tokens_output, time_created in rows:
            when = None
            if time_created is not None:
                try:
                    when = datetime.fromtimestamp(float(time_created) / 1000, tz=UTC)
                except (TypeError, ValueError, OverflowError, OSError):
                    pass
            if isinstance(model, str) and model.startswith(("{", "[")):
                try:
                    parsed_model = json.loads(model)
                    if isinstance(parsed_model, dict) and "id" in parsed_model:
                        model = parsed_model["id"]
                except (TypeError, ValueError):
                    pass
            entries.append(
                CostEntry(
                    when=when,
                    label=title or "(untitled)",
                    model=model,
                    cost=float(cost or 0),
                    provider=self.name,
                    tokens_input=int(tokens_input) if tokens_input is not None else None,
                    tokens_output=int(tokens_output) if tokens_output is not None else None,
                )
            )
        return entries


class OpenCodeGoProvider(CostProvider):
    """Read OpenCode Go plan budget usage from the server usage API."""

    name = "opencode-go"
    label = "OpenCode Go"

    def __init__(
        self,
        env_file: str | Path = "/run/agenix/hermes-env",
        *,
        client: httpx.AsyncClient | None = None,
        monthly_limit: float = 60.0,
        weekly_limit: float = 30.0,
        five_hour_limit: float = 12.0,
        local_db: str = "~/.local/share/opencode/opencode-stable.db",
    ) -> None:
        self.env_file = Path(env_file).expanduser()
        self.key = (
            os.environ.get("OPENCODE_API_KEY")
            or os.environ.get("OPENCODE_GO_API_KEY")
            or self._read_env_file()
        )
        self.client = client
        self.monthly_limit = monthly_limit
        self.weekly_limit = weekly_limit
        self.five_hour_limit = five_hour_limit
        self.local_db = Path(local_db).expanduser()
        self.available = bool(self.key) or self._local_available()

    def _read_env_file(self) -> str | None:
        """Read an OpenCode Go key from a KEY=value secrets file."""
        try:
            lines = self.env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        values: dict[str, str] = {}
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'") or ""
        return values.get("OPENCODE_API_KEY") or values.get("OPENCODE_GO_API_KEY") or None

    def _local_available(self) -> bool:
        return Path(self.local_db).expanduser().is_file()

    async def _get(self, url: str) -> dict[str, Any]:
        """Fetch a JSON object using either the injected or a real client."""
        headers = {"Authorization": f"Bearer {self.key}"}
        if self.client is not None:
            response = await self.client.get(url, headers=headers)
            return self._response_json(response)
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, headers=headers)
        return self._response_json(response)

    def _response_json(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code < 200 or response.status_code >= 300:
            raise CostProviderError(f"OpenCode Go returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise CostProviderError("OpenCode Go returned invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
            raise CostProviderError("OpenCode Go returned an invalid response")
        return payload

    @staticmethod
    def _parse_resets_at(value: Any) -> datetime:
        if not isinstance(value, str):
            raise ValueError("resetsAt is not a string")
        iso = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(iso)

    async def summary(self) -> CostSummary:
        """Return server-reported OpenCode Go plan usage, or fall back to local."""
        if not self.available:
            raise CostProviderError("OpenCode Go is not configured")
        try:
            if self.key is None:
                raise CostProviderError("OpenCode Go API key is missing")
            usage = (await self._get("https://opencode.ai/zen/go/v1/usage"))["usage"]
            five_hour_percent = float(usage["rolling"]["percent"])
            week_percent = float(usage["weekly"]["percent"])
            month_percent = float(usage["monthly"]["percent"])
            five_hour_used = self.five_hour_limit * five_hour_percent / 100.0
            week_used = self.weekly_limit * week_percent / 100.0
            month_used = self.monthly_limit * month_percent / 100.0
            return CostSummary(
                provider=self.name,
                label=self.label,
                used=month_used,
                total=self.monthly_limit,
                remaining=self.monthly_limit - month_used,
                as_of=datetime.now(UTC),
                note="Server-reported OpenCode Go usage",
                windows=[
                    CostWindow(
                        "5-hour window",
                        used=five_hour_used,
                        limit=self.five_hour_limit,
                        remaining=self.five_hour_limit - five_hour_used,
                        resets_at=self._parse_resets_at(usage["rolling"]["resetsAt"]),
                        period=timedelta(hours=5),
                    ),
                    CostWindow(
                        "This week",
                        used=week_used,
                        limit=self.weekly_limit,
                        remaining=self.weekly_limit - week_used,
                        resets_at=self._parse_resets_at(usage["weekly"]["resetsAt"]),
                        period=timedelta(days=7),
                    ),
                    CostWindow(
                        "This month",
                        used=month_used,
                        limit=self.monthly_limit,
                        remaining=self.monthly_limit - month_used,
                        resets_at=self._parse_resets_at(usage["monthly"]["resetsAt"]),
                        # The server gives the reset date but not the cycle start; 30 days is the documented approximation.
                        period=timedelta(days=30),
                    ),
                ],
            )
        except (CostProviderError, httpx.HTTPError, KeyError, TypeError, ValueError):
            if not self._local_available():
                raise CostProviderError("OpenCode Go is not configured") from None
            local = OpenCodeLocalProvider(
                self.local_db,
                monthly_limit=self.monthly_limit,
                weekly_limit=self.weekly_limit,
                five_hour_limit=self.five_hour_limit,
            )
            fallback = await local.summary()
            fallback.note = "Server usage unavailable — estimated from local session records"
            fallback.degraded = True
            return fallback

    async def usage(self, limit: int = 50) -> list[CostEntry]:
        """Return per-session local OpenCode costs."""
        local = OpenCodeLocalProvider(
            self.local_db,
            monthly_limit=self.monthly_limit,
            weekly_limit=self.weekly_limit,
            five_hour_limit=self.five_hour_limit,
        )
        return await local.usage(limit)


def build_providers(settings: Any) -> list[CostProvider]:
    """Build the providers configured by application settings."""
    return [
        OpenRouterProvider(settings.costs_env_file),
        OpenCodeGoProvider(
            settings.costs_env_file,
            monthly_limit=settings.opencode_go_monthly_limit,
            weekly_limit=settings.opencode_go_weekly_limit,
            five_hour_limit=settings.opencode_go_5h_limit,
            local_db=settings.opencode_db,
        ),
    ]
