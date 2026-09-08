"""Tests for the xaelwiki plugin's NoteStore (scan, search, folders)."""

from __future__ import annotations

from pathlib import Path

from hermes_nicegui.plugins.xaelwiki.logic import NoteStore, split_frontmatter

NOTE_A = """---
id: 20260811-0924a2
title: Spiri — project family
created: '2026-08-11'
updated: 2026-08-11 21:23 UTC
tags:
- spiri
- index
status: evergreen
folder: 30-resources
source: organizer
---

# Spiri — project family

Distributed sensing network.
"""

NOTE_B = """---
title: Grocery list
created: '2026-08-12'
updated: 2026-08-12 09:00 UTC
tags:
- household
status: active
---

Milk, eggs, bread.
"""

PLAIN_NOTE = """Just a plain markdown file with no frontmatter.

Some body text about antennas.
"""


def _write_vault(root: Path) -> None:
    (root / "30-resources").mkdir(parents=True)
    (root / "00-inbox").mkdir()
    (root / "30-resources" / "spiri-project-family.md").write_text(NOTE_A)
    (root / "00-inbox" / "grocery-list.md").write_text(NOTE_B)
    (root / "00-inbox" / "plain.md").write_text(PLAIN_NOTE)


def test_split_frontmatter_parses() -> None:
    fm, body = split_frontmatter(NOTE_A)
    assert fm is not None
    assert fm["title"] == "Spiri — project family"
    assert fm["status"] == "evergreen"
    assert "Distributed sensing network" in body


def test_split_frontmatter_missing() -> None:
    fm, body = split_frontmatter(PLAIN_NOTE)
    assert fm is None
    assert body == PLAIN_NOTE


def test_scan_lists_all_notes(tmp_path: Path) -> None:
    _write_vault(tmp_path)
    store = NoteStore(tmp_path, refresh_seconds=0)
    notes = store.notes()
    assert {n.title for n in notes} == {
        "Spiri — project family",
        "Grocery list",
        "plain",
    }


def test_scan_reads_frontmatter_metadata(tmp_path: Path) -> None:
    _write_vault(tmp_path)
    store = NoteStore(tmp_path)
    note = store.get("spiri-project-family")
    assert note is not None
    assert note.folder == "30-resources"
    assert note.status == "evergreen"
    assert note.tags == ["spiri", "index"]
    assert note.id == "spiri-project-family"
    assert note.rel_path == "30-resources/spiri-project-family.md"


def test_plain_note_gets_filename_title(tmp_path: Path) -> None:
    _write_vault(tmp_path)
    store = NoteStore(tmp_path)
    note = store.get("plain")
    assert note is not None
    assert note.title == "plain"
    assert note.body.strip().startswith("Just a plain markdown")


def test_search_matches_title(tmp_path: Path) -> None:
    _write_vault(tmp_path)
    store = NoteStore(tmp_path)
    assert {n.id for n in store.search("Grocery")} == {"grocery-list"}


def test_search_matches_body_content(tmp_path: Path) -> None:
    _write_vault(tmp_path)
    store = NoteStore(tmp_path)
    assert {n.id for n in store.search("sensing")} == {"spiri-project-family"}


def test_search_matches_tags(tmp_path: Path) -> None:
    _write_vault(tmp_path)
    store = NoteStore(tmp_path)
    assert {n.id for n in store.search("household")} == {"grocery-list"}


def test_search_case_insensitive(tmp_path: Path) -> None:
    _write_vault(tmp_path)
    store = NoteStore(tmp_path)
    assert {n.id for n in store.search("GROCERY")} == {"grocery-list"}


def test_search_empty_query_returns_all(tmp_path: Path) -> None:
    _write_vault(tmp_path)
    store = NoteStore(tmp_path)
    assert len(store.search("")) == 3
    assert len(store.search("   ")) == 3


def test_folder_filter(tmp_path: Path) -> None:
    _write_vault(tmp_path)
    store = NoteStore(tmp_path)
    ids = {n.id for n in store.search("", folder="00-inbox")}
    assert ids == {"grocery-list", "plain"}
    assert "spiri-project-family" not in ids


def test_folders_canonical_order_first(tmp_path: Path) -> None:
    _write_vault(tmp_path)
    store = NoteStore(tmp_path)
    assert store.folders() == ["00-inbox", "30-resources"]


def test_get_unknown_returns_none(tmp_path: Path) -> None:
    _write_vault(tmp_path)
    store = NoteStore(tmp_path)
    assert store.get("does-not-exist") is None


def test_duplicate_stems_disambiguated(tmp_path: Path) -> None:
    _write_vault(tmp_path)
    (tmp_path / "10-projects").mkdir()
    (tmp_path / "10-projects" / "plain.md").write_text(
        "---\ntitle: plain (project)\n---\n\nProject body."
    )
    store = NoteStore(tmp_path)
    notes = [n for n in store.notes() if n.id.startswith("plain")]
    assert len(notes) == 2
    ids = {n.id for n in notes}
    assert len(ids) == 2  # one bare "plain", one suffixed
    assert store.get("plain") is not None


def test_sort_newest_updated_first(tmp_path: Path) -> None:
    _write_vault(tmp_path)
    store = NoteStore(tmp_path)
    notes = store.notes()
    assert notes[0].id == "grocery-list"  # updated 2026-08-12 > 2026-08-11


def test_missing_directory_reports_error(tmp_path: Path) -> None:
    store = NoteStore(tmp_path / "nope")
    assert store.notes() == []
    assert store.last_error is not None
