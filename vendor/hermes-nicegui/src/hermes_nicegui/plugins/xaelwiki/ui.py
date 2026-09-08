"""Xaelwiki plugin pages: a searchable note list + a per-note detail view.

``/xaelwiki`` renders one vertical list of every note in the vault (newest-
updated first) with a real-time search box and a folder filter. ``/xaelwiki/
{note_id}`` shows the full note: metadata badges (folder/status), tags, and
the markdown body.

Mirrors the kanban plugin's page shape: closures per interaction, list
rendered into a container that is cleared + re-rendered on every filter
change, and a ``ui.timer`` that re-scans the vault on the configured refresh
interval (the store itself is TTL-cached, so the timer is cheap).
"""

from __future__ import annotations

from functools import partial
from typing import Any

from nicegui import background_tasks, ui

from hermes_nicegui.plugin import Plugin
from hermes_nicegui.web import frame

from .logic import Note, NoteStore

STATUS_COLORS = {
    "inbox": "blue",
    "active": "green",
    "evergreen": "teal",
    "archived": "grey",
}


def _status_color(status: str) -> str:
    return STATUS_COLORS.get((status or "").lower(), "grey")


def register_pages(plugin: Plugin) -> None:
    store: NoteStore = plugin.store  # type: ignore[attr-defined]
    logger = plugin.logger

    @ui.page("/xaelwiki", title="Notes")
    async def notes_page() -> None:
        with frame(active="/xaelwiki"):
            search_input = (
                ui.input("Search notes…")
                .props("outlined dense clearable")
                .classes("w-full md:w-96")
                .mark("notes-search-input")
            )
            with ui.row().classes("w-full items-center gap-2"):
                ui.label("Notes").classes("text-lg")
                ui.space()
                folder_select = (
                    ui.select([], value=None, label="Folder")
                    .props("outlined dense clearable")
                    .classes("w-44")
                    .mark("notes-folder-select")
                )
                folder_select.set_options(store.folders())
                ui.button(
                    icon="refresh", on_click=lambda: background_tasks.create(_refresh())
                ).props("flat round dense").mark("notes-refresh-button").tooltip("Refresh")

            list_container = ui.list().props("separator").classes("w-full")
            error_label = ui.label("").classes("text-negative text-sm")
            empty_label = ui.label("").classes("text-sm opacity-60 q-pa-sm")

            def current_query() -> str:
                return (search_input.value or "").strip()

            def current_folder() -> str | None:
                return folder_select.value or None

            def render_list() -> None:
                list_container.clear()
                query = current_query()
                folder = current_folder()
                notes = store.search(query, folder)
                error_label.set_text(store.last_error or "")
                with list_container:
                    if not notes:
                        empty_label.set_text(
                            "No notes." if not query else "No notes match your search."
                        )
                    else:
                        empty_label.set_text("")
                    if folder is not None:
                        for note in notes:
                            render_note_row(note)
                    else:
                        notes_by_folder: dict[str, list[Note]] = {}
                        for note in notes:
                            notes_by_folder.setdefault(note.folder, []).append(note)
                        for folder_name in store.folders():
                            folder_notes = notes_by_folder.get(folder_name, [])
                            if not folder_notes:
                                continue
                            ui.label(f"{folder_name} ({len(folder_notes)})").mark(
                                "notes-folder-header"
                            )
                            for note in folder_notes:
                                render_note_row(note)
                        root_notes = notes_by_folder.get("", [])
                        if root_notes:
                            ui.label(f"root ({len(root_notes)})").mark("notes-folder-header")
                            for note in root_notes:
                                render_note_row(note)
                logger.debug(
                    "xaelwiki list rendered: {} notes (query={!r}, folder={!r})",
                    len(notes),
                    query,
                    folder,
                )

            def render_note_row(note: Note) -> None:
                with (
                    ui.item(on_click=partial(ui.navigate.to, f"/xaelwiki/{note.id}"))
                    .props("v-ripple")
                    .mark("notes-card")
                ):
                    with ui.item_section().props("avatar"):
                        ui.icon("description", color="primary")
                    with ui.item_section():
                        ui.item_label(note.title)
                        caption = note.folder
                        if note.status:
                            caption = f"{caption} · {note.status}" if caption else note.status
                        if note.updated:
                            caption = f"{caption} · {note.updated}" if caption else note.updated
                        ui.item_label(caption).props("caption lines=1")
                        if note.tags:
                            with ui.row().classes("gap-1"):
                                for tag in note.tags[:4]:
                                    ui.badge(tag, color="grey-8").props("outline")

            def _on_filter_change(_e: Any = None) -> None:
                render_list()

            search_input.on_value_change(_on_filter_change)
            folder_select.on_value_change(_on_filter_change)

            async def _refresh() -> None:
                store.refresh()
                folder_select.set_options(store.folders())
                render_list()

            # Re-scan the vault on the configured interval. The store's TTL
            # cache makes this a no-op until the TTL expires, so the timer is
            # cheap even on a busy page.
            ui.timer(store.refresh_seconds, lambda: render_list())

            render_list()

    @ui.page("/xaelwiki/{note_id}", title="Note")
    async def note_detail_page(note_id: str) -> None:
        with frame(active="/xaelwiki"):
            note = store.get(note_id)
            if note is None:
                ui.label("Note not found.").classes("text-negative")
                ui.link("← Back to notes", "/xaelwiki")
                return

            with ui.row().classes("w-full items-center gap-2"):
                ui.link("← Notes", "/xaelwiki").classes("text-sm")
                ui.space()
                if note.status:
                    ui.badge(note.status, color=_status_color(note.status))
                ui.badge(note.folder or "root", color="grey")

            ui.label(note.title).classes("text-xl font-bold")
            if note.tags:
                with ui.row().classes("gap-1"):
                    for tag in note.tags:
                        ui.badge(tag, color="grey-8").props("outline")
            meta = " · ".join(
                part
                for part in [
                    f"created {note.created}" if note.created else "",
                    f"updated {note.updated}" if note.updated else "",
                    note.rel_path,
                ]
                if part
            )
            ui.label(meta).classes("text-xs opacity-60")

            with ui.separator().classes("my-2") as _sep:
                pass

            body = note.body.strip()
            if body:
                ui.markdown(body).classes("w-full")
            else:
                ui.label("(empty note)").classes("text-sm opacity-60")

            logger.debug("xaelwiki note rendered: {}", note.id)
