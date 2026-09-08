"""Tests for the email-inbox unread indicator."""

from __future__ import annotations

from hermes_nicegui.gateway import Session
from hermes_nicegui.plugins.sessions.logic import ALL_READ_KEY, is_unread


def test_unread_logic() -> None:
    s = Session(id="x", message_count=5, last_active=2000.0)
    assert is_unread(s, {}) is True
    assert is_unread(s, {"x": 1999.0}) is True
    assert is_unread(s, {"x": 2000.0}) is False


def test_empty_session_not_unread() -> None:
    s = Session(id="x", message_count=0, last_active=2000.0)
    assert is_unread(s, {}) is False


def test_no_activity_not_unread() -> None:
    s = Session(id="x", message_count=5, last_active=None)
    assert is_unread(s, {}) is False


def test_all_read_marker_newer_than_activity_is_read() -> None:
    """The global marker alone covers a session with no per-session entry."""
    s = Session(id="x", message_count=5, last_active=2000.0)
    assert is_unread(s, {ALL_READ_KEY: 2000.0}) is False
    assert is_unread(s, {ALL_READ_KEY: 2001.0}) is False


def test_all_read_marker_older_than_activity_is_still_unread() -> None:
    s = Session(id="x", message_count=5, last_active=2000.0)
    assert is_unread(s, {ALL_READ_KEY: 1999.0}) is True


def test_all_read_marker_wins_over_staler_per_session_entry() -> None:
    """Per-session entry older than the marker but last_active between them:
    the marker's higher threshold is what counts (``max()`` wins)."""
    s = Session(id="x", message_count=5, last_active=1500.0)
    assert is_unread(s, {"x": 1000.0, ALL_READ_KEY: 2000.0}) is False


def test_all_read_marker_does_not_revive_empty_session() -> None:
    s = Session(id="x", message_count=0, last_active=2000.0)
    assert is_unread(s, {ALL_READ_KEY: 2001.0}) is False


def test_all_read_marker_does_not_revive_inactive_session() -> None:
    s = Session(id="x", message_count=5, last_active=None)
    assert is_unread(s, {ALL_READ_KEY: 2001.0}) is False
