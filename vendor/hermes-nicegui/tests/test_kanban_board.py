"""Tests for the kanban board page: rendering, nav, filters, create dialog."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import cast

from nicegui import ui
from nicegui.testing import User

from hermes_nicegui import web
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.kanban import KanbanPlugin
from hermes_nicegui.plugins.kanban.logic import CANONICAL_COLUMNS
from tests.conftest import FakeHermesCli, FakeKanban


async def test_kanban_board_renders(
    user: User, kanban_context: PluginContext
) -> None:
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban")
    await user.should_see("Fix flaky test")
    assert user.find(marker="new-task-button").elements


async def test_kanban_nav_item_present(
    user: User, kanban_context: PluginContext
) -> None:
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/")
    await user.should_see("Kanban")


async def test_task_cards_are_clickable(
    user: User, kanban_context: PluginContext
) -> None:
    """Cards must carry Quasar's `clickable` prop for real-browser clicks."""
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban")
    await user.should_see("Fix flaky test")
    for item in user.find(marker="task-card").elements:
        assert item.props.get("clickable") is True


async def test_click_card_navigates_to_detail(
    user: User, kanban_context: PluginContext
) -> None:
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban")
    await user.should_see("Fix flaky test")
    user.find(marker="task-card").click()
    await user.should_see("Comments", retries=10)


async def test_filter_tab_hides_tasks_in_other_status(
    user: User, kanban_context: PluginContext
) -> None:
    """``t_1`` is ``ready``; switching to the ``done`` filter should hide it."""
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban")
    await user.should_see("Fix flaky test")

    tabs = cast(ui.tabs, next(iter(user.find(marker="status-filter").elements)))
    tabs.set_value("done")

    await user.should_see("No tasks.", retries=10)
    await user.should_not_see("Fix flaky test", retries=10)


async def test_filter_tabs_order_review_before_blocked(
    user: User, kanban_context: PluginContext
) -> None:
    """The status filter tabs must render in canonical order -- ``review``
    before ``blocked`` (ticket: "review should be before blocked in kanban
    views")."""
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban")
    await user.should_see("Fix flaky test")

    tabs = cast(ui.tabs, next(iter(user.find(marker="status-filter").elements)))
    names = [tab.props["name"] for tab in tabs.default_slot.children]
    assert names.index("review") < names.index("blocked")


async def test_stage_help_expansion_lists_canonical_columns(
    user: User, kanban_context: PluginContext
) -> None:
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban")

    expansion = cast(ui.expansion, next(iter(user.find(marker="stage-help-expansion").elements)))
    rows = expansion.default_slot.children
    assert len(rows) == 8
    labels = [cast(ui.label, row.default_slot.children[1]).text.split(" — ", 1)[0] for row in rows]
    assert labels == [
        "Triage",
        "To Do",
        "Scheduled",
        "Ready",
        "Running",
        "Review",
        "Blocked",
        "Done",
    ]


async def test_create_task_dialog_adds_card(
    user: User, kanban_context: PluginContext
) -> None:
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban")
    await user.should_see("Fix flaky test")

    user.find(marker="new-task-button").click()
    await user.should_see("New task", retries=10)
    user.find(marker="new-task-title").type("Write docs")
    user.find(marker="create-task-confirm").click()

    await user.should_see("Write docs", retries=10)


async def test_board_paginates_with_numbered_pages(
    user: User, kanban_context: PluginContext, fake_kanban
) -> None:
    for i in range(35):
        fake_kanban.insert_task(
            {
                "id": f"t-extra-{i}",
                "title": f"Extra task {i}",
                "body": "",
                "assignee": "default",
                "status": "ready",
                "priority": 1,
                "tenant": None,
                # Strictly older than the default t_1 fixture's created_at
                # (1786620000) -- the board sorts newest-first within a
                # status, so t_1 (the single newest task) is guaranteed page
                # 1 and the extras split 29/6 across the two pages by age.
                "created_at": 1786619000 + i,
                "started_at": None,
                "completed_at": None,
                "consecutive_failures": 0,
                "last_failure_error": None,
                "current_run_id": None,
                "session_id": None,
            }
        )
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban")
    await user.should_see("Fix flaky test")
    assert len(user.find(marker="task-card").elements) == 30

    pager = cast(ui.pagination, next(iter(user.find(marker="page-control").elements)))
    pager.set_value(2)
    await user.should_see("Extra task 0", retries=10)
    assert len(user.find(marker="task-card").elements) == 6
    await user.should_not_see("Fix flaky test", retries=10)


