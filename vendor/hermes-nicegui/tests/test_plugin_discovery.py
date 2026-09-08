"""Smoke test for the production entry-point discovery path.

Every other UI test passes ``web.build(context, [ExplicitPlugin(context)])``
so it only registers the plugin it's testing. This is the one test that
exercises the other path -- ``web.build(context)`` with no explicit list --
to check that the ``hermes_nicegui.plugins`` entry-point group installed by
``pyproject.toml`` still resolves to the plugins we ship.
"""

from __future__ import annotations

from nicegui.testing import User

from hermes_nicegui import web
from hermes_nicegui.plugin import PluginContext


async def test_discovers_installed_plugins(user: User, context: PluginContext) -> None:
    state = web.build(context)
    assert {p.name for p in state.plugins} == {
        "sessions",
        "cron",
        "costs",
        "terminal",
        "kanban",
        "files",
        "vnc",
        "xaelwiki",
    }
    await user.open("/")
    await user.should_see("Sessions")
    await user.should_see("Cron Jobs")
    await user.should_see("Costs")
    await user.should_see("Terminal")
    await user.should_see("Kanban")
    await user.should_see("Files")
    await user.should_see("VNC")
    await user.should_see("Notes")
