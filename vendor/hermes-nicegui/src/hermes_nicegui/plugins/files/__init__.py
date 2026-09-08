"""Files plugin: browse, view/edit, download, and upload local files."""

from __future__ import annotations

from hermes_nicegui.plugin import Plugin

from .ui import register_pages


class FilesPlugin(Plugin):
    name = "files"
    title = "Files"
    icon = "folder"
    route = "/files"

    def register(self) -> None:
        register_pages(self)
        self.logger.info("files plugin registered")
