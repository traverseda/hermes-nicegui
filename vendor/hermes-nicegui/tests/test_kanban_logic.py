"""Tests for the kanban plugin's pure helpers."""

from __future__ import annotations

from hermes_nicegui.plugins.kanban.logic import (
    CANONICAL_COLUMNS,
    STAGE_HELP,
    column_meta,
    fmt_epoch,
    fmt_priority,
)


def test_fmt_epoch_missing() -> None:
    assert fmt_epoch(None) == "—"


def test_fmt_epoch_formats_known_value() -> None:
    assert fmt_epoch(1786620000) != "—"


def test_column_meta_known() -> None:
    label, _icon, color = column_meta("blocked")
    assert label == "Blocked"
    assert color == "negative"


def test_column_meta_unknown_falls_back() -> None:
    _label, icon, _color = column_meta(None)
    assert icon == "help_outline"


def test_fmt_priority() -> None:
    assert fmt_priority(2) == "P2"
    assert fmt_priority(None) == "—"


def test_canonical_columns_has_eight_statuses() -> None:
    assert len(CANONICAL_COLUMNS) == 8
    assert "ready" in CANONICAL_COLUMNS
    assert "triage" in CANONICAL_COLUMNS
    assert CANONICAL_COLUMNS.index("review") < CANONICAL_COLUMNS.index("blocked")


def test_stage_help_covers_every_canonical_column() -> None:
    assert all(STAGE_HELP.get(status) for status in CANONICAL_COLUMNS)
