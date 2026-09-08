"""Kanban plugin pages: a filterable task list + a per-task detail/edit view.

The board is deliberately *not* laid out as columns-side-by-side (Trello
style): that pattern needs horizontal panning that doesn't work on a phone,
and there's no drag-and-drop here anyway (moving a task is a "Move to"
select, same on every screen size). Instead ``/kanban`` renders one vertical
list of every task with its status as a colored tag, plus a row of filter
tabs and a free-text search box (``mark=kanban-search``) to narrow it down --
the same single-column list pattern the cron and sessions pages already use.
The search matches title, body, assignee, or task id server-side (the same
``LIKE`` search `KanbanStore.list_tasks` applies), fires debounced after the
user stops typing, and combines with the active status tab -- the pager's
total reflects the filtered set.

Mirrors ``plugins/cron/ui.py``'s shape (closures per mutation, wrapped in
``background_tasks.create`` so they can be awaited from a synchronous
``on_click``, patch the already-rendered UI in place via an ``on_updated``
callback): both reads (`list_tasks`/task detail) and writes go through
``web.current_store().kanban`` -- reads are direct SQLite against
`kanban.db`, writes delegate to `HermesExecutor`'s dashboard-REST methods
(kanban's CLI has no generic field-setter, unlike cron's, so its writes
couldn't move the way cron's did -- see `HermesExecutor`'s "kanban writes"
section), but the plugin itself only ever talks to the one store. The one
thing worth calling out: the dashboard's ``POST /tasks`` always creates a
task in the ``ready`` column, which the live dispatcher picks up within ~60s
and spawns a real agent run for -- there's no ``status`` field on create.
The "New task" dialog defaults its own status picker to the safer
``triage`` (parked, needs an explicit move) rather than matching that
server-side default, and issues a follow-up PATCH via
``KanbanStore.create``'s ``status=`` kwarg when the two differ.

One more create-time behavior: a task created with an **empty description**
is auto-specified in the background — the UI runs ``hermes kanban specify``
(the triage specifier: an auxiliary-LLM call that writes a Goal/Approach/
Acceptance-criteria body and promotes the task ``triage -> todo``) via
``HermesExecutor``, then reloads the board. The specifier only accepts
triage tasks (the dialog's default), so a non-triage creation with an empty
body just warns instead; the spec machinery is `hermes_cli`-style (CLI
subprocess), not a new HTTP client.
"""

from __future__ import annotations

import asyncio
import json
from functools import partial
from typing import Any, cast

from nicegui import background_tasks, ui

from hermes_nicegui import web
from hermes_nicegui.gateway import Comment, HermesError, Task
from hermes_nicegui.pagination import Pager, render_pager
from hermes_nicegui.plugin import Plugin
from hermes_nicegui.plugins.kanban.logic import (
    CANONICAL_COLUMNS,
    STAGE_HELP,
    column_meta,
    fmt_epoch,
    fmt_epoch_age,
    fmt_priority,
    parse_specify_outcome,
    should_auto_specify,
)
from hermes_nicegui.web import frame

_RUNNING_ICON_CSS = """
@keyframes kanban-running-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.kanban-running-icon {
  animation: kanban-running-spin 1.5s linear infinite;
  display: inline-block;
}
"""


