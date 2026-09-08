"""Tests for hermes_nicegui.store: the SessionsStore/CronStore/KanbanStore
facade, against the real on-disk fixtures `fake_hermes_cli`/`fake_kanban`
seed under `hermes_home` (see tests/conftest.py) plus the fake `hermes`
CLI-subprocess mutation boundary."""

from __future__ import annotations

from hermes_nicegui.executor import HermesExecutor
from hermes_nicegui.plugins.kanban.logic import CANONICAL_COLUMNS
from hermes_nicegui.store import build_store


async def test_sessions_list_includes_preview_and_last_active(executor: HermesExecutor) -> None:
    sessions, total = await build_store(executor, "").sessions.list()
    assert [s.id for s in sessions] == ["sess-1", "sess-2"]
    assert sessions[0].preview == "How do I access your API?"
    assert sessions[0].last_active == 1786620319.0
    assert total == 2


async def test_sessions_list_paginates_and_searches(executor: HermesExecutor) -> None:
    store = build_store(executor, "")
    sessions, total = await store.sessions.list(limit=1, offset=0)
    assert [s.id for s in sessions] == ["sess-1"]
    assert total == 2

    sessions, total = await store.sessions.list(limit=1, offset=1)
    assert [s.id for s in sessions] == ["sess-2"]
    assert total == 2

    sessions, total = await store.sessions.list(search="cron")
    assert [s.id for s in sessions] == ["sess-2"]
    assert total == 1


async def test_sessions_get_session_returns_row(executor: HermesExecutor) -> None:
    session = await build_store(executor, "").sessions.get_session("sess-1")
    assert session.id == "sess-1"
    assert session.title == "First session"


async def test_sessions_get_messages_page_parses_tool_calls(executor: HermesExecutor) -> None:
    messages, has_earlier = await build_store(executor, "").sessions.get_messages_page("sess-1")
    assert len(messages) == 3
    assert has_earlier is False
    assert messages[0].role == "user"
    tool_calls = messages[2].tool_calls
    assert tool_calls is not None
    assert tool_calls[0]["function"]["name"] == "terminal"


async def test_sessions_get_messages_page_paginates_backward(
    executor: HermesExecutor, fake_hermes_cli
) -> None:
    """Seed a session with more messages than a small page size to exercise
    the `before_id` keyset walk a "Load earlier" click drives."""
    import sqlite3

    con = sqlite3.connect(fake_hermes_cli._state_db_path())
    con.executemany(
        "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        [(i, "sess-1", "user", f"msg {i}", 1786620000.0 + i) for i in range(4, 8)],
    )
    con.commit()
    con.close()

    store = build_store(executor, "").sessions
    newest, has_earlier = await store.get_messages_page("sess-1", limit=3)
    assert [m.id for m in newest] == [5, 6, 7]
    assert has_earlier is True

    older, has_earlier = await store.get_messages_page("sess-1", before_id=5, limit=3)
    assert [m.id for m in older] == [2, 3, 4]
    assert has_earlier is True

    oldest, has_earlier = await store.get_messages_page("sess-1", before_id=2, limit=3)
    assert [m.id for m in oldest] == [1]
    assert has_earlier is False


async def test_sessions_rename_and_delete_go_through_cli(
    executor: HermesExecutor, fake_hermes_cli
) -> None:
    store = build_store(executor, "").sessions
    await store.rename("sess-1", "New title")
    sessions, _total = await store.list()
    assert next(s for s in sessions if s.id == "sess-1").title == "New title"

    await store.delete("sess-2")
    sessions, total = await store.list()
    assert [s.id for s in sessions] == ["sess-1"]
    assert total == 1


async def test_cron_list_jobs_reads_jobs_json(executor: HermesExecutor) -> None:
    jobs, total = await build_store(executor, "").cron.list_jobs()
    assert [j.id for j in jobs] == ["aabbccddeeff"]
    assert jobs[0].name == "Nightly report"
    assert total == 1


async def test_cron_save_fields_edits_then_rereads(
    executor: HermesExecutor, fake_hermes_cli
) -> None:
    store = build_store(executor, "").cron
    updated = await store.save_fields("aabbccddeeff", {"name": "Renamed job"})
    assert updated.name == "Renamed job"
    assert fake_hermes_cli.jobs_actions == [
        ("cron", "edit", "aabbccddeeff", "--name", "Renamed job")
    ]


async def test_kanban_list_tasks_filters_by_status(executor: HermesExecutor, fake_kanban) -> None:
    """`fake_kanban` is unused directly -- requesting it is what seeds
    `kanban.db` under the same `hermes_home` `executor` reads from."""
    store = build_store(executor, "").kanban
    tasks, total = await store.list_tasks(status="ready")
    assert [t.id for t in tasks] == ["t_1"]
    assert tasks[0].comment_count == 1
    assert total == 1

    tasks, total = await store.list_tasks(status="done")
    assert tasks == []
    assert total == 0

    tasks, total = await store.list_tasks()
    assert [t.id for t in tasks] == ["t_1"]
    assert total == 1


