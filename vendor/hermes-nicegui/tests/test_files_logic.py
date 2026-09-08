"""Tests for the files plugin's pure helpers: sandboxing, classification,
and the streaming tar.zst archiver."""

from __future__ import annotations

import io
import tarfile
from datetime import datetime
from pathlib import Path

import pytest
import zstandard

from hermes_nicegui.plugins.files.logic import (
    DestinationExists,
    PathEscapesRoot,
    classify,
    fmt_mtime,
    guess_language,
    human_size,
    is_probably_text,
    iter_tar_zst,
    kind_icon,
    parent_rel,
    resolve_under_root,
    sanitize_relative_path,
    validate_new_path,
)

# -- resolve_under_root ------------------------------------------------


def test_resolve_under_root_accepts_nested_path(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    assert resolve_under_root(tmp_path, "sub") == tmp_path / "sub"


def test_resolve_under_root_accepts_empty_path(tmp_path: Path) -> None:
    assert resolve_under_root(tmp_path, "") == tmp_path


def test_resolve_under_root_rejects_dotdot_escape(tmp_path: Path) -> None:
    with pytest.raises(PathEscapesRoot):
        resolve_under_root(tmp_path, "../etc/passwd")


def test_resolve_under_root_rejects_dotdot_mid_path(tmp_path: Path) -> None:
    with pytest.raises(PathEscapesRoot):
        resolve_under_root(tmp_path, "sub/../../etc")


def test_resolve_under_root_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-target"
    outside.mkdir(exist_ok=True)
    root = tmp_path / "root"
    root.mkdir()
    (root / "escape").symlink_to(outside)
    with pytest.raises(PathEscapesRoot):
        resolve_under_root(root, "escape")


# -- validate_new_path ------------------------------------------------------


def test_validate_new_path_accepts_free_destination(tmp_path: Path) -> None:
    source = tmp_path / "a.txt"
    source.write_text("hi")
    assert validate_new_path(tmp_path, source, "b.txt") == tmp_path / "b.txt"


def test_validate_new_path_rejects_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "a.txt"
    source.write_text("hi")
    (tmp_path / "b.txt").write_text("already here")
    with pytest.raises(DestinationExists):
        validate_new_path(tmp_path, source, "b.txt")


def test_validate_new_path_rejects_same_source_and_destination(tmp_path: Path) -> None:
    source = tmp_path / "a.txt"
    source.write_text("hi")
    with pytest.raises(ValueError, match="same"):
        validate_new_path(tmp_path, source, "a.txt")


def test_validate_new_path_rejects_escaping_destination(tmp_path: Path) -> None:
    source = tmp_path / "a.txt"
    source.write_text("hi")
    with pytest.raises(PathEscapesRoot):
        validate_new_path(tmp_path, source, "../escape.txt")


# -- sanitize_relative_path ----------------------------------------------


def test_sanitize_relative_path_preserves_folder_structure() -> None:
    assert sanitize_relative_path("project/src/main.py") == Path("project/src/main.py")


def test_sanitize_relative_path_rejects_dotdot() -> None:
    assert sanitize_relative_path("../etc/passwd") is None


def test_sanitize_relative_path_rejects_empty() -> None:
    assert sanitize_relative_path("") is None


def test_sanitize_relative_path_normalizes_backslashes() -> None:
    assert sanitize_relative_path("project\\src\\main.py") == Path("project/src/main.py")


# -- human_size ------------------------------------------------------------


def test_human_size_bytes() -> None:
    assert human_size(500) == "500 B"


def test_human_size_megabytes() -> None:
    assert human_size(5 * 1024 * 1024) == "5.0 MB"


# -- classify / guess_language / is_probably_text --------------------------


def test_classify_image_by_extension() -> None:
    assert classify("photo.png") == "image"


def test_classify_pdf_by_extension() -> None:
    assert classify("doc.pdf") == "pdf"


def test_classify_text_by_known_code_extension() -> None:
    assert classify("script.py") == "text"


def test_classify_text_by_sniffing_unknown_extension() -> None:
    assert classify("README", sniff=b"just plain text") == "text"


def test_classify_binary_by_sniffing_unknown_extension() -> None:
    assert classify("blob.dat", sniff=b"\x00\x01\x02binary") == "binary"


def test_guess_language_known_extension() -> None:
    assert guess_language("main.rs") == "Rust"


def test_guess_language_dockerfile() -> None:
    assert guess_language("Dockerfile") == "Dockerfile"


def test_guess_language_unknown_extension() -> None:
    assert guess_language("file.xyz123") is None


def test_is_probably_text_rejects_null_bytes() -> None:
    assert is_probably_text(b"abc\x00def") is False


def test_is_probably_text_accepts_utf8() -> None:
    assert is_probably_text("héllo".encode()) is True


# -- parent_rel --------------------------------------------------------


def test_parent_rel_nested_path() -> None:
    assert parent_rel("a/b/c.txt") == "a/b"


def test_parent_rel_top_level_path() -> None:
    assert parent_rel("c.txt") == ""


def test_parent_rel_empty_path() -> None:
    assert parent_rel("") == ""


# -- kind_icon / fmt_mtime --------------------------------------------------


def test_kind_icon_directory_uses_folder_icon() -> None:
    icon, _color = kind_icon("anything", is_dir=True)
    assert icon == "folder"


def test_kind_icon_matches_classify_kind() -> None:
    icon, color = kind_icon("photo.png", is_dir=False)
    assert (icon, color) == ("image", "teal")


def test_fmt_mtime_recent() -> None:
    assert fmt_mtime(datetime.now().timestamp()) == "now"


def test_fmt_mtime_hours_ago() -> None:
    assert fmt_mtime(datetime.now().timestamp() - 3 * 3600) == "3h"


# -- iter_tar_zst ------------------------------------------------------


def test_iter_tar_zst_roundtrips_directory_contents(tmp_path: Path) -> None:
    src = tmp_path / "project"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    sub = src / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("world")

    archive = b"".join(iter_tar_zst(src))

    decompressor = zstandard.ZstdDecompressor()
    with decompressor.stream_reader(io.BytesIO(archive)) as reader:
        tar_bytes = reader.read()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        names = sorted(m.name for m in tf.getmembers() if m.isfile())
        assert names == ["project/a.txt", "project/sub/b.txt"]
        extracted = tf.extractfile("project/a.txt")
        assert extracted is not None
        assert extracted.read() == b"hello"


def test_iter_tar_zst_handles_single_file(tmp_path: Path) -> None:
    src = tmp_path / "solo.txt"
    src.write_text("just one file")

    archive = b"".join(iter_tar_zst(src))

    decompressor = zstandard.ZstdDecompressor()
    with decompressor.stream_reader(io.BytesIO(archive)) as reader:
        tar_bytes = reader.read()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        member = tf.getmembers()[0]
        assert member.name == "solo.txt"
