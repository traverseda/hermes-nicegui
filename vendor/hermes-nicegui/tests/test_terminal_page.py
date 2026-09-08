"""Tests for the terminal plugin's page: rendering + nav item."""

from __future__ import annotations

from nicegui.testing import User

from hermes_nicegui import web
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.terminal import TerminalPlugin


async def test_terminal_nav_item_present(user: User, context: PluginContext) -> None:
    web.build(context, [TerminalPlugin(context)])
    await user.open("/")
    await user.should_see("Terminal")


async def test_terminal_page_renders_xterm(user: User, context: PluginContext) -> None:
    web.build(context, [TerminalPlugin(context)])
    await user.open("/terminal")
    await user.should_see("Local terminal")
    assert user.find(marker="terminal-view").elements
