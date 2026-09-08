"""Tests for the sessions plugin's chat-attachment helpers (pure logic)."""

from __future__ import annotations

from pathlib import Path

from hermes_nicegui.plugins.sessions.logic import (
    attachment_block,
    sanitize_filename,
    upload_target,
)


def test_sanitize_filename_takes_basename_only() -> None:
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("/a/b/c.txt") == "c.txt"
    assert sanitize_filename("C:\\fakepath\\notes.txt") == "notes.txt"
    assert sanitize_filename(r"..\..\win\evil.exe") == "evil.exe"


def test_sanitize_filename_strips_control_chars_and_dots() -> None:
    assert sanitize_filename("a\x00b\x1fc.txt") == "abc.txt"
    assert sanitize_filename("...") == "upload"
    assert sanitize_filename("") == "upload"
    assert sanitize_filename("   ") == "upload"
    # Windows refuses trailing dots/spaces; so do we.
    assert sanitize_filename("report. ") == "report"


def test_upload_target_uses_sanitized_session_and_filename() -> None:
    target = upload_target(Path("/uploads"), "sess-1", "notes.txt")
    assert target == Path("/uploads/sess-1/notes.txt")


def test_upload_target_scrubs_unsafe_session_id() -> None:
    target = upload_target(Path("/uploads"), "../../evil", "a.txt")
    assert target == Path("/uploads/evil/a.txt")
    # A session id that is only dots collapses to the fallback component.
    target = upload_target(Path("/uploads"), "...", "a.txt")
    assert target == Path("/uploads/session/a.txt")


def test_upload_target_avoids_collisions(tmp_path: Path) -> None:
    (tmp_path / "sess-1").mkdir()
    (tmp_path / "sess-1" / "notes.txt").write_text("first")
    target = upload_target(tmp_path, "sess-1", "notes.txt")
    assert target == tmp_path / "sess-1" / "notes (1).txt"
    target.write_text("second")
    assert upload_target(tmp_path, "sess-1", "notes.txt") == tmp_path / "sess-1" / "notes (2).txt"


def test_attachment_block_empty() -> None:
    assert attachment_block([]) == ""


def test_attachment_block_single_and_multiple() -> None:
    assert attachment_block(["/x/a.txt"]) == "Attached files:\n- /x/a.txt"
    assert attachment_block(["/x/a.txt", "/x/b.txt"]) == (
        "Attached files:\n- /x/a.txt\n- /x/b.txt"
    )