async def test_kanban_list_tasks_orders_all_by_status_order(
    executor: HermesExecutor, fake_kanban
) -> None:
    for task in [
        {"id": "t_1", "status": "done", "priority": 1, "created_at": 400},
        {"id": "t_ready_1", "status": "ready", "priority": 1, "created_at": 300},
        {"id": "t_triage", "status": "triage", "priority": 3, "created_at": 100},
        {"id": "t_ready_2", "status": "ready", "priority": 2, "created_at": 200},
        {"id": "t_blocked", "status": "blocked", "priority": 1, "created_at": 250},
        {"id": "t_review", "status": "review", "priority": 1, "created_at": 260},
        {"id": "t_unknown", "status": "weird", "priority": 5, "created_at": 500},
    ]:
        fake_kanban.insert_task(task)

    tasks, total = await build_store(executor, "").kanban.list_tasks(
        status_order=CANONICAL_COLUMNS
    )

    assert [task.id for task in tasks] == [
        "t_triage",
        "t_ready_1",
        "t_ready_2",
        "t_review",
        "t_blocked",
        "t_1",
        "t_unknown",
    ]
    assert total == 7


async def test_kanban_list_tasks_without_status_order_sorts_by_created_at(
    executor: HermesExecutor, fake_kanban
) -> None:
    """With no `status_order`, tasks sort purely by created_at descending
    (newest first) -- priority is not part of the sort at all."""
    for task in [
        {"id": "t_1", "status": "done", "priority": 1, "created_at": 400},
        {"id": "t_ready_1", "status": "ready", "priority": 1, "created_at": 300},
        {"id": "t_triage", "status": "triage", "priority": 3, "created_at": 100},
        {"id": "t_ready_2", "status": "ready", "priority": 2, "created_at": 200},
        {"id": "t_unknown", "status": "weird", "priority": 5, "created_at": 500},
    ]:
        fake_kanban.insert_task(task)

    tasks, total = await build_store(executor, "").kanban.list_tasks()

    assert [task.id for task in tasks] == [
        "t_unknown",
        "t_1",
        "t_ready_1",
        "t_ready_2",
        "t_triage",
    ]
    assert total == 5

async def test_kanban_get_task_detail_includes_comments_and_runs(
    executor: HermesExecutor, fake_kanban
) -> None:
    detail = await build_store(executor, "").kanban.get_task_detail("t_1")
    assert detail.task.title == "Fix flaky test"
    assert [c.body for c in detail.comments] == ["Looking into it\n\n**Investigating**"]
    assert detail.runs[0]["status"] == "completed"


def _seed_worker_session(hermes_home, session_id: str, title: str, last_activity: float) -> None:
    """Insert a kanban-tagged worker session row into the fake state.db."""
    import sqlite3

    con = sqlite3.connect(hermes_home / "state.db")
    con.execute(
        "INSERT INTO sessions (id, title, source, model, started_at, ended_at, end_reason,"
        " message_count, tool_call_count, input_tokens, output_tokens, estimated_cost_usd,"
        " pinned, archived, last_activity_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id, title, "kanban", "deepseek-v4-flash",
            None, None, None, 0, 0, 0, 0, None, 0, 0, last_activity,
        ),
    )
    con.commit()
    con.close()


async def test_kanban_get_task_detail_resolves_worker_session(
    executor: HermesExecutor, fake_kanban, hermes_home
) -> None:
    """The detail's `worker_session_id` must be the dispatcher's kanban
    session doing the work (matched by title), not the task row's spawning
    `session_id` -- that's the whole point of the field."""
    _seed_worker_session(hermes_home, "worker-1", "work kanban task t_1", 1786620900.0)

    detail = await build_store(executor, "").kanban.get_task_detail("t_1")
    assert detail.task.session_id == "sess-99"  # spawning session, unchanged
    assert detail.worker_session_id == "worker-1"


async def test_kanban_get_task_detail_worker_session_picks_latest_run(
    executor: HermesExecutor, fake_kanban, hermes_home
) -> None:
    """A retried task gets one kanban session per run (" #N" suffix); the
    resolution must return the most recent one."""
    _seed_worker_session(hermes_home, "worker-1", "work kanban task t_1", 1786620800.0)
    _seed_worker_session(hermes_home, "worker-2", "work kanban task t_1 #2", 1786620900.0)

    detail = await build_store(executor, "").kanban.get_task_detail("t_1")
    assert detail.worker_session_id == "worker-2"


async def test_kanban_get_task_detail_worker_session_none_when_unclaimed(
    executor: HermesExecutor, fake_kanban
) -> None:
    """No kanban session for the task (never claimed) -> None, so the UI
    falls back to the spawning session."""
    detail = await build_store(executor, "").kanban.get_task_detail("t_1")
    assert detail.task.session_id == "sess-99"
    assert detail.worker_session_id is None
