"""Tests for the files plugin's page and raw HTTP routes.

Every test scopes ``files_root`` to ``tmp_path`` via ``make_context`` --
never the real cwd -- since this plugin gives the browser real filesystem
access under that root.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest
import zstandard
from nicegui import ui
from nicegui.testing import User

from hermes_nicegui import web
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.files import FilesPlugin


async def test_files_nav_item_present(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    await user.open("/")
    await user.should_see("Files")


async def test_lists_directory_contents(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    (tmp_path / "notes.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    await user.open("/files")
    await user.should_see("notes.txt")
    await user.should_see("sub")
    assert user.find(marker="dir-row").elements
    assert user.find(marker="file-row").elements


async def test_empty_directory_shows_placeholder(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    await user.open("/files")
    await user.should_see("Empty directory.")


async def test_views_text_file_in_editor(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    (tmp_path / "script.py").write_text("print('hi')")
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    await user.open("/files/script.py")
    await user.should_see("script.py")
    assert user.find(marker="file-editor").elements
    assert user.find(marker="save-file-button").elements


async def test_views_image_file_with_ui_image(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    (tmp_path / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    await user.open("/files/photo.png")
    assert user.find(kind=ui.image).elements


async def test_binary_file_falls_back_to_download_only(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    await user.open("/files/blob.bin")
    await user.should_see("No preview available for this file type.")


async def test_missing_path_shows_not_found(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    await user.open("/files/does-not-exist.txt")
    await user.should_see("Not found")


# -- raw HTTP routes ---------------------------------------------------


async def test_raw_route_serves_file_bytes(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    (tmp_path / "data.txt").write_text("payload")
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    response = await user.http_client.get("/files/raw/data.txt")
    assert response.status_code == 200
    assert response.text == "payload"


async def test_raw_route_404s_for_missing_file(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    response = await user.http_client.get("/files/raw/nope.txt")
    assert response.status_code == 404


async def test_archive_route_streams_tar_zst_of_root(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("world")
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    response = await user.http_client.get("/files/archive/")
    assert response.status_code == 200

    decompressor = zstandard.ZstdDecompressor()
    with decompressor.stream_reader(io.BytesIO(response.content)) as reader:
        tar_bytes = reader.read()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        names = {m.name for m in tf.getmembers() if m.isfile()}
    assert any(n.endswith("a.txt") for n in names)
    assert any(n.endswith("sub/b.txt") for n in names)


async def test_upload_tree_preserves_relative_paths(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    response = await user.http_client.post(
        "/files/upload-tree/",
        files=[("file", ("project/src/main.py", b"print(1)", "text/plain"))],
    )
    assert response.status_code == 200
    assert response.json()["saved"] == 1
    assert (tmp_path / "project" / "src" / "main.py").read_text() == "print(1)"


async def test_upload_tree_rejects_escaping_paths(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    response = await user.http_client.post(
        "/files/upload-tree/",
        files=[("file", ("../escape.txt", b"nope", "text/plain"))],
    )
    assert response.status_code == 200
    assert response.json()["saved"] == 0
    assert not (tmp_path.parent / "escape.txt").exists()


# -- delete / rename / copy --------------------------------------------


def _set_value(user: User, marker: str, value: str) -> None:
    for element in user.find(marker=marker).elements:
        element.value = value


async def test_delete_row_removes_file(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    (tmp_path / "doomed.txt").write_text("bye")
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    await user.open("/files")
    await user.should_see("doomed.txt")

    # The row-level action buttons register `click.stop` (Vue's `.stop`
    # modifier, needed so a real click doesn't *also* trigger the row's own
    # navigate-on-click) -- `UserInteraction.click()` only dispatches bare
    # `'click'` listeners, so `.trigger()` with the exact modifier-suffixed
    # type is what's needed to simulate it here.
    user.find(marker="delete-button").trigger("click.stop")
    await user.should_see("Delete", retries=10)
    user.find(marker="confirm-delete-button").click()

    await user.should_see("Empty directory.", retries=10)
    assert not (tmp_path / "doomed.txt").exists()


async def test_delete_row_removes_directory_recursively(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.txt").write_text("inner")
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    await user.open("/files")
    await user.should_see("sub")

    user.find(marker="delete-button").trigger("click.stop")
    await user.should_see("Delete", retries=10)
    user.find(marker="confirm-delete-button").click()

    await user.should_see("Empty directory.", retries=10)
    assert not (tmp_path / "sub").exists()


async def test_rename_row_moves_file(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    (tmp_path / "old.txt").write_text("content")
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    await user.open("/files")
    await user.should_see("old.txt")

    user.find(marker="rename-button").trigger("click.stop")
    await user.should_see("Rename / Move", retries=10)
    _set_value(user, "move-dest-input", "new.txt")
    user.find(marker="confirm-move-button").click()

    # The dialog's own input still shows "new.txt" until `do_submit` (a
    # background task) actually finishes and closes it, so waiting on that
    # text would pass before the move ran. Waiting for the dialog to close
    # is the real completion signal.
    await user.should_not_see(marker="move-dest-input", retries=20)
    assert not (tmp_path / "old.txt").exists()
    assert (tmp_path / "new.txt").read_text() == "content"


async def test_copy_row_duplicates_file(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    (tmp_path / "original.txt").write_text("content")
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    await user.open("/files")
    await user.should_see("original.txt")

    user.find(marker="copy-button").trigger("click.stop")
    await user.should_see("Copy", retries=10)
    _set_value(user, "copy-dest-input", "duplicate.txt")
    user.find(marker="confirm-copy-button").click()

    await user.should_not_see(marker="copy-dest-input", retries=20)
    assert (tmp_path / "original.txt").read_text() == "content"
    assert (tmp_path / "duplicate.txt").read_text() == "content"


async def test_move_rejects_existing_destination(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    (tmp_path / "old.txt").write_text("content")
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    await user.open("/files")
    await user.should_see("old.txt")

    # Only "old.txt" exists yet, so there's exactly one "rename-button" to
    # trigger here -- `UserInteraction.trigger()` fires every element a
    # marker matches, and a second row's button would make this ambiguous.
    # "taken.txt" is created *after* the dialog opens (validation reads live
    # disk state at submit time, not dialog-open time) to keep it that way.
    user.find(marker="rename-button").trigger("click.stop")
    await user.should_see("Rename / Move", retries=10)
    (tmp_path / "taken.txt").write_text("already here")
    _set_value(user, "move-dest-input", "taken.txt")
    user.find(marker="confirm-move-button").click()

    assert (tmp_path / "old.txt").read_text() == "content"
    assert (tmp_path / "taken.txt").read_text() == "already here"


async def test_directory_self_actions_hidden_at_root(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    await user.open("/files")
    with pytest.raises(AssertionError):
        user.find(marker="delete-self-button")


async def test_directory_self_actions_present_in_subdirectory(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    (tmp_path / "sub").mkdir()
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    await user.open("/files/sub")
    assert user.find(marker="rename-self-button").elements
    assert user.find(marker="copy-self-button").elements
    assert user.find(marker="delete-self-button").elements


async def test_file_view_has_rename_copy_delete_buttons(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    (tmp_path / "doc.txt").write_text("hi")
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    await user.open("/files/doc.txt")
    assert user.find(marker="rename-button").elements
    assert user.find(marker="copy-button").elements
    assert user.find(marker="delete-button").elements


async def test_delete_current_file_navigates_to_parent(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    (tmp_path / "doc.txt").write_text("hi")
    context = make_context(files_root=str(tmp_path))
    web.build(context, [FilesPlugin(context)])
    await user.open("/files/doc.txt")

    user.find(marker="delete-button").click()
    await user.should_see("Delete", retries=10)
    user.find(marker="confirm-delete-button").click()

    await user.should_see("Empty directory.", retries=10)
    assert not (tmp_path / "doc.txt").exists()
