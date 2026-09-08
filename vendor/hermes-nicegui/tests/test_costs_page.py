"""Tests for the costs page."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nicegui import ui
from nicegui.testing import User

from hermes_nicegui import web
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.costs import CostsPlugin
from hermes_nicegui.plugins.costs.logic import (
    CostEntry,
    CostProvider,
    CostProviderError,
    CostSummary,
    CostWindow,
)


class FakeProvider(CostProvider):
    """Small deterministic provider for page tests."""

    def __init__(
        self,
        label: str,
        failing: bool = False,
        degraded: bool = False,
        funds_total: float | None = None,
        funds_remaining: float | None = None,
    ) -> None:
        self.name = label.lower().replace(" ", "-")
        self.label = label
        self.available = True
        self.failing = failing
        self.degraded = degraded
        self.funds_total = funds_total
        self.funds_remaining = funds_remaining

    async def summary(self) -> CostSummary:
        if self.failing:
            raise CostProviderError("provider failed")
        return CostSummary(
            provider=self.name,
            label=self.label,
            used=12.5,
            total=20,
            remaining=7.5,
            funds_total=self.funds_total,
            funds_remaining=self.funds_remaining,
            as_of=datetime.now(UTC),
            degraded=self.degraded,
            windows=[
                CostWindow(
                    "This month",
                    used=12.5,
                    limit=20,
                    remaining=7.5,
                    resets_at=datetime.now(UTC) + timedelta(days=5),
                    period=timedelta(days=30),
                ),
            ],
        )

    async def usage(self, limit: int = 50) -> list[CostEntry]:
        del limit
        return [
            CostEntry(
                when=datetime.now(UTC),
                label="Example request",
                cost=1.25,
                model="test-model",
                provider=self.name,
            )
        ]


async def test_costs_page_renders_provider_and_usage(user: User, context: PluginContext) -> None:
    plugin = CostsPlugin(context, providers=[FakeProvider("Test provider")])
    web.build(context, [plugin])
    await user.open("/costs")
    await user.should_see("Costs")
    await user.should_see("Test provider")
    await user.should_see("$7.50 remaining this month")
    await user.should_see("63% used this month")
    await user.should_see("5d 0h left · 83% elapsed")
    await user.should_see("83% elapsed")
    await user.should_see("This month: $7.50 remaining of $20.00")
    await user.should_see("Test provider usage")
    await user.should_not_see("all time")
    user.find(kind=ui.table)


async def test_costs_page_isolates_provider_errors(user: User, context: PluginContext) -> None:
    plugin = CostsPlugin(
        context,
        providers=[FakeProvider("Healthy"), FakeProvider("Broken", failing=True)],
    )
    web.build(context, [plugin])
    await user.open("/costs")
    await user.should_see("Healthy")
    await user.should_see("$7.50 remaining this month")
    await user.should_see("Broken")
    await user.should_see("provider failed")


async def test_costs_page_shows_prepaid_funds(user: User, context: PluginContext) -> None:
    plugin = CostsPlugin(
        context,
        providers=[FakeProvider("Test provider", funds_total=10.0, funds_remaining=3.0)],
    )
    web.build(context, [plugin])
    await user.open("/costs")
    await user.should_see("$3.00 remaining")
    await user.should_see("of $10.00 prepaid credits")


async def test_costs_page_marks_degraded_summary(user: User, context: PluginContext) -> None:
    plugin = CostsPlugin(context, providers=[FakeProvider("Degraded", degraded=True)])
    web.build(context, [plugin])
    await user.open("/costs")
    await user.should_see("NOT your plan total")


def test_quota_pace_helpers() -> None:
    from hermes_nicegui.plugins.costs.ui import (
        _elapsed_fraction,
        _time_left,
    )

    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    assert _time_left(now + timedelta(days=12, hours=4), now) == "12d 4h left"
    assert _time_left(now + timedelta(hours=1, minutes=30), now) == "1h 30m left"
    assert _time_left(now + timedelta(seconds=45), now) == "0m left"
    assert _elapsed_fraction(
        CostWindow(
            "w",
            used=1.0,
            limit=2.0,
            remaining=1.0,
            resets_at=now + timedelta(days=10),
            period=timedelta(days=30),
        ),
        now,
    ) == pytest.approx(2 / 3)
