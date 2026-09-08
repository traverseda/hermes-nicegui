"""Tests for KanbanStore.list_tasks search filtering: the `LIKE` search that
narrows the board the way SessionsStore's does, against the real `kanban.db`
fixture `fake_kanban` seeds under `hermes_home`."""

from __future__ import annotations

import sqlite3

from hermes_nicegui.executor import HermesExecutor
from hermes_nicegui.store import build_store


def _insert_done_task(fake_kanban, task_id: str, title: str) -> None:
    fake_kanban.insert_task(
        {
            "id": task_id,
            "title": title,
            "body": "",
            "assignee": "default",
            "status": "done",
            "priority": 1,
            "tenant": None,
            "created_at": 1786620100,
            "started_at": None,
            "completed_at": 1786620300,
            "consecutive_failures": 0,
            "last_failure_error": None,
            "current_run_id": None,
            "session_id": None,
        }
    )


async def test_kanban_list_tasks_search_filters_and_totals(
    executor: HermesExecutor, fake_kanban
) -> None:
    """`search=` narrows the returned rows *and* the count the same way, so
    pagination totals stay correct under a query."""
    _insert_done_task(fake_kanban, "t_cleanup", "Clean up backlog")
    store = build_store(executor, "").kanban

    tasks, total = await store.list_tasks(search="flaky")
    assert [t.id for t in tasks] == ["t_1"]
    assert total == 1

    tasks, total = await store.list_tasks(search="zzz-nonexistent")
    assert tasks == []
    assert total == 0

    tasks, total = await store.list_tasks(search="backlog")
    assert [t.id for t in tasks] == ["t_cleanup"]
    assert total == 1


async def test_kanban_list_tasks_search_combines_with_status(
    executor: HermesExecutor, fake_kanban
) -> None:
    """`search` and `status` combine (not OR): a query matching only a `done`
    task finds nothing under `status="ready"`."""
    _insert_done_task(fake_kanban, "t_legacy", "Retire the legacy flaky report")
    store = build_store(executor, "").kanban

    tasks, total = await store.list_tasks(search="legacy")
    assert [t.id for t in tasks] == ["t_legacy"]
    assert total == 1

    tasks, total = await store.list_tasks(search="legacy", status="ready")
    assert tasks == []
    assert total == 0

    tasks, total = await store.list_tasks(search="flaky", status="ready")
    assert [t.id for t in tasks] == ["t_1"]
    assert total == 1


async def test_kanban_list_tasks_search_matches_comments(
    executor: HermesExecutor, fake_kanban
) -> None:
    """A query matched only inside a task's comment body still surfaces the
    task -- the `EXISTS` over `task_comments` in the search clause."""
    fake_kanban.insert_task(
        {
            "id": "t_docs",
            "title": "Ship release notes",
            "body": "No trace of the keyword here",
            "assignee": "default",
            "status": "ready",
            "priority": 1,
            "tenant": None,
            "created_at": 1786620100,
            "started_at": None,
            "completed_at": None,
            "consecutive_failures": 0,
            "last_failure_error": None,
            "current_run_id": None,
            "session_id": None,
        }
    )
    con = sqlite3.connect(fake_kanban.db_path)
    con.execute(
        "INSERT INTO task_comments (id, task_id, author, body, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (50, "t_docs", "bot", "the integration is flaky under load", 1786620200),
    )
    con.commit()
    con.close()

    tasks, total = await build_store(executor, "").kanban.list_tasks(search="flaky")
    assert {t.id for t in tasks} == {"t_1", "t_docs"}
    assert total == 2
