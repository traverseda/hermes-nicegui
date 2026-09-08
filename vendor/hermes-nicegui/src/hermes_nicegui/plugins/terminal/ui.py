"""Terminal plugin page: a browser terminal backed by a real local shell.

Each page load forks its own pty-attached shell (``PtySession``, see
``logic.py``) and wires it to a ``ui.xterm()`` widget: keystrokes go from
xterm's ``on_data`` straight to the pty's stdin, and pty output is pushed to
the terminal via ``asyncio.add_reader`` on the (non-blocking) master fd --
the event loop only wakes us when there's actually something to read, no
polling. The shell is killed and the reader torn down when the tab
disconnects.
"""

from __future__ import annotations

import asyncio

from nicegui import ui
from nicegui.events import XtermDataEventArguments, XtermResizeEventArguments

from hermes_nicegui.plugin import Plugin
from hermes_nicegui.plugins.terminal.logic import PtySession
from hermes_nicegui.web import frame


def register_pages(plugin: Plugin) -> None:
    logger = plugin.logger

    @ui.page("/terminal", title="Terminal")
    async def terminal_page() -> None:
        with frame(active="/terminal"):
            ui.label("Local terminal").classes("text-lg")

            ui.notify(
                "Click on the terminal area to start typing.",
                color="grey-7",
                position="top",
                timeout=5000,
            )

            terminal = (
                ui.xterm({"cursorBlink": True, "fontSize": 14})
                .classes("w-full h-[70vh]")
                .mark("terminal-view")
            )

            session = PtySession()
            fd = session.start()
            loop = asyncio.get_event_loop()

            def _pump() -> None:
                data = session.read()
                if data:
                    terminal.write(data)
                elif data is None:
                    loop.remove_reader(fd)
                    terminal.writeln("\r\n[process exited]")

            loop.add_reader(fd, _pump)

            def _on_data(e: XtermDataEventArguments) -> None:
                try:
                    session.write(e.data.encode())
                except OSError:
                    pass

            def _on_resize(e: XtermResizeEventArguments) -> None:
                try:
                    session.resize(e.cols, e.rows)
                except OSError:
                    pass

            terminal.on_data(_on_data)
            terminal.on_resize(_on_resize)

            # Command input bar with send button so users have a visible way
            # to submit commands (in addition to typing in the xterm area).
            with ui.row().classes("w-full items-center gap-2 mt-2"):
                cmd_input = ui.input(
                    placeholder="Type a command and press Send or Enter",
                ).classes("grow")

                def _on_send() -> None:
                    cmd = cmd_input.value
                    if cmd:
                        # Append a newline so the shell executes the command
                        session.write((cmd + "\n").encode())
                        cmd_input.value = ""

                cmd_input.on("keydown.enter", _on_send)
                ui.button("Send", on_click=_on_send, icon="play_arrow")

            def _cleanup() -> None:
                loop.remove_reader(fd)
                session.close()
                logger.debug("terminal session {} closed", session.pid)

            ui.context.client.on_disconnect(_cleanup)

            # A one-off `fit()` only matches the pty to the terminal's size
            # at load; it drifts the moment the browser window is resized or
            # the nav drawer is toggled (both change the container's size
            # with no window "resize" event of their own). A ResizeObserver
            # on the terminal's own DOM node catches every such change, not
            # just window resizes, and its first callback fires with the
            # initial layout too, so this replaces the one-off fit rather
            # than supplementing it. `getElement`/`getHtmlElement` are
            # NiceGUI's own globals for looking up a live Vue
            # component/DOM node by element id from injected JS -- see
            # `plugins/sessions/ui.py::_icon_tooltip` for the same pattern.
            ui.run_javascript(f"""
                (() => {{
                    const el = getHtmlElement({terminal.id});
                    if (!el) return;
                    new ResizeObserver(() => getElement({terminal.id}).fit()).observe(el);
                }})();
            """)

            logger.debug("terminal page rendered")
