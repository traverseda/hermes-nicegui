"""Kanban plugin: view and manage tasks on the Hermes dashboard's kanban board.

Reads and writes alike go through ``context.executor`` (a ``HermesExecutor``)
-- unlike the other plugins here, this one's *writes* end up on the Hermes
CLI's own dashboard web server (cookie/password auth) rather than the CLI
itself or the main gateway, but that's ``HermesExecutor``'s concern (see its
"kanban writes" section), not this plugin's. See ``hermes_nicegui.app``'s
``_bootstrap`` for where the dashboard credentials get wired into the
executor.
"""

from __future__ import annotations

from hermes_nicegui.plugin import Plugin

from .ui import register_pages


class KanbanPlugin(Plugin):
    name = "kanban"
    title = "Kanban"
    icon = "view_kanban"
    route = "/kanban"

    def register(self) -> None:
        register_pages(self)
        self.logger.info("kanban plugin registered")
