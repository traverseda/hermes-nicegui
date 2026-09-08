"""Sessions plugin: browse Hermes sessions and inspect their transcripts."""

from __future__ import annotations

from hermes_nicegui.plugin import Plugin

from .ui import register_pages


class SessionsPlugin(Plugin):
    name = "sessions"
    title = "Sessions"
    icon = "forum"
    route = "/sessions"

    def register(self) -> None:
        register_pages(self)
        self.logger.info("sessions plugin registered")
