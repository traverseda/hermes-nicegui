"""Tests for the durable running-session indicator predicate."""

from __future__ import annotations

from hermes_nicegui.gateway import Session
from hermes_nicegui.plugins.sessions.logic import is_running


def test_open_session_with_description_is_running() -> None:
    session = Session(id="x", last_activity_description="working")
    assert is_running(session) is True


def test_open_session_without_description_is_idle() -> None:
    assert is_running(Session(id="x")) is False
    assert is_running(Session(id="x", last_activity_description="")) is False


def test_ended_session_with_description_is_not_running() -> None:
    session = Session(id="x", ended_at=1.0, last_activity_description="working")
    assert is_running(session) is False


def test_ended_session_without_description_is_not_running() -> None:
    assert is_running(Session(id="x", ended_at=1.0)) is False
