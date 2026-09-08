"""Cron plugin pages: job list + a per-job detail/edit view + a per-run
detail view.

Listing reads `cron/jobs.json` directly; create/pause/resume/run/remove/edit
go through the `hermes` CLI -- both via `web.current_store().cron` (see
`hermes_nicegui.store`), scoped to whichever profile is active for this
browser tab.

The detail page offers two ways to edit a job: a plain form over the core
fields (name, schedule, prompt, deliver, skills, repeat), and a raw-YAML tab
(``ui.codemirror``) over the job's full record. Both funnel through
`CronStore.save_fields`, which only ever passes `hermes cron edit` the flags
for fields actually present -- editing via YAML can't set anything the form
couldn't (the CLI has no generic field-setter), which mirrors what the old
dashboard-REST path's own server-side whitelist already limited it to.

The run detail page (`/cron/{job_id}/runs/{run_id}`, reachable by clicking a
row on the detail page's "Runs" tab) shows one execution attempt: its
metadata, the output markdown the scheduler saved under
`cron/output/{job_id}/` when there is one, and a link to the state.db session
the run spawned -- both resolved by `CronStore.find_run_output`/
`find_related_session_id`, matched by wall-clock timestamp proximity since
neither filename nor session id records the run's own id.
"""

from __future__ import annotations

from functools import partial
from typing import Any

import yaml
from nicegui import background_tasks, ui

from hermes_nicegui import web
from hermes_nicegui.gateway import CronRun, HermesError, Job
from hermes_nicegui.pagination import Pager, render_pager
from hermes_nicegui.plugin import Plugin
from hermes_nicegui.plugins.cron.logic import (
    fmt_duration,
    fmt_iso,
    fmt_iso_age,
    fmt_repeat,
    job_to_yaml,
    run_status_icon,
    skills_to_text,
    state_icon,
    text_to_skills,
    yaml_to_job_fields,
)
from hermes_nicegui.web import frame