async def test_running_task_icon_is_animated(
    user: User, kanban_context: PluginContext, fake_kanban: FakeKanban
) -> None:
    fake_kanban.insert_task(
        {
            "id": "t_running",
            "title": "Long running job",
            "body": "",
            "assignee": "default",
            "status": "running",
            "priority": 1,
            "tenant": None,
            "created_at": 1786620001,
            "started_at": None,
            "completed_at": None,
            "consecutive_failures": 0,
            "last_failure_error": None,
            "current_run_id": None,
            "session_id": None,
        }
    )
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban")

    icons = user.find(marker="task-status-icon").elements
    animated = [icon for icon in icons if "kanban-running-icon" in icon.classes]
    assert len(animated) == 1
    ready_icon = next(icon for icon in icons if icon not in animated)
    assert "kanban-running-icon" not in ready_icon.classes


async def test_search_filters_board(
    user: User,
    kanban_context: PluginContext,
    fake_kanban: FakeKanban,
) -> None:
    """Typing into the kanban-search box narrows the board to matching tasks
    (title/body/assignee/id/comments) and shows "No tasks." on no match --
    the same `LIKE` search the sessions list page uses."""
    fake_kanban.insert_task(
        {
            "id": "t_cleanup",
            "title": "Clean up backlog",
            "body": "",
            "assignee": "default",
            "status": "ready",
            "priority": 1,
            "tenant": None,
            "created_at": 1786620001,
            "started_at": None,
            "completed_at": None,
            "consecutive_failures": 0,
            "last_failure_error": None,
            "current_run_id": None,
            "session_id": None,
        }
    )
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban")
    await user.should_see("Fix flaky test")
    await user.should_see("Clean up backlog")

    search = user.find(marker="kanban-search")
    search.type("flaky")
    search.trigger("change")
    await user.should_see("Fix flaky test", retries=10)
    await user.should_not_see("Clean up backlog", retries=10)

    search.clear()
    search.type("zzz-nonexistent")
    search.trigger("change")
    await user.should_see("No tasks.", retries=10)
    await user.should_not_see("Fix flaky test", retries=10)


async def test_search_combines_with_status_tab(
    user: User,
    kanban_context: PluginContext,
    fake_kanban: FakeKanban,
) -> None:
    """Search and the status tabs combine: a query whose only match is a
    `done` task shows nothing while the `ready` tab is selected."""
    fake_kanban.insert_task(
        {
            "id": "t_legacy",
            "title": "Retire the legacy flaky report",
            "body": "",
            "assignee": "default",
            "status": "done",
            "priority": 1,
            "tenant": None,
            "created_at": 1786620002,
            "started_at": None,
            "completed_at": 1786620300,
            "consecutive_failures": 0,
            "last_failure_error": None,
            "current_run_id": None,
            "session_id": None,
        }
    )
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban")
    await user.should_see("Fix flaky test")

    search = user.find(marker="kanban-search")
    search.type("legacy")
    search.trigger("change")
    await user.should_see("Retire the legacy flaky report", retries=10)
    await user.should_not_see("Fix flaky test", retries=10)

    tabs = cast(ui.tabs, next(iter(user.find(marker="status-filter").elements)))
    tabs.set_value("ready")

    await user.should_see("No tasks.", retries=10)
    await user.should_not_see("Retire the legacy flaky report", retries=10)


def _task_row(db_path: str, task_id: str) -> tuple[str, str]:
    """(status, body) of a task directly from the fake kanban.db — the same
    file the board page re-reads after an auto-specify, mirroring how the
    real `hermes kanban specify` CLI mutates the daemon's DB in place."""
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT status, body FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    finally:
        con.close()
    assert row is not None, f"task {task_id} not in kanban.db"
    return row[0], row[1]


