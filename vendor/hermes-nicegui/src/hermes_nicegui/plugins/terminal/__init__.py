"""Terminal plugin: a browser-based terminal backed by a local shell."""

from __future__ import annotations

from hermes_nicegui.plugin import Plugin

from .ui import register_pages


class TerminalPlugin(Plugin):
    name = "terminal"
    title = "Terminal"
    icon = "terminal"
    route = "/terminal"

    def register(self) -> None:
        register_pages(self)
        self.logger.info("terminal plugin registered")
