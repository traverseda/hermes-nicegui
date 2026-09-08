"""Tests for the server-side ReadStateStore."""

from __future__ import annotations

from pathlib import Path

from hermes_nicegui.auth import ReadStateStore


def test_load_empty(tmp_path: Path) -> None:
    store = ReadStateStore(tmp_path / "test.db")
    assert store.load("admin") == {}


def test_set_and_load(tmp_path: Path) -> None:
    store = ReadStateStore(tmp_path / "test.db")
    store.set_key("admin", "session_1", 1000.0)
    assert store.load("admin") == {"session_1": 1000.0}


def test_upsert(tmp_path: Path) -> None:
    store = ReadStateStore(tmp_path / "test.db")
    store.set_key("admin", "session_1", 1000.0)
    store.set_key("admin", "session_1", 2000.0)
    assert store.load("admin") == {"session_1": 2000.0}


def test_delete_key(tmp_path: Path) -> None:
    store = ReadStateStore(tmp_path / "test.db")
    store.set_key("admin", "session_1", 1000.0)
    store.delete_key("admin", "session_1")
    assert store.load("admin") == {}


def test_save_replaces_all(tmp_path: Path) -> None:
    store = ReadStateStore(tmp_path / "test.db")
    store.set_key("admin", "a", 1.0)
    store.set_key("admin", "b", 2.0)
    store.save("admin", {"c": 3.0})
    assert store.load("admin") == {"c": 3.0}


def test_isolation_between_users(tmp_path: Path) -> None:
    store = ReadStateStore(tmp_path / "test.db")
    store.set_key("admin", "s1", 100.0)
    store.set_key("other", "s1", 200.0)
    assert store.load("admin") == {"s1": 100.0}
    assert store.load("other") == {"s1": 200.0}