async def test_create_task_empty_body_triggers_auto_specify(
    user: User,
    kanban_context: PluginContext,
    fake_hermes_cli: FakeHermesCli,
    fake_kanban: FakeKanban,
) -> None:
    """An empty-description task created at the dialog's default Triage
    status gets auto-specified in the background: the UI shells out to
    `hermes kanban specify <id> --json` and the task lands with a fleshed-out
    body, promoted triage -> todo."""
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban")
    await user.should_see("Fix flaky test")

    user.find(marker="new-task-button").click()
    await user.should_see("New task", retries=10)
    user.find(marker="new-task-title").type("Write docs")
    user.find(marker="create-task-confirm").click()

    # Let the background auto-specify (fake CLI) land, then check it ran and
    # wrote through to kanban.db.
    for _ in range(50):
        if fake_hermes_cli.kanban_actions:
            break
        await asyncio.sleep(0.05)
    assert fake_hermes_cli.kanban_actions, "auto-specify was never triggered"
    args = fake_hermes_cli.kanban_actions[0]
    assert args[:2] == ("kanban", "specify")
    assert args[-1] == "--json"

    status, body = _task_row(str(fake_kanban.db_path), "t_2")
    assert status == "todo"
    assert body.startswith("**Goal**")


async def test_create_task_with_body_skips_auto_specify(
    user: User,
    kanban_context: PluginContext,
    fake_hermes_cli: FakeHermesCli,
) -> None:
    """A task created with a real description must NOT be auto-specified —
    the specifier is only for cards that arrive with an empty body."""
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban")
    await user.should_see("Fix flaky test")

    user.find(marker="new-task-button").click()
    await user.should_see("New task", retries=10)
    user.find(marker="new-task-title").type("Write docs")
    user.find(marker="new-task-body").type("Document the API and the CLI.")
    user.find(marker="create-task-confirm").click()
    await user.should_see("Write docs", retries=10)
    await asyncio.sleep(0.2)

    assert not fake_hermes_cli.kanban_actions, "auto-specify ran for a task with a body"


async def test_create_task_empty_body_non_triage_warns(
    user: User,
    kanban_context: PluginContext,
    fake_hermes_cli: FakeHermesCli,
) -> None:
    """An empty-body task created at a non-Triage status can't use the
    triage specifier (it refuses), so no specify call is made — the dialog
    warns instead."""
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban")
    await user.should_see("Fix flaky test")

    user.find(marker="new-task-button").click()
    await user.should_see("New task", retries=10)
    user.find(marker="new-task-title").type("Write docs")
    status_select = cast(
        ui.select, next(iter(user.find(marker="new-task-status").elements))
    )
    status_select.set_value("ready")
    user.find(marker="create-task-confirm").click()
    await user.should_see("Write docs", retries=10)
    await asyncio.sleep(0.2)

    assert not fake_hermes_cli.kanban_actions, "auto-specify ran for a non-triage task"


async def test_same_priority_tasks_sorted_by_created_at(
    user: User, kanban_context: PluginContext, fake_kanban: FakeKanban
) -> None:
    """Within a status group, tasks sort by created_at descending (newest
    first) -- see `_kanban_list_sql`'s "status first, newest at top" order."""
    # Insert two tasks at the same priority (2) with different created_at,
    # plus the default t_1 fixture (priority 2, created_at 1786620000).
    # Insert out of date order so the SQL sort must reorder them.
    fake_kanban.insert_task(
        {
            "id": "t_newest",
            "title": "Newest priority-2 task",
            "body": "",
            "assignee": "default",
            "status": "ready",
            "priority": 2,
            "tenant": None,
            "created_at": 1786620050,  # newer than t_1
            "started_at": None,
            "completed_at": None,
            "consecutive_failures": 0,
            "last_failure_error": None,
            "current_run_id": None,
            "session_id": None,
        }
    )
    fake_kanban.insert_task(
        {
            "id": "t_oldest",
            "title": "Oldest priority-2 task",
            "body": "",
            "assignee": "default",
            "status": "ready",
            "priority": 2,
            "tenant": None,
            "created_at": 1786610000,  # older than t_1
            "started_at": None,
            "completed_at": None,
            "consecutive_failures": 0,
            "last_failure_error": None,
            "current_run_id": None,
            "session_id": None,
        }
    )
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban")
    # Wait for the board to load
    await user.should_see("Fix flaky test", retries=10)

    # Verify through the store layer (SQLite sort order) rather than
    # NiceGUI element traversal, which depends on rendering internals.
    store = web.current_store()
    tasks, _total = await store.kanban.list_tasks(
        status=None,
        limit=50,
        offset=0,
        status_order=CANONICAL_COLUMNS,
    )
    # Filter to just the three priority-2 ready tasks
    p2_ready = [t for t in tasks if t.priority == 2 and t.status == "ready"]
    ids = [t.id for t in p2_ready]
    # Newest first, then t_1 (middle), then oldest last
    assert ids == ["t_newest", "t_1", "t_oldest"], (
        f"Expected newest-first date sort within status, got order: {ids}"
    )
