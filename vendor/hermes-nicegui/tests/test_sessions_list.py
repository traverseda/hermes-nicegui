"""Tests for the sessions list page: rendering, search, filtering, nav."""

from __future__ import annotations

import asyncio
import time
from typing import cast

import httpx
from loguru import logger
from nicegui import ui
from nicegui.testing import User

from hermes_nicegui import web
from hermes_nicegui.config import Settings
from hermes_nicegui.executor import CompletedResult, HermesExecutor
from hermes_nicegui.gateway import HermesClient
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.sessions import SessionsPlugin
from tests.conftest import FakeHermes, FakeHermesCli


async def test_slow_fetch_does_not_block_initial_response(
    user: User, hermes: FakeHermes, tmp_path
) -> None:
    """A read slower than NiceGUI's own page `response_timeout` (default 3s)
    must not 500 the page -- see `sessions/ui.py`'s
    `await ui.context.client.connected()` guard before the fetch. Confirmed
    against the real host: a 451-session `sessions export` (the old
    CLI-subprocess read path) took ~11s, well past that 3s ceiling, and
    *did* 500 before that guard was added -- direct SQLite reads are fast in
    "local" mode (no subprocess at all), but a remote ("ssh") profile still
    pays a real network round trip per read, so this fakes *that* path being
    slow (`io_run_fn`) rather than the now-fast local one. The other tests
    in this file use the (near-instant) fake CLI/local reads and so wouldn't
    catch a regression here.
    """

    async def slow_io_run_fn(argv: list[str], input_text: str) -> CompletedResult:
        await asyncio.sleep(3.5)
        return CompletedResult(0, "[]", "")

    executor = HermesExecutor(
        mode="ssh",
        ssh_target="hermes@host",
        hermes_home="~/.hermes",
        io_run_fn=slow_io_run_fn,
    )
    settings = Settings(
        gateway_url="http://hermes.test", api_token="test-token", data_dir=str(tmp_path)
    )
    client = HermesClient(
        settings.gateway_url, settings.api_token, transport=httpx.MockTransport(hermes.handle)
    )
    context = PluginContext(client=client, settings=settings, logger=logger, executor=executor)
    web.build(context, [SessionsPlugin(context)])

    start = time.monotonic()
    await user.open("/sessions")  # must not raise (a 500 fails this assertion)
    assert time.monotonic() - start < 3.0


async def test_sessions_list_renders(user: User, context: PluginContext) -> None:
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions")
    await user.should_see("First session")
    await user.should_see("Cron run")
    await user.should_see("Unread")
    assert user.find(marker="refresh-button").elements
    assert user.find(marker="mark-all-read-button").elements


async def test_mark_all_read_button_clears_unread_filter(
    user: User, context: PluginContext
) -> None:
    """With the Unread filter on, marking everything read must leave no rows."""
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions")
    await user.should_see("First session")
    user.find(ui.switch).click()
    await user.should_see("First session")
    user.find(marker="mark-all-read-button").click()
    await user.should_see("No sessions.", retries=10)


async def test_running_session_row_shows_spinner(user: User, context: PluginContext) -> None:
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions")
    await user.should_see("First session")
    await user.should_see("Cron run")
    # Only sess-1 is seeded running -> exactly one spinner on the page.
    assert len(user.find(marker="session-running").elements) == 1


async def test_sessions_nav_item_present(user: User, context: PluginContext) -> None:
    web.build(context, [SessionsPlugin(context)])
    await user.open("/")
    await user.should_see("Sessions")


async def test_rows_are_clickable(user: User, context: PluginContext) -> None:
    """Rows must carry Quasar's `clickable` prop for real-browser clicks."""
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions")
    await user.should_see("First session")
    for item in user.find(marker="session-row").elements:
        assert item.props.get("clickable") is True


async def test_click_row_navigates_to_detail(user: User, context: PluginContext) -> None:
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions")
    await user.should_see("First session")
    user.find(marker="session-row").click()
    await user.should_see("How do I access your API?", retries=10)
    await user.should_see("Here is the answer.", retries=10)


async def test_unread_filter_toggles(user: User, context: PluginContext) -> None:
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions")
    await user.should_see("First session")
    toggle = user.find(ui.switch)
    toggle.click()
    await user.should_see("First session")


async def test_new_session_button_navigates_to_new_session(
    user: User, context: PluginContext
) -> None:
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions")
    await user.should_see("First session")
    user.find(marker="new-session-button").click()
    await user.should_see("Message Hermes", retries=10)
    assert user.find(marker="chat-input").elements


async def test_list_paginates_with_numbered_pages(
    user: User, context: PluginContext, fake_hermes_cli: FakeHermesCli
) -> None:
    """More than one page of sessions must not all mount as DOM rows up
    front -- see `sessions/ui.py::render_rows`. A numbered page control
    appears instead, and switching pages fetches the next batch server-side
    (offset/limit), replacing rather than appending to the current page."""
    for i in range(40):
        fake_hermes_cli.insert_session(
            {
                "id": f"sess-extra-{i}",
                "title": f"Extra session {i}",
                "source": "webui",
                "model": "deepseek-v4-flash",
                "message_count": 1,
                "tool_call_count": 0,
                "input_tokens": 10,
                "output_tokens": 5,
                "estimated_cost_usd": 0.0,
                "last_activity_at": 1786620000.0 - i,
                "pinned": 0,
                "archived": 0,
            }
        )
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions")
    await user.should_see("First session")
    assert len(user.find(marker="session-row").elements) == 20
    pager = cast(ui.pagination, next(iter(user.find(marker="page-control").elements)))
    pager.set_value(2)
    await user.should_see("Extra session 35", retries=10)
    assert len(user.find(marker="session-row").elements) == 20
    await user.should_not_see("First session", retries=10)


async def test_search_filters_list(user: User, context: PluginContext) -> None:
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions")
    await user.should_see("First session")
    await user.should_see("Cron run")
    search = user.find(kind=ui.input)
    search.type("cron")
    search.trigger("change")
    await user.should_see("Cron run", retries=10)
    await user.should_not_see("First session", retries=10)


async def test_topbar_new_session_button_navigates_to_new_session(
    user: User, context: PluginContext
) -> None:
    """The shared top-bar button (present on every frame() page, not just
    /sessions) creates a session via the gateway and opens its detail page."""
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions")
    await user.should_see("First session")
    user.find(marker="topbar-new-session-button").click()
    await user.should_see("Message Hermes", retries=10)
    assert user.find(marker="chat-input").elements