def register_pages(plugin: Plugin) -> None:
    logger = plugin.logger

    def _create_job_dialog(on_created: Any) -> None:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-md"):
            ui.label("New cron job").classes("text-lg font-bold")
            name = ui.input("Name").props("outlined dense").classes("w-full").mark("new-job-name")
            schedule = (
                ui.input(
                    "Schedule",
                    placeholder="every 5m, */5 * * * *, or an ISO timestamp",
                )
                .props("outlined dense")
                .classes("w-full")
                .mark("new-job-schedule")
            )
            prompt = ui.textarea("Prompt").props("outlined dense").classes("w-full")
            deliver = (
                ui.input("Deliver", placeholder="local").props("outlined dense").classes("w-full")
            )
            skills = (
                ui.input("Skills", placeholder="comma-separated")
                .props("outlined dense")
                .classes("w-full")
            )
            repeat = (
                ui.number("Repeat (blank = forever)", min=1)
                .props("outlined dense")
                .classes("w-full")
            )

            async def do_create() -> None:
                new_name = (name.value or "").strip()
                new_schedule = (schedule.value or "").strip()
                if not new_name or not new_schedule:
                    ui.notify("Name and schedule are required", type="warning")
                    return
                try:
                    await web.current_store().cron.create(
                        name=new_name,
                        schedule=new_schedule,
                        prompt=prompt.value or "",
                        deliver=(deliver.value or "").strip() or None,
                        skills=text_to_skills(skills.value or ""),
                        repeat=int(repeat.value) if repeat.value else None,
                    )
                except HermesError as exc:
                    ui.notify(f"Create failed: {exc}", type="negative")
                    return
                dialog.close()
                ui.notify("Job created", type="positive")
                on_created()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Create", on_click=lambda: background_tasks.create(do_create())).mark(
                    "create-job-confirm"
                )
        dialog.open()

    @ui.page("/cron", title="Cron Jobs")
    async def cron_list_page() -> None:
        with frame(active="/cron"):
            with ui.row().classes("w-full items-center gap-2"):
                ui.label("Scheduled jobs").classes("text-lg")
                ui.space()
                ui.button(
                    icon="refresh", on_click=lambda: background_tasks.create(_refresh())
                ).props("flat round dense").mark("refresh-button").tooltip("Refresh")
                ui.button(
                    "New job",
                    icon="add",
                    on_click=lambda: _create_job_dialog(
                        lambda: background_tasks.create(load_list())
                    ),
                ).mark("new-job-button")

            list_container = ui.list().props("separator").classes("w-full")
            pager = Pager()

            def render_job_row(job: Job) -> None:
                icon, color = state_icon(job.state)

                def _confirm_delete() -> None:
                    with ui.dialog() as dialog, ui.card().classes("w-full max-w-xs"):
                        ui.label(f"Delete job '{job.name or job.id}'?").classes(
                            "text-lg font-bold"
                        )
                        ui.label("This action cannot be undone.").classes("text-sm opacity-70")
                        with ui.row().classes("w-full justify-end gap-2"):
                            ui.button("Cancel", on_click=dialog.close).props("flat")
                            ui.button(
                                "Delete",
                                icon="delete",
                                color="negative",
                                on_click=lambda: _delete_job(
                                    job.id,
                                    lambda: background_tasks.create(load_list()),
                                )
                                or dialog.close(),
                            ).mark("confirm-delete-button")

                    dialog.open()

                with (
                    ui.item(on_click=partial(ui.navigate.to, f"/cron/{job.id}"))
                    .props("v-ripple")
                    .mark("job-row")
                ):
                    with ui.item_section().props("avatar"):
                        ui.icon(icon, color=color)
                    with ui.item_section():
                        ui.item_label(job.name or "(unnamed)")
                        ui.item_label(job.schedule_display).props("caption lines=1")
                    with ui.item_section().props("side top"):
                        ui.label(fmt_iso_age(job.next_run_at)).classes("text-xs opacity-60")
                        ui.badge(job.deliver or "local", color="grey")
                        if job.no_agent:
                            ui.badge("script", color="secondary").props("outline").mark(
                                "script-badge"
                            ).tooltip(job.script or "script-only job")
                    with ui.item_section().props("side"):
                        # Pause/resume
                        ui.button(
                            icon="pause" if job.enabled else "play_arrow",
                            color="warning" if job.enabled else "positive",
                        ).props("flat dense size=sm").on(
                            "click.stop",
                            lambda: _pause_resume(
                                job,
                                lambda updated: background_tasks.create(load_list()),
                            ),
                        ).mark("pause-resume-button").tooltip(
                            "Pause" if job.enabled else "Resume"
                        )

                        # Edit
                        ui.button(
                            icon="edit",
                            color="info",
                        ).props("flat dense size=sm").on(
                            "click.stop",
                            lambda j=job: _edit_job_dialog(
                                j,
                                lambda updated: background_tasks.create(load_list()),
                            ),
                        ).mark("edit-job-button").tooltip(
                            f"Edit '{job.name or job.id}'"
                        )
                        # Delete
                        ui.button(
                            icon="delete",
                            color="negative",
                        ).props("flat dense size=sm").on(
                            "click.stop",
                            _confirm_delete,
                        ).mark("delete-job-button").tooltip(
                            f"Delete '{job.name or job.id}'"
                        )

            async def load_list() -> None:
                try:
                    jobs, total = await web.current_store().cron.list_jobs(
                        limit=pager.limit, offset=pager.offset
                    )
                except HermesError as exc:
                    ui.notify(f"Failed to load jobs: {exc}", type="negative")
                    return
                list_container.clear()
                with list_container:
                    if not jobs:
                        ui.label("No cron jobs yet.")
                    for job in jobs:
                        render_job_row(job)
                    render_pager(pager, total, lambda: background_tasks.create(load_list()))

            async def _refresh() -> None:
                pager.reset()
                await load_list()

            await load_list()
            logger.debug("cron list rendered")

    def _pause_resume(job: Job, on_updated: Any) -> None:
        async def do_toggle() -> None:
            store = web.current_store().cron
            try:
                updated = await (store.pause(job.id) if job.enabled else store.resume(job.id))
            except HermesError as exc:
                ui.notify(f"Failed: {exc}", type="negative")
                return
            ui.notify("Paused" if not updated.enabled else "Resumed", type="positive")
            on_updated(updated)

        background_tasks.create(do_toggle())

    def _run_now(job_id: str, on_updated: Any) -> None:
        async def do_run() -> None:
            try:
                updated = await web.current_store().cron.run(job_id)
            except HermesError as exc:
                ui.notify(f"Run failed: {exc}", type="negative")
                return
            ui.notify("Job triggered", type="positive")
            on_updated(updated)

        background_tasks.create(do_run())

    def _edit_job_dialog(job: Job, on_updated: Any) -> None:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-md"):
            ui.label("Edit cron job").classes("text-lg font-bold")
            name = ui.input("Name", value=job.name).props("outlined dense").classes("w-full").mark(
                "edit-job-name"
            )
            schedule = (
                ui.input("Schedule", value=job.schedule_display)
                .props("outlined dense")
                .classes("w-full")
                .mark("edit-job-schedule")
            )
            prompt = ui.textarea("Prompt", value=job.prompt or "").props("outlined dense").classes(
                "w-full"
            )
            if job.no_agent:
                prompt.props("disabled")
                ui.label("Prompt is ignored for script-only (no-agent) jobs.").props("caption")
            deliver = (
                ui.input("Deliver", value=job.deliver or "")
                .props("outlined dense")
                .classes("w-full")
            )
            skills = (
                ui.input("Skills", value=skills_to_text(job.skills))
                .props("outlined dense")
                .classes("w-full")
            )
            repeat = (
                ui.number(
                    "Repeat (blank = forever)",
                    value=job.repeat_times,
                    min=1,
                )
                .props("outlined dense")
                .classes("w-full")
            )

            async def do_save() -> None:
                fields: dict[str, Any] = {
                    "name": (name.value or "").strip(),
                    "schedule": (schedule.value or "").strip(),
                    "prompt": (prompt.value or "").strip(),
                    "deliver": (deliver.value or "").strip() or None,
                    "skills": text_to_skills(skills.value or ""),
                    "repeat": int(repeat.value) if repeat.value else None,
                }
                if not fields["name"] or not fields["schedule"]:
                    ui.notify("Name and schedule are required", type="warning")
                    return
                _save_fields(job.id, fields, lambda updated: (dialog.close(), on_updated(updated)))

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button(
                    "Save", on_click=lambda: background_tasks.create(do_save())
                ).mark("edit-job-confirm")
        dialog.open()

    def _delete_job(job_id: str, on_deleted: Any) -> None:
        async def do_delete() -> None:
            try:
                await web.current_store().cron.delete(job_id)
            except HermesError as exc:
                ui.notify(f"Delete failed: {exc}", type="negative")
                return
            ui.notify("Job deleted", type="positive")
            on_deleted()

        background_tasks.create(do_delete())

    def _save_fields(job_id: str, fields: dict[str, Any], on_updated: Any) -> None:
        async def do_save() -> None:
            try:
                updated = await web.current_store().cron.save_fields(job_id, fields)
            except HermesError as exc:
                ui.notify(f"Save failed: {exc}", type="negative")
                return
            ui.notify("Job saved", type="positive")
            on_updated(updated)

        background_tasks.create(do_save())

    @ui.page("/cron/{job_id}", title="Cron Job")
    async def cron_detail_page(job_id: str) -> None:
        with frame(active="/cron"):
            try:
                job = await web.current_store().cron.get_job(job_id)
                runs = await web.current_store().cron.list_runs(job_id)
            except HermesError as exc:
                ui.label(f"Failed to load job: {exc}")
                return

            def stats_text(j: Job) -> str:
                return (
                    f"Next run {fmt_iso(j.next_run_at)} · "
                    f"Last run {fmt_iso(j.last_run_at)} ({j.last_status or 'never'}) · "
                    f"{fmt_repeat(j.repeat_times, j.repeat_completed)}"
                )

            with ui.card().classes("w-full"):
                with ui.row().classes("items-center"):
                    title_label = ui.label(job.name or "(unnamed)").classes("text-lg font-bold")
                    ui.space()
                    state_badge = ui.badge(job.state or "unknown", color=state_icon(job.state)[1])
                    ui.badge(job.deliver or "local", color="grey")
                stats_label = ui.label(stats_text(job))
                if job.no_agent:
                    ui.label(f"Script (no-agent): {job.script or '—'}").mark("job-script-line")
                else:
                    ui.label("Mode: LLM agent (prompt-driven)").mark("job-agent-mode-line")
                with ui.row():
                    pause_button = ui.button(
                        "Pause" if job.enabled else "Resume",
                        icon="pause" if job.enabled else "play_arrow",
                    ).mark("pause-resume-button")
                    run_button = ui.button("Run now", icon="play_circle").mark("run-now-button")
                    delete_button = ui.button("Delete", icon="delete", color="negative").mark(
                        "delete-job-button"
                    )

            def apply_update(updated: Job) -> None:
                nonlocal job
                job = updated
                title_label.set_text(job.name or "(unnamed)")
                state_badge.set_text(job.state or "unknown")
                state_badge.set_background_color(state_icon(job.state)[1])
                stats_label.set_text(stats_text(job))
                pause_button.set_text("Pause" if job.enabled else "Resume")
                pause_button.set_icon("pause" if job.enabled else "play_arrow")
                yaml_editor.set_value(job_to_yaml(job.raw))

            pause_button.on_click(lambda: _pause_resume(job, apply_update))
            run_button.on_click(lambda: _run_now(job_id, apply_update))
            delete_button.on_click(lambda: _delete_job(job_id, lambda: ui.navigate.to("/cron")))

            with ui.tabs().classes("w-full") as tabs:
                details_tab = ui.tab("details", label="Details")
                yaml_tab = ui.tab("yaml", label="Raw YAML")
                runs_tab = ui.tab("runs", label="Runs").mark("runs-tab")

            with ui.tab_panels(tabs, value=details_tab).classes("w-full"):
                with ui.tab_panel(details_tab):
                    name_input = (
                        ui.input("Name", value=job.name)
                        .props("outlined dense")
                        .classes("w-full")
                        .mark("job-name-input")
                    )
                    schedule_input = (
                        ui.input("Schedule", value=job.schedule_display)
                        .props("outlined dense")
                        .classes("w-full")
                    )
                    prompt_input = (
                        ui.textarea("Prompt", value=job.prompt or "")
                        .props("outlined dense")
                        .classes("w-full")
                    )
                    if job.no_agent:
                        prompt_input.props("disabled")
                        ui.label("Prompt is ignored for script-only (no-agent) jobs.").props(
                            "caption"
                        )
                    deliver_input = (
                        ui.input("Deliver", value=job.deliver or "")
                        .props("outlined dense")
                        .classes("w-full")
                    )
                    skills_input = (
                        ui.input("Skills", value=skills_to_text(job.skills))
                        .props("outlined dense")
                        .classes("w-full")
                    )
                    repeat_input = (
                        ui.number("Repeat (blank = forever)", value=job.repeat_times, min=1)
                        .props("outlined dense")
                        .classes("w-full")
                    )

                    def save_details() -> None:
                        fields: dict[str, Any] = {
                            "name": name_input.value or "",
                            "schedule": schedule_input.value or "",
                            "prompt": prompt_input.value or "",
                            "deliver": deliver_input.value or "",
                            "skills": text_to_skills(skills_input.value or ""),
                            "repeat": int(repeat_input.value) if repeat_input.value else None,
                        }
                        _save_fields(job_id, fields, apply_update)

                    ui.button("Save", icon="save", on_click=save_details).mark(
                        "save-details-button"
                    )

                with ui.tab_panel(yaml_tab):
                    yaml_editor = ui.codemirror(
                        job_to_yaml(job.raw), language="YAML", theme="basicDark"
                    ).classes("w-full")

                    def save_yaml() -> None:
                        try:
                            fields = yaml_to_job_fields(yaml_editor.value)
                        except (yaml.YAMLError, ValueError) as exc:
                            ui.notify(f"Invalid YAML: {exc}", type="negative")
                            return
                        _save_fields(job_id, fields, apply_update)

                    ui.button("Save YAML", icon="save", on_click=save_yaml).mark("save-yaml-button")

                with ui.tab_panel(runs_tab):
                    with ui.row().classes("w-full items-center"):
                        ui.label("Recent runs").classes("text-lg")
                        ui.space()
                        ui.button(
                            icon="refresh",
                            on_click=lambda: background_tasks.create(load_runs()),
                        ).props("flat round dense").mark("runs-refresh-button").tooltip("Refresh")
                    runs_list = ui.list().props("separator").classes("w-full")

                    def render_run(run: CronRun) -> None:
                        icon, color = run_status_icon(run.status)
                        with (
                            ui.item(
                                on_click=partial(ui.navigate.to, f"/cron/{job_id}/runs/{run.id}")
                            )
                            .props("v-ripple")
                            .mark("run-row")
                        ):
                            with ui.item_section().props("avatar"):
                                ui.icon(icon, color=color)
                            with ui.item_section():
                                with ui.row().classes("items-center gap-2"):
                                    ui.badge(run.status, color=color)
                                    ui.label(
                                        f"{fmt_iso(run.claimed_at)} · {fmt_iso_age(run.claimed_at)}"
                                    ).classes("text-sm")
                                ui.item_label(fmt_duration(run.started_at, run.finished_at)).props(
                                    "caption"
                                )
                                if run.error:
                                    ui.item_label(run.error).props("caption lines=2")
                            with ui.item_section().props("side top"):
                                ui.badge(run.source, color="grey")
                            with ui.item_section().props("side"):
                                ui.icon("chevron_right", color="grey")

                    def render_runs(items: list[CronRun]) -> None:
                        runs_list.clear()
                        with runs_list:
                            if not items:
                                ui.label("No runs yet.").mark("runs-empty")
                            for run in items:
                                render_run(run)

                    async def load_runs() -> None:
                        try:
                            fresh_runs = await web.current_store().cron.list_runs(job_id)
                        except HermesError as exc:
                            ui.notify(f"Failed to load runs: {exc}", type="negative")
                            return
                        render_runs(fresh_runs)

                    render_runs(runs)

            logger.debug("cron detail rendered for {}", job_id)

    @ui.page("/cron/{job_id}/runs/{run_id}", title="Cron Run")
    async def cron_run_page(job_id: str, run_id: str) -> None:
        with frame(active="/cron"):
            try:
                job = await web.current_store().cron.get_job(job_id)
                run = await web.current_store().cron.get_run(job_id, run_id)
            except HermesError as exc:
                ui.label(f"Failed to load job: {exc}")
                return
            if run is None:
                ui.label("Run not found")
                return

            with ui.row().classes("w-full items-center gap-2"):
                ui.button(
                    icon="arrow_back",
                    on_click=partial(ui.navigate.to, f"/cron/{job_id}"),
                ).props("flat round dense")
                icon, color = run_status_icon(run.status)
                ui.icon(icon, color=color)
                ui.label(f"Run {run.id}").classes("text-lg font-bold")
                ui.badge(run.status, color=color)
                ui.space()
                ui.label(job.name or "(unnamed)").classes("text-sm opacity-70")
                ui.badge(run.source, color="grey")

            with ui.card().classes("w-full"):
                ui.label("Details").classes("text-lg font-bold")
                for label, value in (
                    ("Status", run.status),
                    ("Source", run.source),
                    ("PID", str(run.pid)),
                    ("Claimed", fmt_iso(run.claimed_at)),
                    ("Started", fmt_iso(run.started_at)),
                    ("Finished", fmt_iso(run.finished_at)),
                    ("Duration", fmt_duration(run.started_at, run.finished_at)),
                ):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(label).classes("w-24 opacity-70")
                        ui.label(value)
                if run.error:
                    with ui.row().classes("items-center gap-2"):
                        ui.label("Error").classes("w-24 opacity-70")
                        ui.label(run.error).classes("text-negative")

            output: tuple[str, str] | None = None
            session_id: str | None = None
            try:
                output = await web.current_store().cron.find_run_output(job_id, run)
                session_id = await web.current_store().cron.find_related_session_id(job_id, run)
            except HermesError as exc:
                ui.notify(f"Failed to load run details: {exc}", type="negative")

            with ui.card().classes("w-full"):
                ui.label("Output").classes("text-lg font-bold")
                if output is not None:
                    ui.markdown(output[1]).classes("w-full")
                else:
                    ui.label("No output file saved for this run.").classes("text-sm opacity-60")

            if session_id:
                ui.button(
                    "View session",
                    icon="chat",
                    on_click=partial(ui.navigate.to, f"/sessions/{session_id}"),
                ).mark("run-session-link")
            else:
                ui.label("No related session.").classes("text-sm opacity-60")

            logger.debug("cron run detail rendered for {}", run_id)
