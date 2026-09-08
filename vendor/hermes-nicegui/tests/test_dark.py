"""Tests for dark-mode following Settings.ui_dark."""

from __future__ import annotations

from collections.abc import Callable

from nicegui import ui
from nicegui.testing import User

from hermes_nicegui import web
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.sessions import SessionsPlugin


async def test_dark_mode_enabled(user: User, make_context: Callable[..., PluginContext]) -> None:
    context = make_context(ui_dark=True)
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions")
    dark = user.find(kind=ui.dark_mode)
    assert next(iter(dark.elements)).value is True


async def test_dark_mode_disabled(user: User, make_context: Callable[..., PluginContext]) -> None:
    context = make_context(ui_dark=False)
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions")
    dark = user.find(kind=ui.dark_mode)
    assert next(iter(dark.elements)).value is False
