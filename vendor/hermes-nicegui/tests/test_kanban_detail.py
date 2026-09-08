"""Tests for the kanban task detail/edit page."""

from __future__ import annotations

from typing import cast

from nicegui import ui
from nicegui.testing import User

from hermes_nicegui import web
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.kanban import KanbanPlugin
from hermes_nicegui.plugins.sessions import SessionsPlugin

TASK_ID = "t_1"


async def test_detail_page_loads_directly(
    user: User, kanban_context: PluginContext
) -> None:
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open(f"/kanban/{TASK_ID}")
    await user.should_see("Fix flaky test")


async def test_detail_page_shows_comments(
    user: User, kanban_context: PluginContext
) -> None:
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open(f"/kanban/{TASK_ID}")
    await user.should_see("Looking into it")


async def test_body_renders_as_markdown(
    user: User, kanban_context: PluginContext
) -> None:
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open(f"/kanban/{TASK_ID}")

    elements = list(user.find(marker="task-body-rendered").elements)
    assert len(elements) == 1
    assert isinstance(elements[0], ui.markdown)
    assert "Goal" in elements[0].content
    await user.should_see("Goal")


async def test_comments_render_as_markdown(
    user: User, kanban_context: PluginContext
) -> None:
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open(f"/kanban/{TASK_ID}")

    elements = list(user.find(marker="comment-body").elements)
    assert len(elements) >= 1
    assert all(isinstance(element, ui.markdown) for element in elements)


async def test_detail_page_has_actions(
    user: User, kanban_context: PluginContext
) -> None:
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open(f"/kanban/{TASK_ID}")
    assert user.find(marker="move-status-select").elements
    assert user.find(marker="delete-task-button").elements


async def test_move_status_updates_badge(
    user: User, kanban_context: PluginContext
) -> None:
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open(f"/kanban/{TASK_ID}")
    await user.should_see("Fix flaky test")

    select = cast(ui.select, next(iter(user.find(marker="move-status-select").elements)))
    select.set_value("done")

    await user.should_see("Done", retries=10)


async def test_add_comment_appends_to_list(
    user: User, kanban_context: PluginContext
) -> None:
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open(f"/kanban/{TASK_ID}")
    await user.should_see("Looking into it")

    user.find(marker="comment-input").type("On it now")
    user.find(marker="add-comment-button").click()

    await user.should_see("On it now", retries=10)


async def test_delete_navigates_to_origin_session(
    user: User,
    kanban_context: PluginContext,
    hermes_home,
) -> None:
    """Deleting a task lands back on the task's session page -- the origin
    session (`sess-99`), since the fixture task has no worker session --
    instead of always dropping to the board."""
    import sqlite3

    con = sqlite3.connect(hermes_home / "state.db")
    con.execute(
        "INSERT INTO sessions (id, title, source, model, started_at, ended_at, end_reason,"
        " message_count, tool_call_count, input_tokens, output_tokens, estimated_cost_usd,"
        " pinned, archived, last_activity_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "sess-99", "Session for deleted task", "kanban", "deepseek-v4-flash",
            None, None, None, 0, 0, 0, 0, None, 0, 0, 1786620900.0,
        ),
    )
    con.commit()
    con.close()

    web.build(
        kanban_context,
        [
            KanbanPlugin(kanban_context),
            SessionsPlugin(kanban_context),
        ],
    )
    await user.open(f"/kanban/{TASK_ID}")
    await user.should_see("Fix flaky test")
    user.find(marker="delete-task-button").click()
    await user.should_see("Session for deleted task", retries=10)


async def test_delete_without_session_falls_back_to_board(
    user: User,
    kanban_context: PluginContext,
    fake_kanban,
) -> None:
    """A task with no `session_id` (CLI/dashboard-created) still falls back to
    the board after delete."""
    fake_kanban.insert_task(
        {
            "id": "t_nosess",
            "title": "No-session task",
            "body": "",
            "assignee": "default",
            "status": "ready",
            "priority": 2,
            "created_at": 1786620003,
            "started_at": None,
            "completed_at": None,
            "consecutive_failures": 0,
            "last_failure_error": None,
            "current_run_id": None,
            "session_id": None,
            "latest_summary": None,
            "comment_count": 0,
        }
    )
    fake_kanban.tasks.append(
        {
            "id": "t_nosess",
            "title": "No-session task",
            "body": "",
            "assignee": "default",
            "status": "ready",
            "priority": 2,
            "created_at": 1786620003,
            "started_at": None,
            "completed_at": None,
            "consecutive_failures": 0,
            "last_failure_error": None,
            "current_run_id": None,
            "session_id": None,
            "latest_summary": None,
            "comment_count": 0,
        }
    )

    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban/t_nosess")
    await user.should_see("No-session task")
    user.find(marker="delete-task-button").click()
    await user.should_see("Kanban board", retries=10)
    await user.should_not_see("No-session task", retries=10)


