"""Files plugin: browse, view/edit, download, and upload local files.

Unlike the sessions/cron plugins, there's no Hermes gateway involved here --
everything operates directly on the local filesystem under
``settings.files_root_path`` via ``pathlib``/``run.io_bound`` from the page
and route handlers themselves. Three raw (non-``ui.page``) routes are
registered once, alongside the browse/view page, for things a plain page
can't do: streaming a raw file (``/files/raw``), streaming a directory as a
``.tar.zst`` archive (``/files/archive``), and accepting a folder upload
with its relative paths intact (``/files/upload-tree`` -- see the module
docstring in ``logic.py`` for why ``ui.upload`` alone can't do that).
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import cast
from urllib.parse import quote

from fastapi import HTTPException, Request
from nicegui import app, background_tasks, run, ui
from nicegui.elements.codemirror.constants import SUPPORTED_LANGUAGES, SUPPORTED_THEMES
from starlette.datastructures import UploadFile
from starlette.responses import FileResponse, StreamingResponse

from hermes_nicegui.plugin import Plugin
from hermes_nicegui.plugins.files.logic import (
    DestinationExists,
    PathEscapesRoot,
    classify,
    fmt_mtime,
    guess_language,
    human_size,
    iter_tar_zst,
    kind_icon,
    parent_rel,
    resolve_under_root,
    sanitize_relative_path,
    validate_new_path,
)
from hermes_nicegui.web import frame


def _read_sample(path: Path, size: int = 4096) -> bytes:
    with path.open("rb") as f:
        return f.read(size)


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _list_dir(path: Path) -> list[Path]:
    return sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))


def _delete_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _move_path(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))


def _copy_path(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, dest)
    else:
        shutil.copy2(source, dest)


def register_pages(plugin: Plugin) -> None:
    logger = plugin.logger
    root = plugin.settings.files_root_path
    max_edit_bytes = plugin.settings.files_edit_max_bytes
    editor_theme: SUPPORTED_THEMES = "vscodeDark" if plugin.settings.ui_dark else "vscodeLight"
    root.mkdir(parents=True, exist_ok=True)

    def _resolve_or_404(path: str) -> Path:
        try:
            return resolve_under_root(root, path)
        except PathEscapesRoot as exc:
            raise HTTPException(404, "Not found") from exc

    def _url(kind: str, rel: str) -> str:
        encoded = quote(rel, safe="/")
        return f"/files/{kind}/{encoded}"

    # -- raw HTTP routes (registered once, independent of any page load) ---

    @app.get("/files/raw/{path:path}")
    async def raw_file(path: str, download: bool = False) -> FileResponse:
        target = _resolve_or_404(path)
        if not target.is_file():
            raise HTTPException(404, "Not found")
        return FileResponse(
            target,
            filename=target.name,
            content_disposition_type="attachment" if download else "inline",
        )

    @app.get("/files/archive/{path:path}")
    async def archive(path: str) -> StreamingResponse:
        target = _resolve_or_404(path)
        if not target.exists():
            raise HTTPException(404, "Not found")
        return StreamingResponse(
            iter_tar_zst(target),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{target.name}.tar.zst"'},
        )

    @app.post("/files/upload-tree/{path:path}")
    async def upload_tree(path: str, request: Request) -> dict:
        target_dir = _resolve_or_404(path)
        if not target_dir.is_dir():
            raise HTTPException(404, "Not found")
        saved = 0
        async with request.form() as form:
            for value in form.values():
                if not isinstance(value, UploadFile):
                    continue
                rel = sanitize_relative_path(value.filename or "")
                if rel is None:
                    continue
                try:
                    dest = resolve_under_root(target_dir, str(rel))
                except PathEscapesRoot:
                    continue
                data = await value.read()
                await run.io_bound(_write_bytes, dest, data)
                saved += 1
        return {"saved": saved}

    # -- delete / move / copy dialogs ---------------------------------------

    def _confirm_delete_dialog(rel: str, *, is_dir: bool, on_deleted: Callable[[], None]) -> None:
        noun = "directory and everything inside it" if is_dir else "file"
        with ui.dialog() as dialog, ui.card():
            ui.label(f"Delete {rel!r}?").classes("text-lg font-bold")
            ui.label(f"This permanently deletes the {noun}. This can't be undone.").classes(
                "opacity-70"
            )

            async def do_delete() -> None:
                target = _resolve_or_404(rel)
                try:
                    await run.io_bound(_delete_path, target)
                except OSError as exc:
                    ui.notify(f"Delete failed: {exc}", type="negative")
                    return
                dialog.close()
                ui.notify("Deleted", type="positive")
                on_deleted()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button(
                    "Delete",
                    color="negative",
                    on_click=lambda: background_tasks.create(do_delete()),
                ).mark("confirm-delete-button")
        dialog.open()

    def _move_or_copy_dialog(
        rel: str, *, copy: bool, on_done: Callable[[str], None]
    ) -> None:
        verb = "Copy" if copy else "Rename / Move"
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-md"):
            ui.label(f"{verb} {rel!r}").classes("text-lg font-bold")
            dest_input = (
                ui.input("New path", value=rel)
                .props("outlined dense")
                .classes("w-full")
                .mark("copy-dest-input" if copy else "move-dest-input")
            )

            async def do_submit() -> None:
                source = _resolve_or_404(rel)
                new_rel = (dest_input.value or "").strip()
                try:
                    dest = validate_new_path(root, source, new_rel)
                except DestinationExists:
                    ui.notify("A file or folder already exists there", type="warning")
                    return
                except (PathEscapesRoot, ValueError) as exc:
                    ui.notify(f"Invalid destination: {exc}", type="warning")
                    return
                try:
                    if copy:
                        await run.io_bound(_copy_path, source, dest)
                    else:
                        await run.io_bound(_move_path, source, dest)
                except OSError as exc:
                    ui.notify(f"{verb} failed: {exc}", type="negative")
                    return
                dialog.close()
                ui.notify("Copied" if copy else "Moved", type="positive")
                on_done(new_rel)

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button(
                    verb, on_click=lambda: background_tasks.create(do_submit())
                ).mark("confirm-copy-button" if copy else "confirm-move-button")
        dialog.open()

    # -- browse / view page --------------------------------------------

    def _breadcrumbs(rel: str) -> None:
        with ui.row().classes("items-center gap-1"):
            ui.icon("folder_open", color="primary")
            ui.link("root", "/files").classes("no-underline text-base font-medium")
            acc: list[str] = []
            for part in Path(rel).parts:
                acc.append(part)
                ui.label("/").classes("opacity-50")
                ui.link(part, "/files/" + quote("/".join(acc), safe="/")).classes(
                    "no-underline text-base font-medium"
                )

    def _make_multi_uploader(rel: str, on_done) -> None:
        async def handle(e) -> None:
            target_dir = _resolve_or_404(rel)
            for f in e.files:
                await f.save(target_dir / f.name)
            ui.notify(f"Uploaded {len(e.files)} file(s)", type="positive")
            on_done()

        ui.upload(
            multiple=True, auto_upload=True, on_multi_upload=handle, label="Upload files"
        ).props("flat").classes("max-w-xs").mark("upload-files")

    def _make_folder_uploader(rel: str) -> None:
        """A folder-picker button backed by a native ``webkitdirectory``
        input, wired by hand to ``/files/upload-tree`` -- ``ui.upload``
        strips path components from every filename it receives, which would
        flatten the folder into a single directory, so it can't be used
        for this. Each selected file's ``webkitRelativePath`` (which browsers
        allow to contain ``/``, being just a filename string, not a real
        path) is what carries the folder structure through to the server.
        """
        button = ui.button("Upload folder", icon="drive_folder_upload").props("outline").mark(
            "upload-folder-button"
        )
        folder_input = (
            ui.element("input")
            .props("type=file webkitdirectory multiple")
            .style("display:none")
            .mark("upload-folder-input")
        )
        button.on("click", js_handler=f"() => getHtmlElement({folder_input.id}).click()")
        upload_url = json.dumps(_url("upload-tree", rel))
        ui.run_javascript(f"""
            (() => {{
                const input = getHtmlElement({folder_input.id});
                if (!input || input._wired) return;
                input._wired = true;
                input.addEventListener('change', async () => {{
                    const files = Array.from(input.files || []);
                    if (!files.length) return;
                    const formData = new FormData();
                    for (const f of files) {{
                        formData.append('file', f, f.webkitRelativePath || f.name);
                    }}
                    await fetch((window.path_prefix || '') + {upload_url}, {{
                        method: 'POST',
                        body: formData,
                    }});
                    location.reload();
                }});
            }})();
        """)

    async def _render_directory(current: Path, rel: str) -> None:
        with ui.card().classes("w-full"):
            _breadcrumbs(rel)
            with ui.row().classes("w-full items-center gap-2"):
                _make_multi_uploader(
                    rel, lambda: ui.navigate.to(f"/files/{quote(rel, safe='/')}")
                )
                _make_folder_uploader(rel)
                ui.space()
                ui.button(
                    "Download .tar.zst",
                    icon="archive",
                    on_click=lambda: ui.navigate.to(_url("archive", rel), new_tab=True),
                ).props("outline").mark("archive-button")
                if rel:
                    ui.button(
                        icon="edit",
                        on_click=lambda: _move_or_copy_dialog(
                            rel,
                            copy=False,
                            on_done=lambda new_rel: ui.navigate.to(
                                "/files/" + quote(new_rel, safe="/")
                            ),
                        ),
                    ).props("outline round dense").mark("rename-self-button").tooltip(
                        "Rename / move this folder"
                    )
                    ui.button(
                        icon="content_copy",
                        on_click=lambda: _move_or_copy_dialog(
                            rel,
                            copy=True,
                            on_done=lambda _new_rel: ui.navigate.to(
                                "/files/" + quote(rel, safe="/")
                            ),
                        ),
                    ).props("outline round dense").mark("copy-self-button").tooltip(
                        "Copy this folder"
                    )
                    ui.button(
                        icon="delete",
                        color="negative",
                        on_click=lambda: _confirm_delete_dialog(
                            rel,
                            is_dir=True,
                            on_deleted=lambda: ui.navigate.to(
                                "/files/" + quote(parent_rel(rel), safe="/")
                            ),
                        ),
                    ).props("outline round dense").mark("delete-self-button").tooltip(
                        "Delete this folder"
                    )

        entries = await run.io_bound(_list_dir, current) or []
        refresh = partial(ui.navigate.to, "/files/" + quote(rel, safe="/"))
        with ui.list().props("separator").classes("w-full"):
            if not entries:
                ui.label("Empty directory.").classes("opacity-70 q-pa-md")
            for entry in entries:
                entry_rel = f"{rel}/{entry.name}" if rel else entry.name
                entry_url = "/files/" + quote(entry_rel, safe="/")
                is_dir = entry.is_dir()
                try:
                    stat = entry.stat()
                except OSError:
                    stat = None
                icon, color = kind_icon(entry.name, is_dir=is_dir)
                with (
                    ui.item(on_click=partial(ui.navigate.to, entry_url))
                    .props("clickable v-ripple")
                    .mark("dir-row" if is_dir else "file-row")
                ):
                    with ui.item_section().props("avatar"):
                        ui.icon(icon, color=color)
                    with ui.item_section():
                        ui.item_label(entry.name)
                        if stat is not None:
                            ui.item_label(f"Modified {fmt_mtime(stat.st_mtime)}").props(
                                "caption"
                            )
                    with ui.item_section().props("side"):
                        if not is_dir and stat is not None:
                            ui.label(human_size(stat.st_size)).classes("text-xs opacity-60")
                        # `click.stop` (Vue's `.stop` modifier) keeps these
                        # from also triggering the row's own `on_click`
                        # navigation above -- see `Element.on`'s docstring
                        # and `nicegui.js`'s `Vue.withModifiers` call.
                        with ui.row().classes("gap-0 items-center"):
                            ui.button(icon="edit").props("flat round dense").on(
                                "click.stop",
                                partial(
                                    _move_or_copy_dialog,
                                    entry_rel,
                                    copy=False,
                                    on_done=lambda _new: refresh(),
                                ),
                            ).mark("rename-button").tooltip("Rename / move")
                            ui.button(icon="content_copy").props("flat round dense").on(
                                "click.stop",
                                partial(
                                    _move_or_copy_dialog,
                                    entry_rel,
                                    copy=True,
                                    on_done=lambda _new: refresh(),
                                ),
                            ).mark("copy-button").tooltip("Copy")
                            ui.button(icon="delete", color="negative").props(
                                "flat round dense"
                            ).on(
                                "click.stop",
                                partial(
                                    _confirm_delete_dialog,
                                    entry_rel,
                                    is_dir=is_dir,
                                    on_deleted=refresh,
                                ),
                            ).mark("delete-button").tooltip("Delete")

    async def _render_file(current: Path, rel: str) -> None:
        try:
            stat = current.stat()
        except OSError:
            stat = None
        size = stat.st_size if stat else 0
        sample = (await run.io_bound(_read_sample, current) if size else b"") or b""
        kind = classify(current.name, sniff=sample)
        icon, color = kind_icon(current.name, is_dir=False, sniff=sample)

        with ui.card().classes("w-full"):
            _breadcrumbs(rel)
            with ui.row().classes("w-full items-center gap-2"):
                ui.icon(icon, color=color, size="md")
                ui.label(current.name).classes("text-lg font-mono")
                ui.badge(human_size(size), color="grey")
                if stat is not None:
                    ui.label(f"Modified {fmt_mtime(stat.st_mtime)}").classes(
                        "text-xs opacity-60"
                    )
                ui.space()
                ui.button(
                    "Download",
                    icon="download",
                    on_click=lambda: ui.navigate.to(_url("raw", rel) + "?download=1", new_tab=True),
                ).props("outline").mark("download-button")
                ui.button(
                    icon="edit",
                    on_click=lambda: _move_or_copy_dialog(
                        rel,
                        copy=False,
                        on_done=lambda new_rel: ui.navigate.to(
                            "/files/" + quote(new_rel, safe="/")
                        ),
                    ),
                ).props("outline round dense").mark("rename-button").tooltip("Rename / move")
                ui.button(
                    icon="content_copy",
                    on_click=lambda: _move_or_copy_dialog(rel, copy=True, on_done=lambda _n: None),
                ).props("outline round dense").mark("copy-button").tooltip("Copy")
                ui.button(
                    icon="delete",
                    color="negative",
                    on_click=lambda: _confirm_delete_dialog(
                        rel,
                        is_dir=False,
                        on_deleted=lambda: ui.navigate.to(
                            "/files/" + quote(parent_rel(rel), safe="/")
                        ),
                    ),
                ).props("outline round dense").mark("delete-button").tooltip("Delete")

        raw_url = _url("raw", rel)

        if kind == "image":
            ui.image(raw_url).classes("max-w-full")
        elif kind == "pdf":
            ui.html(
                f'<iframe src="{raw_url}" class="w-full" style="height:80vh;border:0"></iframe>'
            )
        elif kind == "audio":
            ui.audio(raw_url).classes("w-full")
        elif kind == "video":
            ui.video(raw_url).classes("w-full")
        elif kind == "text" and size <= max_edit_bytes:
            read = await run.io_bound(current.read_text, encoding="utf-8", errors="replace")
            content = read or ""
            language = cast("SUPPORTED_LANGUAGES | None", guess_language(current.name))
            editor = (
                ui.codemirror(content, language=language, theme=editor_theme)
                .classes("w-full h-[70vh]")
                .mark("file-editor")
            )

            async def save() -> None:
                await run.io_bound(current.write_text, editor.value, encoding="utf-8")
                ui.notify("Saved", type="positive")

            ui.button(
                "Save", icon="save", on_click=lambda: background_tasks.create(save())
            ).mark("save-file-button")
        elif kind == "text":
            ui.label(
                f"File is too large to edit inline (> {human_size(max_edit_bytes)}). "
                "Download to view."
            ).classes("opacity-70")
        else:
            ui.label("No preview available for this file type.").classes("opacity-70")

    async def _render_page(rel: str) -> None:
        with frame(active="/files"):
            try:
                current = resolve_under_root(root, rel)
            except PathEscapesRoot:
                ui.label("Not found").classes("text-lg")
                return
            if not current.exists():
                ui.label("Not found").classes("text-lg")
                return
            if current.is_dir():
                await _render_directory(current, rel)
            else:
                await _render_file(current, rel)
            logger.debug("files page rendered for {!r}", rel)

    @ui.page("/files", title="Files")
    async def files_root_page() -> None:
        await _render_page("")

    @ui.page("/files/{path:path}", title="Files")
    async def files_page(path: str) -> None:
        await _render_page(path)
