"""VNC viewer page: status card, help-request banner, and a noVNC canvas.

The operator-facing half of the vnc plugin (raw routes live in ``logic.py``).
The page renders a status card (does the Xvfb display answer?), a
help-request banner driven by the bot's ``state.json``, and a noVNC canvas
wired through an injected ES-module script -- ``import RFB from
'/vnc/static/core/rfb.js'``. The display is view-only by default; input is
enabled only by an explicit operator action on the view-only switch.

The noVNC client script is injected once per page load via
``ui.add_head_html`` (per-client), and the Connect button only *invokes* the
registered ``window.__vncConnect``, so repeated connects never re-inject the
module. The VNC password and the bridge token travel straight from
``/vnc/token`` into the ``RFB`` constructor inside that script's closure --
never into a URL, a DOM attribute, or a log line.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from nicegui import app, background_tasks, ui
from nicegui.events import ValueChangeEventArguments

from hermes_nicegui.plugins.vnc.logic import clear_help_requested, display_online, read_state
from hermes_nicegui.web import frame

if TYPE_CHECKING:
    from hermes_nicegui.plugins.vnc import VncPlugin


def _client_script(canvas_id: str, conn_id: str) -> str:
    """The ES-module block that wires the noVNC RFB client to the canvas.

    Token and password exist only inside ``__vncConnect``'s closure; the
    websocket URL carries no query string. ``viewOnly: true`` by default --
    ``__vncSetViewOnly`` flips it only when the operator asks for input.
    """
    return f"""
    <script type="module">
    import RFB from '/vnc/static/core/rfb.js';

    let rfb = null;
    let ws = null;

    function setConn(text) {{
        const el = document.getElementById({conn_id!r});
        if (el) el.innerText = text;
    }}

    window.__vncConnect = async function (canvasId) {{
        if (rfb) return;
        const canvasEl = document.getElementById(canvasId);
        if (!canvasEl) return;
        let res;
        try {{
            res = await fetch('/vnc/token');
        }} catch (err) {{
            setConn('token endpoint unreachable');
            return;
        }}
        if (!res.ok) {{
            setConn('display offline');
            return;
        }}
        let token, password;
        try {{
            const data = await res.json();
            token = data.token;
            password = data.password;
        }} catch (err) {{
            setConn('bad token response');
            return;
        }}
        try {{
            const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
            const wsUrl = proto + location.host + '/vnc/ws';
            ws = new WebSocket(wsUrl);
            ws.onopen = function () {{
                ws.send(JSON.stringify({{token: token}}));
                rfb = new RFB(canvasEl, ws, {{
                    credentials: {{password: password}},
                    viewOnly: true,
                    shared: true,
                }});
                window.__vncRfb = rfb;
                rfb.addEventListener('connect', function () {{ setConn('connected'); }});
                rfb.addEventListener('disconnect', function () {{ setConn('disconnected'); }});
            }};
            ws.onerror = function () {{ setConn('websocket error'); }};
            ws.onclose = function (e) {{
                if (e.code === 1008) setConn('rejected: invalid token');
                rfb = null;
                ws = null;
            }};
        }} catch (err) {{
            setConn('connect failed');
        }}
    }};

    window.__vncSetViewOnly = function (on) {{
        if (window.__vncRfb) window.__vncRfb.viewOnly = !!on;
    }};

    window.__vncDisconnect = function () {{
        try {{ if (window.__vncRfb) window.__vncRfb.disconnect(); }} catch (err) {{}}
        try {{ if (ws) ws.close(); }} catch (err) {{}}
        rfb = null;
        ws = null;
    }};
    </script>
    """


def register_pages(plugin: VncPlugin) -> None:
    logger = plugin.logger
    settings = plugin.settings
    state_path = Path(settings.vnc_state_dir) / "state.json"

    novnc_dir = Path(settings.vnc_novnc_dir) / "share" / "webapps" / "novnc"
    assets_ok = novnc_dir.is_dir()
    if assets_ok:
        app.add_static_files("/vnc/static", novnc_dir)
    else:
        logger.warning("vnc: noVNC assets not found at {}", novnc_dir)

    @ui.page("/vnc", title="VNC")
    async def vnc_page() -> None:
        canvas: ui.element | None = None
        with frame(active="/vnc"):
            ui.label("VNC viewer").classes("text-lg")

            with ui.card().classes("w-full").mark("vnc-status-card"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label("Display:").classes("text-sm opacity-70")
                    status_label = (
                        ui.label("display offline")
                        .classes("text-sm font-medium")
                        .mark("vnc-display-status")
                    )
                    ui.space()
                    conn_label = (
                        ui.label("not connected")
                        .classes("text-xs opacity-60")
                        .mark("vnc-connection-status")
                    )

            banner = ui.card().classes("w-full").mark("vnc-help-banner")
            with banner:
                ui.label("Help requested").classes("text-sm font-bold")
                help_message = ui.label().classes("text-sm")
                help_link = ui.link("", "").classes("text-sm")

            async def _refresh_status() -> None:
                ok = await display_online(settings.vnc_host, settings.vnc_port)
                status_label.text = "display online" if ok else "display offline"

            async def _poll_help() -> None:
                data = await asyncio.to_thread(read_state, state_path)
                if data.get("help_requested"):
                    help_message.text = data.get("message") or ""
                    help_url = data.get("url") or ""
                    help_link.text = help_url
                    help_link.props(f"href={help_url}")
                    if not banner.visible:
                        banner.set_visibility(True)
                elif banner.visible:
                    banner.set_visibility(False)

            async def _clear_help() -> None:
                await asyncio.to_thread(clear_help_requested, state_path)
                await _poll_help()

            def _on_connect() -> None:
                if canvas is None:
                    ui.notify("VNC client assets missing", type="negative")
                    return
                try:
                    ui.run_javascript(f"__vncConnect({canvas.html_id!r})")
                except Exception as exc:  # noqa: BLE001 - surface any wiring failure
                    ui.notify(f"VNC connect failed: {exc}", type="negative")

            def _on_view_only_change(e: ValueChangeEventArguments) -> None:
                ui.run_javascript(f"__vncSetViewOnly({e.value})")
                if not e.value:
                    logger.info("vnc: input enabled")

            with banner:
                ui.button(
                    "Done",
                    icon="check",
                    on_click=lambda: background_tasks.create(_clear_help()),
                ).mark("vnc-done-button")
            banner.set_visibility(False)

            with ui.row().classes("w-full items-center gap-2"):
                ui.button("Connect", icon="cast_connected", on_click=_on_connect).mark(
                    "vnc-connect-button"
                )
                ui.switch(
                    "Input is off by default — enable it only when you need to click/type",
                    value=True,
                    on_change=_on_view_only_change,
                ).mark("vnc-viewonly-switch")

            if assets_ok:
                canvas = (
                    ui.element("div")
                    .classes("w-full")
                    .style("height: 70vh; background: #111")
                    .mark("vnc-canvas")
                )
                ui.add_head_html(_client_script(canvas.html_id, conn_label.html_id))
            else:
                ui.label(
                    "noVNC client assets missing — set HERMES_VNC_NOVNC_DIR "
                    "to the noVNC install path."
                ).classes("opacity-70").mark("vnc-assets-missing")

            def _cleanup() -> None:
                try:
                    ui.run_javascript("window.__vncDisconnect && window.__vncDisconnect();")
                except Exception:  # noqa: BLE001 - best-effort teardown
                    pass

            ui.context.client.on_disconnect(_cleanup)

            await _refresh_status()
            ui.timer(5.0, _refresh_status)
            await _poll_help()
            ui.timer(3.0, _poll_help)

            logger.debug("vnc page rendered")
