"""Tests for the xaelwiki plugin pages: rendering, search, detail view."""

from __future__ import annotations

from pathlib import Path

from nicegui.testing import User

from hermes_nicegui import web
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.xaelwiki import XaelWikiPlugin

NOTE_A = """---
title: Spiri — project family
created: '2026-08-11'
updated: 2026-08-11 21:23 UTC
tags:
- spiri
- index
status: evergreen
folder: 30-resources
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
folder: 00-inbox
---

Milk, eggs, bread.
"""

ROOT_NOTE = """---
title: Root level note
---

A note at the vault root.
"""


def _write_vault(root: Path) -> None:
    (root / "30-resources").mkdir(parents=True)
    (root / "00-inbox").mkdir()
    (root / "30-resources" / "spiri-project-family.md").write_text(NOTE_A)
    (root / "00-inbox" / "grocery-list.md").write_text(NOTE_B)


def _plugin(context: PluginContext, vault: Path) -> XaelWikiPlugin:
    return XaelWikiPlugin(context, notes_dir=str(vault), refresh_seconds=3600)


async def test_notes_page_renders_all_notes(
    user: User, context: PluginContext, tmp_path: Path
) -> None:
    _write_vault(tmp_path)
    web.build(context, [_plugin(context, tmp_path)])
    await user.open("/xaelwiki")
    await user.should_see("Spiri — project family")
    await user.should_see("Grocery list")
    await user.should_see("00-inbox (1)")
    await user.should_see("30-resources (1)")
    assert len(list(user.find(marker="notes-folder-header").elements)) == 2


async def test_notes_page_renders_root_notes_in_grouped_and_filtered_views(
    user: User, context: PluginContext, tmp_path: Path
) -> None:
    _write_vault(tmp_path)
    (tmp_path / "root-note.md").write_text(ROOT_NOTE)
    web.build(context, [_plugin(context, tmp_path)])
    await user.open("/xaelwiki")
    await user.should_see("root (1)")
    await user.should_see("Root level note")

    user.find(marker="notes-search-input").type("Root level note")
    await user.should_see("Root level note", retries=10)


async def test_nav_item_present(user: User, context: PluginContext, tmp_path: Path) -> None:
    _write_vault(tmp_path)
    web.build(context, [_plugin(context, tmp_path)])
    await user.open("/")
    await user.should_see("Notes")


async def test_search_filters_in_real_time(
    user: User, context: PluginContext, tmp_path: Path
) -> None:
    _write_vault(tmp_path)
    web.build(context, [_plugin(context, tmp_path)])
    await user.open("/xaelwiki")
    await user.should_see("Spiri — project family")

    user.find(marker="notes-search-input").type("grocery")
    await user.should_see("Grocery list", retries=10)
    await user.should_not_see("Spiri — project family", retries=10)
    assert len(list(user.find(marker="notes-folder-header").elements)) == 1


async def test_search_matches_body_content(
    user: User, context: PluginContext, tmp_path: Path
) -> None:
    _write_vault(tmp_path)
    web.build(context, [_plugin(context, tmp_path)])
    await user.open("/xaelwiki")
    user.find(marker="notes-search-input").type("sensing")
    await user.should_see("Spiri — project family", retries=10)
    await user.should_not_see("Grocery list", retries=10)


async def test_folder_filter_hides_other_folders(
    user: User, context: PluginContext, tmp_path: Path
) -> None:
    _write_vault(tmp_path)
    web.build(context, [_plugin(context, tmp_path)])
    await user.open("/xaelwiki")
    await user.should_see("Grocery list")

    select = next(iter(user.find(marker="notes-folder-select").elements))
    select.set_value("00-inbox")

    await user.should_see("Grocery list", retries=10)
    await user.should_not_see("Spiri — project family", retries=10)


async def test_click_note_opens_detail(user: User, context: PluginContext, tmp_path: Path) -> None:
    _write_vault(tmp_path)
    web.build(context, [_plugin(context, tmp_path)])
    await user.open("/xaelwiki")
    await user.should_see("Spiri — project family")

    # Narrow to one card so the click target is unambiguous (the list is
    # newest-updated first, so the first card is the grocery list).
    user.find(marker="notes-search-input").type("spiri")
    await user.should_see("Spiri — project family", retries=10)
    await user.should_not_see("Grocery list", retries=10)

    user.find(marker="notes-card").click()
    await user.should_see("Distributed sensing network", retries=10)


async def test_detail_shows_metadata_and_body(
    user: User, context: PluginContext, tmp_path: Path
) -> None:
    _write_vault(tmp_path)
    web.build(context, [_plugin(context, tmp_path)])
    await user.open("/xaelwiki/spiri-project-family")
    await user.should_see("Spiri — project family")
    await user.should_see("evergreen")
    await user.should_see("Distributed sensing network")


async def test_detail_unknown_note_shows_error(
    user: User, context: PluginContext, tmp_path: Path
) -> None:
    _write_vault(tmp_path)
    web.build(context, [_plugin(context, tmp_path)])
    await user.open("/xaelwiki/does-not-exist")
    await user.should_see("Note not found.")
