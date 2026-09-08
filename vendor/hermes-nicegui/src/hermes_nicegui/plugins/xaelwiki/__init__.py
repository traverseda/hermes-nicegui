"""Xaelwiki plugin: browse and search the xaelwiki notes vault.

Read-only. The plugin reads the vault (a git repo of markdown files with
YAML frontmatter) straight from disk via :class:`NoteStore` — no xaelwiki
MCP server, no auth token, no writes. The notes directory is configurable
through the ``HERMES_XAELWIKI_NOTES_DIR`` / ``HERMES_XAELWIKI_REFRESH_SECONDS``
settings (or constructor args), see the README.
"""

from __future__ import annotations

from hermes_nicegui.plugin import Plugin, PluginContext

from .logic import NoteStore
from .ui import register_pages


class XaelWikiPlugin(Plugin):
    name = "xaelwiki"
    title = "Notes"
    icon = "menu_book"
    route = "/xaelwiki"

    def __init__(
        self,
        context: PluginContext,
        *,
        notes_dir: str | None = None,
        refresh_seconds: float | None = None,
    ) -> None:
        super().__init__(context)
        self.store = NoteStore(
            notes_dir or context.settings.xaelwiki_notes_dir,
            refresh_seconds
            if refresh_seconds is not None
            else context.settings.xaelwiki_refresh_seconds,
        )

    def register(self) -> None:
        register_pages(self)
        self.logger.info(
            "xaelwiki plugin registered (notes dir: {}, refresh: {}s)",
            self.store.notes_dir,
            self.store.refresh_seconds,
        )