def register_pages(plugin: Plugin) -> None:
    logger = plugin.logger
    executor = plugin.context.executor

    # A card created with an empty description is auto-fleshed-out by the
    # triage specifier (`hermes kanban specify`, an auxiliary-LLM call that
    # writes Goal/Approach/Acceptance criteria and promotes triage -> todo).
    # The specifier only accepts triage tasks, which is also the dialog's
    # default starting status; a non-triage creation with an empty body
    # can't use it, so the user gets a warning instead.
    AUTO_SPECIFY_TIMEOUT = 240  # the CLI's LLM call has its own 120s cap + retries

    def _auto_specify(task_id: str, on_updated: Any) -> None:
        """Run the triage specifier on a just-created task in the background.

        Fires the `hermes` CLI (the sanctioned mutation path for kanban.db)
        and reloads the board when it lands, so the new title/status/body
        show up. Never raises into the dialog handler; failures surface as
        notifications and the task stays in Triage.
        """

        async def do_specify() -> None:
            try:
                result = await asyncio.wait_for(
                    executor.run("kanban", "specify", task_id, "--json"),
                    timeout=AUTO_SPECIFY_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("kanban auto-specify {} timed out", task_id)
                ui.notify("Auto-specify timed out — task stays in Triage", type="negative")
                return
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("kanban auto-specify {} failed: {}", task_id, exc)
                ui.notify(f"Auto-specify failed: {exc}", type="negative")
                return
            ok, reason, _new_title = parse_specify_outcome(result.stdout)
            if ok:
                logger.debug("kanban auto-specify {} ok", task_id)
                ui.notify("Task created — spec auto-generated", type="positive")
            else:
                logger.info("kanban auto-specify {}: {}", task_id, reason)
                ui.notify(f"Auto-specify failed: {reason}", type="negative")
            on_updated(task_id)

        background_tasks.create(do_specify())

    def _create_task_dialog(on_created: Any) -> None:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-md"):
            ui.label("New task").classes("text-lg font-bold")
            title = (
                ui.input("Title").props("outlined dense").classes("w-full").mark("new-task-title")
            )
            body = (
                ui.textarea("Description")
                .props("outlined dense")
                .classes("w-full")
                .mark("new-task-body")
            )
            assignee = (
                ui.input("Assignee", value="default").props("outlined dense").classes("w-full")
            )
            priority = ui.number("Priority", value=2, min=0, max=5).props("outlined dense")
            status = (
                ui.select(CANONICAL_COLUMNS, value="triage", label="Initial status")
                .props("outlined dense")
                .classes("w-full")
                .mark("new-task-status")
            )
            ui.label(
                '"Ready" tasks are picked up by the live dispatcher within ~60s '
                'and spawn a real agent run. "Triage" stays parked until moved.'
            ).classes("text-xs opacity-60")

            async def do_create() -> None:
                new_title = (title.value or "").strip()
                if not new_title:
                    ui.notify("Title is required", type="warning")
                    return
                body_text = (body.value or "").strip()
                try:
                    task = await web.current_store().kanban.create(
                        title=new_title,
                        body=body.value or "",
                        assignee=(assignee.value or "default").strip() or "default",
                        priority=int(priority.value) if priority.value is not None else 2,
                        status=status.value,
                    )
                except HermesError as exc:
                    ui.notify(f"Create failed: {exc}", type="negative")
                    return
                dialog.close()
                ui.notify("Task created", type="positive")
                on_created(task)
                if should_auto_specify(body_text, status.value):
                    _auto_specify(task.id, on_created)
                elif not body_text:
                    ui.notify(
                        "Created with an empty description — add one on the task page",
                        type="warning",
                    )

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Create", on_click=lambda: background_tasks.create(do_create())).mark(
                    "create-task-confirm"
                )
        dialog.open()

    @ui.page("/kanban", title="Kanban")
    async def kanban_board_page() -> None:
        ui.add_css(_RUNNING_ICON_CSS)
        with frame(active="/kanban"):
            with ui.row().classes("w-full items-center gap-2"):
                ui.label("Kanban board").classes("text-lg")
                ui.space()
                ui.button(
                    icon="bolt", on_click=lambda: background_tasks.create(do_dispatch())
                ).props("flat round dense").mark("dispatch-button").tooltip(
                    "Start next ticket(s) — one at a time (per-profile cap)"
                )
                ui.button(
                    icon="refresh", on_click=lambda: background_tasks.create(_refresh())
                ).props("flat round dense").mark("refresh-button").tooltip("Refresh")
                ui.button(
                    "New task",
                    icon="add",
                    on_click=lambda: _create_task_dialog(
                        lambda _task: background_tasks.create(load_board())
                    ),
                ).mark("new-task-button")

            current_tasks: list[Task] = []
            total = 0
            pager = Pager()

            # `ui.tabs()`'s `value=` constructor kwarg is typed `Tab | TabPanel |
            # None` even though a plain tab-name string is documented and
            # supported at runtime -- set it as a plain attribute assignment
            # after construction instead, where it's typed against the wider
            # `ValueElement` generic that does include `str`.
            with ui.tabs().classes("w-full").mark("status-filter") as filter_tabs:
                ui.tab("all", label="All")
                for name in CANONICAL_COLUMNS:
                    label, icon, _color = column_meta(name)
                    ui.tab(name, label=label, icon=icon).mark(f"filter-tab-{name}")
            filter_tabs.value = "all"

            search_input = (
                ui.input(placeholder="Search tasks...")
                .props("outlined dense clearable debounce=500 icon=search")
                .classes("w-full")
                .mark("kanban-search")
            )

            with (
                ui.expansion("What each stage is for", icon="help_outline")
                .classes("w-full")
                .mark("stage-help-expansion")
            ):
                for name in CANONICAL_COLUMNS:
                    label, icon, color = column_meta(name)
                    with ui.row().classes("items-center gap-2"):
                        ui.icon(icon, color=color)
                        ui.label(f"{label} — {STAGE_HELP[name]}")

            list_container = ui.list().props("separator").classes("w-full")

            def render_task_row(task: Task) -> None:
                label, icon, color = column_meta(task.status)
                with (
                    ui.item(on_click=partial(ui.navigate.to, f"/kanban/{task.id}"))
                    .props("v-ripple")
                    .mark("task-card")
                ):
                    with ui.item_section().props("avatar"):
                        status_icon = ui.icon(icon, color=color).mark("task-status-icon")
                        if task.status == "running":
                            status_icon.classes("kanban-running-icon")
                    with ui.item_section():
                        ui.item_label(task.title or "(untitled)")
                        ui.item_label(
                            f"{task.assignee or '—'} · {fmt_priority(task.priority)}"
                        ).props("caption lines=1")
                    with ui.item_section().props("side top"):
                        ui.badge(label, color=color)
                        if task.comment_count:
                            ui.badge(str(task.comment_count), color="grey")

            def render_list() -> None:
                list_container.clear()
                with list_container:
                    if not current_tasks:
                        ui.label("No tasks.").classes("text-sm opacity-60 q-pa-sm")
                    for task in current_tasks:
                        render_task_row(task)
                    render_pager(pager, total, lambda: background_tasks.create(load_board()))

            def _tab_changed(e: Any) -> None:
                pager.reset()
                background_tasks.create(load_board())

            def _search_changed(e: Any) -> None:
                pager.reset()
                background_tasks.create(load_board())

            filter_tabs.on_value_change(_tab_changed)
            search_input.on_value_change(_search_changed)

            async def do_dispatch() -> None:
                try:
                    result = await web.current_executor().run("kanban", "dispatch", "--json")
                    if result.returncode != 0:
                        ui.notify(
                            f"Dispatch failed: exit {result.returncode}: {result.stderr[:500]}",
                            type="negative",
                        )
                        return
                    data = json.loads(result.stdout or "{}")
                    spawned = data.get("spawned") or []
                    deferred = data.get("skipped_per_profile_capped") or []
                    if spawned:
                        ui.notify(f"Started {len(spawned)} ticket(s)", type="positive")
                    elif deferred:
                        ui.notify(
                            f"{deferred[0].get('assignee')} at per-profile cap "
                            f"({deferred[0].get('current')} running) — tickets start when a slot frees",
                            type="warning",
                        )
                    else:
                        ui.notify("Dispatcher tick — nothing to start", type="positive")
                    await load_board()
                except Exception as exc:
                    ui.notify(f"Dispatch failed: {exc}", type="negative")

            async def load_board() -> None:
                nonlocal total
                status_filter = cast(str, filter_tabs.value) or "all"
                try:
                    current_tasks[:], total = await web.current_store().kanban.list_tasks(
                        status=None if status_filter == "all" else status_filter,
                        search=(search_input.value or "").strip() or None,
                        limit=pager.limit,
                        offset=pager.offset,
                        **({"status_order": CANONICAL_COLUMNS} if status_filter == "all" else {}),
                    )
                except HermesError as exc:
                    ui.notify(f"Failed to load board: {exc}", type="negative")
                    return
                render_list()

            async def _refresh() -> None:
                pager.reset()
                await load_board()

            await load_board()
            logger.debug("kanban board rendered")

    def _move_status(task_id: str, new_status: str, on_updated: Any) -> None:
        async def do_move() -> None:
            try:
                updated = await web.current_store().kanban.move_status(task_id, new_status)
            except HermesError as exc:
                ui.notify(f"Move failed: {exc}", type="negative")
                return
            ui.notify(f"Moved to {column_meta(new_status)[0]}", type="positive")
            on_updated(updated)

        background_tasks.create(do_move())

    def _delete_task(task_id: str, on_deleted: Any) -> None:
        async def do_delete() -> None:
            try:
                await web.current_store().kanban.delete(task_id)
            except HermesError as exc:
                ui.notify(f"Delete failed: {exc}", type="negative")
                return
            ui.notify("Task deleted", type="positive")
            on_deleted()

        background_tasks.create(do_delete())

    def _save_fields(task_id: str, fields: dict[str, Any], on_updated: Any) -> None:
        async def do_save() -> None:
            try:
                updated = await web.current_store().kanban.save_fields(task_id, fields)
            except HermesError as exc:
                ui.notify(f"Save failed: {exc}", type="negative")
                return
            ui.notify("Task saved", type="positive")
            on_updated(updated)

        background_tasks.create(do_save())

    def _add_comment(task_id: str, body: str, on_added: Any) -> None:
        async def do_add() -> None:
            if not body.strip():
                ui.notify("Comment can't be empty", type="warning")
                return
            try:
                await web.current_store().kanban.add_comment(task_id, body)
            except HermesError as exc:
                ui.notify(f"Comment failed: {exc}", type="negative")
                return
            on_added()

        background_tasks.create(do_add())

    @ui.page("/kanban/{task_id}", title="Task")
    async def kanban_detail_page(task_id: str) -> None:
        with frame(active="/kanban"):
            try:
                detail = await web.current_store().kanban.get_task_detail(task_id)
            except HermesError as exc:
                ui.label(f"Failed to load task: {exc}")
                return
            task = detail.task

            def stats_text(t: Task) -> str:
                parts = [f"Created {fmt_epoch_age(t.created_at)}"]
                if t.started_at:
                    parts.append(f"started {fmt_epoch_age(t.started_at)}")
                if t.completed_at:
                    parts.append(f"completed {fmt_epoch_age(t.completed_at)}")
                if t.consecutive_failures:
                    parts.append(f"{t.consecutive_failures} consecutive failures")
                return " · ".join(parts)

            with ui.card().classes("w-full"):
                with ui.row().classes("items-center"):
                    title_label = ui.label(task.title or "(untitled)").classes("text-lg font-bold")
                    ui.space()
                    label, _icon, color = column_meta(task.status)
                    status_badge = ui.badge(label, color=color)
                stats_label = ui.label(stats_text(task)).classes("text-sm opacity-70")
                failure_label = ui.label(task.last_failure_error or "").classes(
                    "text-xs text-negative"
                )
                failure_label.set_visibility(bool(task.last_failure_error))

                with ui.row().classes("items-center gap-2"):
                    move_options = (
                        CANONICAL_COLUMNS
                        if task.status in CANONICAL_COLUMNS
                        else [*CANONICAL_COLUMNS, task.status]
                    )
                    move_select = (
                        ui.select(move_options, value=task.status, label="Move to")
                        .props("outlined dense")
                        .classes("w-48")
                        .mark("move-status-select")
                    )
                    delete_button = ui.button("Delete", icon="delete", color="negative").mark(
                        "delete-task-button"
                    )
                    # "View session" must point at the session *doing the
                    # work* -- the dispatcher's kanban-tagged worker session
                    # (resolved from state.db by `KanbanStore`) -- not the
                    # task row's `session_id`, which is only the session that
                    # *created* the task. Fall back to the origin session
                    # while no worker session exists yet (task never
                    # claimed); when both exist and differ, keep the origin
                    # reachable as a secondary button.
                    worker_session_id = detail.worker_session_id
                    if worker_session_id:
                        ui.button(
                            "View session",
                            icon="terminal",
                            on_click=partial(
                                ui.navigate.to, f"/sessions/{worker_session_id}"
                            ),
                        ).props("outline").mark("view-session-button").tooltip(
                            "Session doing the work"
                        )
                        if task.session_id and task.session_id != worker_session_id:
                            ui.button(
                                "Origin",
                                icon="chat",
                                on_click=partial(
                                    ui.navigate.to, f"/sessions/{task.session_id}"
                                ),
                            ).props("flat dense").mark("view-origin-session-button").tooltip(
                                "Session that created this task"
                            )
                    elif task.session_id:
                        ui.button(
                            "View session",
                            icon="terminal",
                            on_click=partial(ui.navigate.to, f"/sessions/{task.session_id}"),
                        ).props("outline").mark("view-session-button").tooltip(
                            "Origin session (no worker session yet)"
                        )

            if detail.runs:
                with ui.expansion(f"Runs ({len(detail.runs)})", icon="history").classes("w-full"):
                    for run in detail.runs:
                        run_id = run.get("id") or run.get("run_id") or "—"
                        run_status = run.get("status") or "unknown"
                        with ui.row().classes("items-center gap-2"):
                            ui.label(str(run_id)).classes("font-mono text-sm")
                            ui.badge(str(run_status), color="grey")
                            started = run.get("started_at")
                            if started:
                                ui.label(fmt_epoch_age(started)).classes("text-xs opacity-60")

            def apply_update(updated: Task) -> None:
                nonlocal task
                task = updated
                title_label.set_text(task.title or "(untitled)")
                label, _icon, color = column_meta(task.status)
                status_badge.set_text(label)
                status_badge.set_background_color(color)
                stats_label.set_text(stats_text(task))
                failure_label.set_text(task.last_failure_error or "")
                failure_label.set_visibility(bool(task.last_failure_error))
                move_select.set_value(task.status)
                body_preview.set_content(task.body or "")
                body_preview.set_visibility(bool(task.body))

            def on_move_change(event: Any) -> None:
                if event.value and event.value != task.status:
                    _move_status(task_id, event.value, apply_update)

            move_select.on_value_change(on_move_change)
            delete_button.on_click(
                lambda: _delete_task(
                    task_id,
                    # After a delete, land back where the task's work
                    # happened: worker session first, origin session second,
                    # board as fallback (mirrors "View session" precedence).
                    lambda: ui.navigate.to(
                        f"/sessions/{detail.worker_session_id}"
                        if detail.worker_session_id
                        else f"/sessions/{task.session_id}"
                        if task.session_id
                        else "/kanban"
                    ),
                )
            )

            title_input = (
                ui.input("Title", value=task.title)
                .props("outlined dense")
                .classes("w-full")
                .mark("task-title-input")
            )
            body_preview = ui.markdown(task.body or "").classes("w-full").mark("task-body-rendered")
            body_preview.set_visibility(bool(task.body))
            body_input = (
                ui.textarea("Description", value=task.body or "")
                .props("outlined dense")
                .classes("w-full")
                .mark("task-body-input")
            )
            with ui.row().classes("w-full gap-2"):
                assignee_input = (
                    ui.input("Assignee", value=task.assignee or "")
                    .props("outlined dense")
                    .classes("w-full")
                    .mark("task-assignee-input")
                )
                priority_input = (
                    ui.number("Priority", value=task.priority, min=0, max=5)
                    .props("outlined dense")
                    .classes("w-full")
                    .mark("task-priority-input")
                )

            def save_fields() -> None:
                fields: dict[str, Any] = {
                    "title": title_input.value or "",
                    "body": body_input.value or "",
                    "assignee": (assignee_input.value or "").strip() or "default",
                    "priority": (
                        int(priority_input.value) if priority_input.value is not None else 2
                    ),
                }
                _save_fields(task_id, fields, apply_update)

            ui.button("Save", icon="save", on_click=save_fields).mark("save-task-button")

            ui.label("Comments").classes("text-lg font-bold q-mt-md")
            comments_container = ui.column().classes("w-full")

            def render_comments(comments: list[Comment]) -> None:
                comments_container.clear()
                with comments_container:
                    if not comments:
                        ui.label("No comments yet.").classes("text-sm opacity-60")
                    for comment in comments:
                        with ui.card().classes("w-full"):
                            with ui.row().classes("items-center gap-2"):
                                ui.label(comment.author or "—").classes("font-bold text-sm")
                                ui.label(fmt_epoch(comment.created_at)).classes(
                                    "text-xs opacity-60"
                                )
                            ui.markdown(comment.body).classes("w-full").mark("comment-body")

            render_comments(detail.comments)

            comment_input = (
                ui.textarea("Add a comment")
                .props("outlined dense")
                .classes("w-full")
                .mark("comment-input")
            )

            async def reload_comments() -> None:
                try:
                    fresh = await web.current_store().kanban.get_task_detail(task_id)
                except HermesError as exc:
                    ui.notify(f"Failed to refresh comments: {exc}", type="negative")
                    return
                comment_input.set_value("")
                render_comments(fresh.comments)

            ui.button(
                "Add comment",
                icon="add_comment",
                on_click=lambda: _add_comment(
                    task_id,
                    comment_input.value or "",
                    lambda: background_tasks.create(reload_comments()),
                ),
            ).mark("add-comment-button")

            logger.debug("kanban detail rendered for {}", task_id)
