"""Cron plugin: browse and manage scheduled Hermes jobs.

Listing reads the daemon's own `cron/jobs.json` directly; mutations
(create/edit/pause/resume/remove/run) go through the `hermes` CLI -- both via
``hermes_nicegui.web.current_store().cron`` (see ``hermes_nicegui.store``).
No dashboard REST dependency at all (unlike kanban, whose CLI has no
generic field-setter -- see ``plugins/kanban/__init__.py``).
"""

from __future__ import annotations

from hermes_nicegui.plugin import Plugin

from .ui import register_pages


class CronPlugin(Plugin):
    name = "cron"
    title = "Cron Jobs"
    icon = "event_repeat"
    route = "/cron"

    def register(self) -> None:
        register_pages(self)
        self.logger.info("cron plugin registered")