async def test_view_session_button_shown_when_task_has_session(
    user: User, kanban_context: PluginContext
) -> None:
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open(f"/kanban/{TASK_ID}")
    await user.should_see("Fix flaky test")
    assert user.find(marker="view-session-button").elements


async def test_view_session_links_to_worker_session_not_spawning(
    user: User,
    kanban_context: PluginContext,
    hermes_home,
) -> None:
    """The fixture task's row records `sess-99` (the spawning session) but a
    kanban-tagged worker session also exists: "View session" must prefer the
    worker, and the origin must be reachable as a separate button -- not the
    other way around (the bug this fixes)."""
    import sqlite3

    con = sqlite3.connect(hermes_home / "state.db")
    con.execute(
        "INSERT INTO sessions (id, title, source, model, started_at, ended_at, end_reason,"
        " message_count, tool_call_count, input_tokens, output_tokens, estimated_cost_usd,"
        " pinned, archived, last_activity_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "worker-1", "work kanban task t_1", "kanban", "deepseek-v4-flash",
            None, None, None, 0, 0, 0, 0, None, 0, 0, 1786620900.0,
        ),
    )
    con.commit()
    con.close()

    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open(f"/kanban/{TASK_ID}")
    await user.should_see("Fix flaky test")
    # Primary button targets the worker session; the spawning session is the
    # secondary "Origin" button.
    assert user.find(marker="view-session-button").elements
    assert user.find(marker="view-origin-session-button").elements


async def test_no_origin_button_without_spawning_session(
    user: User,
    kanban_context: PluginContext,
    fake_kanban,
    hermes_home,
) -> None:
    """Task created from the CLI/dashboard has no `session_id`: with a worker
    session present only the primary button renders (no Origin button)."""
    import sqlite3

    fake_kanban.insert_task(
        {
            "id": "t_cli",
            "title": "CLI-created task",
            "body": "",
            "assignee": "default",
            "status": "done",
            "priority": 2,
            "created_at": 1786620001,
            "started_at": None,
            "completed_at": None,
            "consecutive_failures": 0,
            "last_failure_error": None,
            "current_run_id": None,
            "session_id": None,
            "latest_summary": None,
            "comment_count": 0,
        }
    )
    con = sqlite3.connect(hermes_home / "state.db")
    con.execute(
        "INSERT INTO sessions (id, title, source, model, started_at, ended_at, end_reason,"
        " message_count, tool_call_count, input_tokens, output_tokens, estimated_cost_usd,"
        " pinned, archived, last_activity_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "worker-cli", "work kanban task t_cli", "kanban", "deepseek-v4-flash",
            None, None, None, 0, 0, 0, 0, None, 0, 0, 1786620901.0,
        ),
    )
    con.commit()
    con.close()

    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban/t_cli")
    await user.should_see("CLI-created task")
    assert user.find(marker="view-session-button").elements
    await user.should_not_see("Origin", retries=10)


async def test_body_preview_hidden_when_empty(
    user: User,
    kanban_context: PluginContext,
    fake_kanban,
) -> None:
    fake_kanban.insert_task(
        {
            "id": "t_empty",
            "title": "Empty task",
            "body": "",
            "assignee": "default",
            "status": "ready",
            "priority": 2,
            "created_at": 1786620002,
            "started_at": None,
            "completed_at": None,
            "consecutive_failures": 0,
            "last_failure_error": None,
            "current_run_id": None,
            "session_id": None,
            "latest_summary": None,
            "comment_count": 0,
        }
    )

    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open("/kanban/t_empty")
    await user.should_not_see(marker="task-body-rendered")


async def test_runs_section_shows_run_status(
    user: User, kanban_context: PluginContext
) -> None:
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open(f"/kanban/{TASK_ID}")
    await user.should_see("Runs (1)")
    await user.should_see("completed")


async def test_save_fields_updates_title(
    user: User, kanban_context: PluginContext
) -> None:
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open(f"/kanban/{TASK_ID}")
    await user.should_see("Fix flaky test")

    title_input = user.find(marker="task-title-input")
    title_input.clear()
    title_input.type("Renamed task")
    user.find(marker="save-task-button").click()

    await user.should_see("Renamed task", retries=10)


async def test_body_preview_updates_after_save(
    user: User, kanban_context: PluginContext
) -> None:
    web.build(kanban_context, [KanbanPlugin(kanban_context)])
    await user.open(f"/kanban/{TASK_ID}")

    body_input = user.find(marker="task-body-input")
    body_input.clear()
    body_input.type("## Updated goal")
    user.find(marker="save-task-button").click()

    await user.should_see(marker="task-body-rendered", content="## Updated goal", retries=10)
    elements = list(user.find(marker="task-body-rendered").elements)
    assert len(elements) == 1
    assert "Updated goal" in elements[0].content
