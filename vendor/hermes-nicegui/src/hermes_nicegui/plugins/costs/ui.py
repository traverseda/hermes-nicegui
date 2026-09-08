"""NiceGUI page for provider costs."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime

from nicegui import ui

from hermes_nicegui.plugin import Plugin
from hermes_nicegui.web import frame

from .logic import CostEntry, CostProvider, CostSummary, CostWindow


def _money(value: float | None, currency: str = "USD") -> str:
    """Format a cost value for display."""
    if value is None:
        return "Unavailable"
    symbol = "$" if currency == "USD" else f"{currency} "
    return f"{symbol}{value:,.2f}"


def _time_left(until, now=None) -> str:
    """Format the time remaining until a reset."""
    if until is None:
        return ""
    now = now or datetime.now(UTC)
    seconds = max(0, int((until - now).total_seconds()))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days >= 1:
        return f"{days}d {hours}h left"
    if hours >= 1:
        return f"{hours}h {minutes}m left"
    return f"{minutes}m left"


def _elapsed_fraction(window: CostWindow, now) -> float | None:
    """Calculate elapsed time as a fraction of a nominal usage window."""
    if window.resets_at is None or window.period is None or window.period.total_seconds() <= 0:
        return None
    remaining = max(0.0, (window.resets_at - now).total_seconds())
    return max(0.0, min(1.0, 1.0 - remaining / window.period.total_seconds()))


def _pct(value: float) -> float:
    """Round a fraction to a whole percentage."""
    return math.floor(value * 100 + 0.5)


def _when(value) -> str:
    return value.isoformat(timespec="seconds") if value is not None else ""


def register_pages(plugin: Plugin) -> None:
    """Register the costs page."""
    providers: list[CostProvider] = plugin.providers  # type: ignore[attr-defined]

    @ui.page("/costs", title="Costs")
    async def costs_page() -> None:
        with frame(active="/costs"):
            with ui.row().classes("w-full items-center gap-2"):
                ui.label("Costs").classes("text-lg")
                ui.space()
                refresh = ui.button("Refresh")
            content = ui.column().classes("w-full gap-4")

            async def load() -> None:
                content.clear()
                summaries = await asyncio.gather(
                    *(provider.summary() for provider in providers), return_exceptions=True
                )
                usages = await asyncio.gather(
                    *(
                        provider.usage() if provider.available else _empty_usage()
                        for provider in providers
                    ),
                    return_exceptions=True,
                )
                with content:
                    with ui.row().classes("w-full items-stretch gap-4 flex-wrap"):
                        for provider, result in zip(providers, summaries, strict=True):
                            _render_summary(provider, result)
                    for provider, result in zip(providers, usages, strict=True):
                        if isinstance(result, list) and result and any(
                            entry.when is not None for entry in result
                        ):
                            _render_usage(provider, result)

            async def refresh_page() -> None:
                await load()

            refresh.on_click(refresh_page)
            await load()


async def _empty_usage() -> list[CostEntry]:
    return []


def _render_summary(provider: CostProvider, result: object) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    with ui.card().classes("min-w-64 flex-1"):
        if isinstance(result, CostSummary) and result.degraded:
            ui.badge(
                "Server usage unavailable — showing local session estimate, NOT your plan total",
                color="amber",
            )
        ui.label(provider.label).classes("text-lg font-medium")
        if not provider.available:
            ui.label("not configured (missing key / no data file)").classes("text-sm opacity-60")
        elif isinstance(result, Exception):
            ui.badge(str(result), color="amber")
        else:
            summary: CostSummary = result  # type: ignore[assignment]
            show_funds = summary.funds_remaining is not None
            if show_funds:
                ui.label(
                    f"{_money(summary.funds_remaining, summary.currency)} remaining"
                ).classes("text-2xl")
                if summary.funds_total is not None:
                    ui.label(
                        f"of {_money(summary.funds_total, summary.currency)} prepaid credits"
                    ).classes("text-sm opacity-70")
            if summary.total is not None:
                ui.label(
                    f"{_money(summary.remaining, summary.currency)} remaining this month"
                ).classes("text-2xl")
                ui.label(f"of {_money(summary.total, summary.currency)}")
                if not summary.degraded and summary.used is not None and summary.total > 0:
                    used_pct = math.floor(summary.used / summary.total * 100 + 0.5)
                    ui.label(f"{used_pct:.0f}% used this month").classes("text-xl")
                month_window = next(
                    (window for window in summary.windows if window.label == "This month"), None
                )
                elapsed = (
                    _elapsed_fraction(month_window, now)
                    if month_window is not None
                    else None
                )
                used_frac = (
                    max(0.0, min(1.0, month_window.used / month_window.limit))
                    if month_window is not None
                    and month_window.used is not None
                    and month_window.limit is not None
                    and month_window.limit > 0
                    else None
                )
                with ui.column().classes("w-full gap-1"):
                    if used_frac is not None:
                        ui.linear_progress(used_frac, show_value=False)
                        ui.label(f"{_pct(used_frac):.0f}% used").classes("text-xs opacity-70")
                    if (
                        elapsed is not None
                        and month_window is not None
                        and month_window.resets_at is not None
                    ):
                        ui.linear_progress(elapsed, show_value=False, color="amber")
                        ui.label(
                            f"{_time_left(month_window.resets_at, now)} · {_pct(elapsed):.0f}% elapsed"
                        ).classes("text-xs opacity-70")
            elif summary.used is not None:
                ui.label(f"{_money(summary.used, summary.currency)} this month").classes(
                    "text-lg" if show_funds else "text-2xl"
                )
            for window in summary.windows:
                if window.limit is not None:
                    text = (
                        f"{window.label}: {_money(window.remaining, summary.currency)} remaining "
                        f"of {_money(window.limit, summary.currency)}"
                    )
                else:
                    text = f"{window.label}: {_money(window.used, summary.currency)} used"
                if window.resets_at is not None:
                    text += f" · {_time_left(window.resets_at, now)}"
                label = ui.label(text)
                if window.resets_at is not None:
                    label.tooltip(f"resets {window.resets_at.isoformat(timespec='minutes')}")
            if summary.as_of:
                ui.label(f"As of {_when(summary.as_of)}").classes("text-xs opacity-60")
            if summary.note:
                ui.label(summary.note).classes("text-sm opacity-60")


def _render_usage(provider: CostProvider, entries: list[CostEntry]) -> None:
    ui.label(f"{provider.label} usage").classes("text-lg")
    columns = [
        {"name": "when", "label": "When", "field": "when"},
        {"name": "provider", "label": "Provider", "field": "provider"},
        {"name": "model", "label": "Model", "field": "model"},
        {"name": "label", "label": "Label", "field": "label"},
        {"name": "cost", "label": "Cost", "field": "cost"},
        {"name": "tokens", "label": "Tokens in/out", "field": "tokens"},
    ]
    rows = [
        {
            "when": _when(entry.when),
            "provider": entry.provider or provider.label,
            "model": entry.model or "",
            "label": entry.label,
            "cost": _money(entry.cost),
            "tokens": f"{entry.tokens_input or ''}/{entry.tokens_output or ''}",
        }
        for entry in entries
    ]
    ui.table(columns=columns, rows=rows, row_key="label").classes("w-full")
