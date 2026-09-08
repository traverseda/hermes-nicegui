"""VNC plugin: show a sandboxed bot's Xvfb display via noVNC in the browser.

A companion systemd service runs Xvfb :99 + x11vnc (bound to 127.0.0.1:5901,
with a random per-start password at /run/hermes-vnc/passwd). When the bot
driving a sandboxed chromium on that display hits a captcha/human-wall it
writes /var/lib/hermes-vnc/state.json with ``help_requested: true``. This
plugin shows that screen to the operator in the browser tab, lets them
click/type on it (view-only by default, enabled per explicit action), and a
"Done" button clears ``help_requested``.

The page and its raw routes live in ``ui.py``/``logic.py`` respectively --
``logic.py`` holds the pure bridge/token/state helpers plus the two raw HTTP
routes (the ``/vnc/token`` POST and the ``/vnc/ws`` websocket) that can't be
expressed as a NiceGUI page.
"""

from __future__ import annotations

from hermes_nicegui.plugin import Plugin

from .logic import register_routes
from .ui import register_pages


class VncPlugin(Plugin):
    name = "vnc"
    title = "VNC"
    icon = "desktop_windows"
    route = "/vnc"

    def register(self) -> None:
        register_pages(self)
        register_routes(self)
        self.logger.info("vnc plugin registered")
