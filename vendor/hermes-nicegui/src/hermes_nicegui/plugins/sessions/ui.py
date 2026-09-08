"""Sessions plugin pages: session list, a per-session event timeline, and chat.

The transcript reads as a compact chat timeline with a *single* data-driven
render path: :func:`render_transcript_events` builds every entry -- user
bubble, reasoning ``_step``, fused tool call+result, orphan tool result, and
Hermes' reply -- from the ``list[Message]`` buffer, in buffer order. Historical
pages render the buffer as-is; a live SSE turn instead folds its events into
the same buffer (one pending assistant ``Message``) and re-renders through the
same function, which returns live-update handles so streamed reasoning/reply
deltas and per-tool status glyphs update in place. Because rendering is purely
data-driven, any SSE arrival order renders canonically
(user -> reasoning -> tool calls -> reply), and the authoritative per-turn
transcript from ``run.completed`` still replaces the local buffer so a
completed turn is identical to a reload. Timestamps are available on hover
(tooltip) rather than printed on every row.

Session *reads* (list, detail, rename, delete) go straight at the daemon's
own SQLite ``state.db`` for whichever profile is active for this browser tab
(``hermes_nicegui.web.current_store``). *Sending* a chat turn still goes
through the gateway's bearer-token streaming API (``plugin.client``,
single global profile from ``Settings.client_kwargs()``) -- there's no
generic "append a message" mutation on the daemon's own state to read back
locally, and the gateway already has one (``stream_turn``). This is the
original inline-compose-box design; a later rework briefly replaced it with
a page that ran ``hermes chat`` in a pty, which was reverted because it
wasn't an actual chat interface.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from nicegui import app, background_tasks, ui
from nicegui.events import UploadEventArguments

from hermes_nicegui import web
from hermes_nicegui.gateway import HermesError, Message, Session
from hermes_nicegui.pagination import Pager, render_pager
from hermes_nicegui.plugin import Plugin
from hermes_nicegui.plugins.sessions.logic import (
    ALL_READ_KEY,
    attachment_block,
    fmt_age,
    fmt_cost,
    fmt_tokens,
    fmt_ts,
    is_running,
    is_unread,
    oneline,
    pretty_yaml,
    source_icon,
    upload_target,
)
from hermes_nicegui.web import frame

SOURCES = {
    "": "All sources",
    "cli": "Terminal",
    "webui": "Web UI",
    "api_server": "API",
    "cron": "Cron",
    "kanban": "Kanban",
    "telegram": "Telegram",
    "discord": "Discord",
    "slack": "Slack",
    "desktop": "Desktop",
    "dashboard": "Dashboard",
    "hermes_browser": "Hermes Browser",
    "browser": "Browser",
}
_SEEN_KEY = "sessions_seen"

#: Cap on chat attachments awaiting send in one message (the uploader's own
#: ``max-files`` prop enforces the same limit client-side).
_MAX_CHAT_ATTACHMENTS = 8


def _unread_state() -> dict[str, float]:
    """Per-browser map of session_id -> last-seen timestamp.

    Primary storage is ``app.storage.user`` (NiceGUI's per-browser
    session dict).  A server-side SQLite ``ReadStateStore`` underpins it
    for cross-device sync: on page open the local dict is hydrated from
    the store, and every mutation is persisted back.
    """
    try:
        return app.storage.user.setdefault(_SEEN_KEY, {})
    except RuntimeError:  # pragma: no cover - storage disabled in some test setups
        return {}


def _hydrate_read_state() -> None:
    """Load the server-side read state into the local ``app.storage.user`` dict.

    Called on each sessions-page open so a new device immediately sees
    read state written by another browser.
    """
    from hermes_nicegui.web import current_username, state

    if state.read_state_store is None:
        return
    try:
        username = current_username()
    except RuntimeError:
        return
    if not username:
        return
    server_state = state.read_state_store.load(username)
    local = _unread_state()
    # Merge: take the max timestamp per key so the "most-read" wins.
    for key, ts in server_state.items():
        if key not in local or ts > local[key]:
            local[key] = ts


def _persist_read_state() -> None:
    """Write the current local read state to the server-side store."""
    from hermes_nicegui.web import current_username, state

    if state.read_state_store is None:
        return
    try:
        username = current_username()
    except RuntimeError:
        return
    if not username:
        return
    state.read_state_store.save(username, _unread_state())


# -- transcript rendering ---------------------------------------------------
#
# Historical messages and live SSE events converge on the same timeline entry
# types, produced by one data-driven render function
# (:func:`render_transcript_events`). Every event -- said or done -- gets
# exactly one ``ui.timeline_entry``. The icon+color say what kind of event it
# is; no separate title/subtitle text duplicates that. Timestamps sit behind a
# hover tooltip on that same icon (see ``_icon_tooltip``) rather than a
# separate element or a permanently visible subtitle.
#
# Live streaming keeps a single pending assistant ``Message`` in the buffer and
# folds SSE events into it (see the detail page's ``_run_turn``); rendering is
# re-invoked through the same function. The message whose id is passed as
# ``active_id`` renders eagerly and open, and the returned handle gives the
# stream code in-place references (``reasoning_md``/``reply_md`` markdown
# bodies, per-tool ``LiveToolRow`` controls) so per-token deltas update without
# rebuilding the whole timeline. ``tool_status`` renders a small spinner or a
# check/error glyph next to a tool row's label while a turn is live.

# Quasar's own rule for an icon entry's subtitle is `padding-top: 8px`
# (`.q-timeline__entry--icon .q-timeline__subtitle`); the icon glyph itself
# is absolutely positioned at `top: 0` of the entry, so 8px still sits a
# few pixels below its vertical center. Halved to bring the two flush.
# Quasar also styles the subtitle (and, in dense-left mode, the content/
# title column) as small-caps and right-justified, both meant for a short
# date label next to the icon -- wrong for the sentence- and JSON-length
# text actually going there here.
_TIMELINE_CSS = """
.q-timeline__entry--icon .q-timeline__subtitle {
    padding-top: 4px !important;
}
.q-timeline__subtitle,
.q-timeline__content,
.q-timeline__title {
    text-align: left !important;
    text-transform: none !important;
    letter-spacing: normal !important;
}
.q-timeline__content {
    padding: 0 !important;
}
"""

# The chat composer's uploader is Quasar's QUploader styled down to a bare
# "+" button: no header background/title, no uploaded-file list (attachments
# render as our own chips instead). The header itself is the file-picker
# hit area (Quasar's hidden `q-uploader__input` overlays it), so a compact
# header is also a compact click/drag-drop target.
_UPLOADER_CSS = """
.chat-uploader .q-uploader__header {
    min-height: 0 !important;
    padding: 2px !important;
    background: transparent !important;
    color: inherit !important;
    box-shadow: none !important;
}
.chat-uploader .q-uploader__header-content {
    gap: 0 !important;
}
.chat-uploader .q-uploader__title,
.chat-uploader .q-uploader__subtitle {
    display: none !important;
}
.chat-uploader .q-uploader__list {
    display: none !important;
}
"""


def _icon_tooltip(entry: ui.timeline_entry, timestamp: float | None) -> None:
    """A native hover tooltip on the entry's own dot -- not a separate icon.

    ``ui.timeline_entry`` has no slot for its dot (confirmed against
    Quasar's ``QTimelineEntry`` source: the icon comes only from a prop,
    rendered with no slot to hook into), so this reaches into the rendered
    DOM directly and sets a plain ``title`` attribute on ``.q-timeline__dot``.
    A native tooltip has zero layout footprint -- nothing to misalign or
    resize on hover, unlike a Quasar ``q-tooltip`` sized against a
    full-row anchor.
    """
    text = json.dumps(fmt_ts(timestamp))
    ui.run_javascript(
        f'getHtmlElement({entry.id}).querySelector(".q-timeline__dot").title = {text}'
    )


@dataclass
class LiveToolRow:
    """Mutable controls for one tool row whose SSE lifecycle is still active.

    The row's ``label`` is the ``name: preview`` text; ``spinner`` is the
    per-tool progress glyph while the call runs; ``done_mark`` replaces it
    with a check/error icon once the call completes or fails. All three are
    created by :func:`render_transcript_events` and updated in place by the
    streaming code -- never re-created per event.
    """

    label: ui.label
    spinner: ui.spinner | None
    done_mark: ui.icon | None


@dataclass
class LiveEntry:
    """In-place update handles for the one message being streamed right now.

    Returned by :func:`render_transcript_events` for the message whose id
    equals ``active_id``; the message renders eagerly and open so these
    references exist. ``tool_rows`` is keyed by ``tool_call_id``.
    """

    reasoning_md: ui.markdown | None
    reply_md: ui.markdown | None
    tool_rows: dict[str, LiveToolRow]


@dataclass
class QueuedTurn:
    """A turn sent while another was still streaming, held for delivery
    once the current one finishes (see the detail page's ``_queue_message``/
    ``send``).

    ``local_ids`` names every locally-synthesized message this queued turn
    owns (one per merged send -- queuing twice concatenates ``text`` but
    keeps each message's own id, so this can hold more than one). Threaded
    into the next ``_run_turn`` call as ``own_local_ids`` so *that* turn's
    own ``run.completed`` reconciliation is what swaps these ids out for
    their authoritative counterparts -- never the turn that was already
    streaming when this one was queued.
    """

    text: str
    local_ids: list[int]


def _collapsible_entry(
    icon: str,
    color: str,
    summary: str,
    timestamp: float | None,
    render_body: Callable[[], None] | None,
    *,
    default_open: bool = False,
    mark: str | None = None,
    status: str | None = None,
) -> LiveToolRow | None:
    """A timeline entry whose header *is* the summary, via Quasar's
    ``subtitle`` slot.

    Quasar's own CSS gives ``.q-timeline__entry--icon .q-timeline__subtitle``
    just ``padding-top: 8px``, versus the much larger offset on
    ``.q-timeline__content``/``title`` -- so the subtitle slot sits close to
    where the icon itself is drawn (``.q-timeline__dot .q-icon`` is
    absolutely positioned at ``top: 0`` of the entry). The default ``title``
    slot could not be made to line up with the icon without fighting that
    layout; the subtitle slot already does, natively.

    ``render_body`` only runs the first time the expansion is actually
    opened, not at build time -- on a long transcript almost none of these
    ever get opened, so building every tool result's YAML/markdown (and the
    DOM nodes for it) up front is pure waste. See ``_lazy_expansion_body``.
    ``default_open`` skips that laziness and builds immediately -- used for
    the one entry (the transcript's last message) that should already be
    visible without a click.

    ``mark`` (a test-visible marker on the entry) and ``status`` are only
    used by the live render path. ``status`` ("running"/"done"/"failed")
    swaps the summary for a row with the matching status glyph -- a small
    spinner while running, a check/error icon once finished -- beside the
    label, and returns the :class:`LiveToolRow` of in-place controls so the
    stream can update them. ``status`` is None for purely historical rows.
    """
    entry = ui.timeline_entry(icon=icon, color=color)
    if mark is not None:
        entry.mark(mark)
    _icon_tooltip(entry, timestamp)
    with entry.add_slot("subtitle"):
        if status is not None:
            with ui.row().classes("items-center gap-2 w-full"):
                if status == "running":
                    spinner = ui.spinner(size="sm")
                    done_mark = None
                else:
                    spinner = None
                    done_mark = ui.icon(
                        "error" if status == "failed" else "check_circle", size="xs"
                    ).mark("live-tool-failed" if status == "failed" else "live-tool-done")
                label_el = ui.label(summary).classes("text-xs")
            return LiveToolRow(label=label_el, spinner=spinner, done_mark=done_mark)
        if render_body is None:
            # Matches Quasar's own `.q-item` header padding on the
            # `ui.expansion` branch below (`padding: 2px 16px`, confirmed
            # against the rendered DOM) -- without it, a plain label sits
            # flush left while every collapsible row's text starts 16px in,
            # making the transcript's left edge ragged wherever a
            # single-line row (nothing to expand) sits next to one that has
            # a body.
            ui.label(summary).classes("text-xs w-full py-0.5 px-4")
        else:
            expansion = (
                ui.expansion(summary, value=default_open).props("dense").classes("text-xs w-full")
            )
            if default_open:
                with expansion:
                    render_body()
            else:
                _lazy_expansion_body(expansion, render_body)
    return None


def _lazy_expansion_body(expansion: ui.expansion, render_body: Callable[[], None]) -> None:
    """Defer building ``render_body``'s content into ``expansion`` until it's
    opened for the first time, then leave it in the DOM (no need to tear
    down on collapse -- Quasar itself just hides it via CSS)."""
    built = False

    def _build_once() -> None:
        nonlocal built
        if built or not expansion.value:
            return
        built = True
        with expansion:
            render_body()

    expansion.on_value_change(_build_once)


def _scroll_to_bottom(anchor: ui.element) -> None:
    """Keep the newest content in view as a live turn grows the page.

    Targets both plain ``window`` scroll and Quasar's own
    ``.q-page-container`` scroll region, since which one actually owns the
    scrollbar depends on layout/viewport and there's no cheap way to ask.

    ``anchor`` must be an element belonging to the page's own client --
    ``ui.run_javascript`` resolves *which* browser client to send the script
    to from the current slot stack, which a background task (this always
    runs from one -- see ``background_tasks.create(send())``) starts out
    with none of, unlike a normal page-request task.
    """
    with anchor:
        ui.run_javascript(
            "window.scrollTo(0, document.body.scrollHeight);"
            "document.querySelectorAll('.q-page-container').forEach("
            "el => el.scrollTop = el.scrollHeight);"
        )


def _is_attached(element: ui.element) -> bool:
    """Return whether an element is still present in its parent slot chain."""
    current: ui.element | None = element
    try:
        while current is not None and current.parent_slot is not None:
            parent_slot = current.parent_slot
            if current not in parent_slot.children:
                return False
            current = parent_slot.parent
    except (AttributeError, ValueError, RuntimeError):
        # A teardown/re-render may have already GC'd a parent slot (whose
        # weakref reads as "deleted") or the element's own slot -- that is
        # precisely the "detached" state this guard exists to detect.
        return False
    return True


def _delete_if_attached(element: ui.element) -> None:
    """Delete a live element unless a concurrent page teardown detached it."""
    try:
        if _is_attached(element):
            element.delete()
    except ValueError:
        pass


def _set_content_if_attached(element: ui.markdown, content: str) -> None:
    """Update live markdown unless a concurrent page teardown detached it."""
    try:
        if _is_attached(element):
            element.set_content(content)
    except ValueError:
        pass


def _oldest_running_call_id(
    tool_calls: list[dict], tool_status: dict[str, str], tool_name: str | None
) -> str | None:
    """Best-effort match for a ``tool.progress``/``tool.completed``/
    ``tool.failed`` event that carries no ``tool_call_id`` of its own -- the
    daemon's session chat-stream endpoint never sends one on these events
    today. Prefers the oldest still-``running`` call with a matching tool
    name over "last started" (which breaks the moment two tool calls are in
    flight at once and finish out of start order -- the tool name at least
    disambiguates the common case of two *different* tools running
    concurrently). Genuinely ambiguous only when the same tool name is
    running twice concurrently and they finish out of order, which needs a
    daemon-side fix (the id-carrying callback exists, just isn't wired into
    this endpoint) to resolve for good.
    """
    running = [c for c in tool_calls if tool_status.get(c.get("id")) == "running"]
    if tool_name:
        named = [c for c in running if c.get("function", {}).get("name") == tool_name]
        if named:
            return named[0].get("id")
    return running[0].get("id") if running else None


def _chat_bubble(
    icon: str,
    color: str,
    sender: str,
    content: str,
    timestamp: float | None,
    *,
    default_open: bool = False,
    active: bool = False,
) -> ui.markdown | None:
    """A visible chat message -- something the user or Hermes actually said.

    Collapsed by default, same as the mechanical event rows below (see
    :func:`_collapsible_entry`): on a long transcript, most bubbles are
    scrollback nobody's currently reading, and building their markdown/DOM
    eagerly is wasted work at that scale. Two exceptions stay open:
    ``default_open`` (used by :func:`render_transcript_events` for the
    transcript's last message, so re-opening a session doesn't start on a
    wall of collapsed rows) and the message actively streaming in right now
    (``active``), which also renders its body eagerly and returns the
    ``ui.markdown`` handle so streamed reply deltas can update it in place.
    """
    summary = f"{sender}: {oneline(content, limit=80)}" if content else sender
    md_holder: list[ui.markdown] = []

    def _body() -> None:
        with ui.card().props("flat bordered").classes("w-fit max-w-full q-mt-xs q-mb-md"):
            md_holder.append(ui.markdown(content))

    _collapsible_entry(
        icon,
        color,
        summary,
        timestamp,
        _body,
        default_open=active or default_open,
        mark="live-reply" if active else None,
    )
    return md_holder[0] if md_holder else None


def _step(
    icon: str,
    color: str,
    text: str,
    timestamp: float | None,
    *,
    mono: bool = False,
    active: bool = False,
) -> ui.markdown | None:
    """One compact row for a mechanical event (not something said).

    Text that already fits on one line *is* the header, nothing to expand.
    Longer text collapses to its first line as the header, with the rest
    revealed on click: prose (reasoning) as markdown, structured data (a
    tool result) as YAML in a code viewer -- see :func:`pretty_yaml`.
    """
    summary = oneline(text)
    if not active and summary == text.strip():
        _collapsible_entry(icon, color, text, timestamp, None)
        return None
    md_holder: list[ui.markdown] = []

    def _body() -> None:
        if mono:
            ui.code(pretty_yaml(text), language="yaml").classes("w-full")
        else:
            md_holder.append(ui.markdown(text))

    _collapsible_entry(
        icon,
        color,
        summary,
        timestamp,
        _body,
        default_open=active,
        mark="live-reasoning" if active else None,
    )
    return md_holder[0] if md_holder else None


def _tool_round_trip(
    name: str,
    args: str | None,
    result: Message | None,
    timestamp: float | None,
    *,
    status: str | None = None,
    active: bool = False,
) -> LiveToolRow | None:
    """One entry for a tool call *and* its own result, not two.

    A "Called X" row followed immediately by an "X result" row says the tool
    name twice for what a reader experiences as a single step. The tool name
    plus a short preview of its arguments is the row's always-visible label;
    the (often much longer) result -- both usually JSON on the wire -- sits
    behind the same toggle, re-rendered as YAML (see :func:`pretty_yaml`)
    so multi-line values inside it read as actual lines, not `\n` escapes.

    ``status``/``active`` are only set by the live render path: a running or
    just-finished tool renders the same entry with a status glyph beside its
    label (via :func:`_collapsible_entry`) instead of the collapsible body --
    the tool's result isn't in the stream, only in the reconciled
    ``run.completed`` transcript -- and returns the :class:`LiveToolRow`
    controls so the stream can finalize the glyph in place.
    """
    label = f"{name}: {oneline(args, limit=60)}" if args else name

    def _body() -> None:
        if args:
            ui.code(pretty_yaml(args), language="yaml").classes("w-full")
        if result and result.content:
            if args:
                ui.separator()
            ui.code(pretty_yaml(result.content), language="yaml").classes("w-full")

    has_body = bool(args) or bool(result and result.content)
    return _collapsible_entry(
        "construction",
        "grey",
        label,
        timestamp,
        _body if has_body else None,
        mark="live-tool" if active else None,
        status=status,
    )


_NOT_GIVEN: Any = object()


def render_transcript_events(
    messages: list[Message],
    *,
    active_id: int | None = None,
    tool_status: dict[str, str] | None = None,
    last_bubble_id: int | None = _NOT_GIVEN,
    queued_ids: set[int] | None = None,
) -> dict[int, LiveEntry]:
    """Render one contiguous slice of the transcript as a true, chronological
    event timeline. Every entry -- chat bubbles included -- collapses to a
    one-line summary by default (see :func:`_chat_bubble`/
    :func:`_collapsible_entry`), except the message named by
    ``last_bubble_id``, which opens automatically so landing on a session (or
    on a turn still streaming) shows what was last said instead of a wall of
    collapsed rows. A tool call and its own result (matched by
    ``tool_call_id``, not just assumed to be the next message) render as one
    fused entry -- see :func:`_tool_round_trip`.

    The detail page calls this twice per render: once for the static
    historical prefix (``active_id=None``), once for the live turn's own
    tail (``messages`` is just the pending message plus anything queued
    after it, ``active_id`` set) -- see ``_render_history``/``_render_live``.
    Because a single call only ever sees *part* of the full buffer, the
    "most recently said" message can't be derived from ``messages`` alone in
    that split-call case; callers passing a slice must pass ``last_bubble_id``
    explicitly (computed from the *whole* buffer). Left unset, it's derived
    from ``messages`` as before -- direct/test callers passing the full
    transcript in one call need not care about the split.

    ``active_id`` marks the message being streamed right now (the pending
    assistant message). Only that message renders eagerly and open and is
    given a live-update handle in the returned dict (keyed by message id), so
    the stream code can update its reasoning/reply markdown and tool status
    glyphs in place; every other message renders exactly as it would
    historically. ``active_id=None`` renders a pure historical transcript and
    returns an empty dict.

    ``tool_status`` maps a ``tool_call_id`` to ``"running"``/``"done"``/
    ``"failed"`` and makes that tool row render a small spinner or a
    check/error icon beside its label (the same ``_tool_round_trip`` entry
    type as a finished call -- just with a live status glyph). ``None`` means
    no glyph anywhere (pure historical).

    ``queued_ids`` marks user messages sent while another turn was still
    streaming: each gets a small "Queued -- will send after this turn
    completes" row under its bubble, dropped once the id is no longer in the
    set (i.e. once that turn actually starts).

    ``msg.compacted`` (soft-archived by the daemon's in-place context
    compaction, ``state.db``'s ``messages.compacted``) still renders like any
    other historical message -- compaction preserves them on disk precisely
    so nothing vanishes from view -- but the first non-compacted message
    following a run of them gets a "Context compressed" divider row ahead of
    it, so the transcript shows *where* the daemon summarized old turns away
    instead of the boundary being invisible.
    """
    results_by_call_id = {
        m.tool_call_id: m for m in messages if m.role == "tool" and m.tool_call_id
    }
    consumed_result_ids = {m.id for m in results_by_call_id.values()}
    if last_bubble_id is _NOT_GIVEN:
        last_bubble_id = next(
            (
                m.id
                for m in reversed(messages)
                if m.role == "user" or (m.role == "assistant" and m.content)
            ),
            None,
        )
    statuses = tool_status or {}
    handles: dict[int, LiveEntry] = {}
    archived_run = 0

    for msg in messages:
        if archived_run and not msg.compacted:
            _step(
                "compress",
                "purple",
                f"Context compressed — {archived_run} earlier "
                f"message{'s' if archived_run != 1 else ''} archived",
                msg.timestamp,
            )
        archived_run = archived_run + 1 if msg.compacted else 0

        is_active = active_id is not None and msg.id == active_id
        if msg.role == "user":
            _chat_bubble(
                "person",
                "primary",
                "You",
                msg.content or "",
                msg.timestamp,
                default_open=msg.id == last_bubble_id,
            )
            if queued_ids and msg.id in queued_ids:
                with ui.row().classes("items-center gap-2 text-xs opacity-60 q-mb-sm"):
                    ui.icon("schedule")
                    ui.label("Queued — will send after this turn completes")

        elif msg.role == "assistant":
            reasoning_md = None
            if msg.reasoning:
                reasoning_md = _step(
                    "psychology", "amber", msg.reasoning, msg.timestamp, active=is_active
                )
            tool_rows: dict[str, LiveToolRow] = {}
            for call in msg.tool_calls or []:
                fn = call.get("function", {})
                call_id = call.get("id")
                result = results_by_call_id.get(call_id) if call_id else None
                row = _tool_round_trip(
                    fn.get("name", "tool"),
                    fn.get("arguments"),
                    result,
                    msg.timestamp,
                    status=statuses.get(call_id) if call_id else None,
                    active=is_active,
                )
                if is_active and call_id and row is not None:
                    tool_rows[call_id] = row
            reply_md = None
            if msg.content:
                reply_md = _chat_bubble(
                    "smart_toy",
                    "secondary",
                    "Hermes",
                    msg.content,
                    msg.timestamp,
                    default_open=msg.id == last_bubble_id,
                    active=is_active,
                )
            if is_active:
                handles[msg.id] = LiveEntry(
                    reasoning_md=reasoning_md,
                    reply_md=reply_md,
                    tool_rows=tool_rows,
                )

        elif msg.id not in consumed_result_ids:
            # An orphaned tool result: its call fell outside this page of
            # messages, so there was nothing to fuse it with above.
            _step("output", "teal", msg.content or "(empty)", msg.timestamp, mono=True)

    return handles


# -- pages --------------------------------------------------------------


def register_pages(plugin: Plugin) -> None:
    logger = plugin.logger
    client = plugin.client  # gateway client -- only used to send chat turns
    settings = plugin.settings
    # Attachments are saved under this local directory (the daemon runs on
    # the same box, so the path is readable by the agent's own file tools);
    # ``chat_uploads_dir_path`` defaults to ``<hermes_home>/uploads``.
    uploads_root = settings.chat_uploads_dir_path
    max_upload_bytes = settings.chat_upload_max_bytes

    def _mark_read(session_id: str) -> None:
        _unread_state()[session_id] = datetime.now().timestamp()
        _persist_read_state()

    def _run_in_client(coro_factory: Callable[[], Any]) -> None:
        """Schedule an async handler as a background task, but re-enter the
        calling client's slot context inside that task first.

        ``ui.notify``/``ui.navigate.to`` resolve the current *client* from
        the calling asyncio task's own slot stack (see ``nicegui.context``).
        A plain ``background_tasks.create(coro)`` spawns a genuinely new task
        with an empty stack, so both silently no-op there (the resulting
        ``RuntimeError`` is swallowed by the task's own exception handler) --
        this is why "click Fork/Delete/New session" appeared to do the
        network call but never notified or navigated. Capturing the client
        here, while still in the click handler's own task (which does have a
        stack), and re-entering it with ``with page_client:`` inside the
        task mirrors what NiceGUI itself does for a plain ``async def``
        event handler (see ``events.handle_event``'s
        ``_await_and_handle_in_context``).
        """
        page_client = ui.context.client
        coro = coro_factory()

        async def run() -> None:
            with page_client:
                await coro

        background_tasks.create(run())

    def _rename(session_id: str, new_title: str, on_renamed: Callable[[], Any] | None) -> None:
        async def do_rename() -> None:
            if not new_title.strip():
                return
            try:
                await web.current_store().sessions.rename(session_id, new_title.strip())
            except HermesError as exc:
                ui.notify(f"Rename failed: {exc}", type="negative")
                return
            ui.notify("Session renamed", type="positive")
            if on_renamed:
                on_renamed()

        _run_in_client(do_rename)

    def _delete(session_id: str, on_deleted: Callable[[], Any] | None) -> None:
        async def do_delete() -> None:
            try:
                await web.current_store().sessions.delete(session_id)
            except HermesError as exc:
                ui.notify(f"Delete failed: {exc}", type="negative")
                return
            ui.notify("Session deleted", type="positive")
            _unread_state().pop(session_id, None)
            _persist_read_state()
            if on_deleted:
                on_deleted()

        _run_in_client(do_delete)

    def _new_session() -> None:
        async def do_create() -> None:
            try:
                session = await client.create_session()
            except HermesError as exc:
                ui.notify(f"Failed to start session: {exc}", type="negative")
                return
            ui.navigate.to(f"/sessions/{session.id}")

        _run_in_client(do_create)

    def _stop_session_list(session_id: str) -> None:
        async def do_stop() -> None:
            try:
                await client.stop_session(session_id)
            except HermesError as exc:
                ui.notify(f"Stop request failed: {exc}", type="warning")
                return
            ui.notify("Session stopped", type="info")
            # Re-render to drop the spinner now that the session is ended
            render_rows()

        _run_in_client(do_stop)

    @ui.page("/sessions", title="Sessions")
    async def sessions_page() -> None:
        _hydrate_read_state()
        with frame(active="/sessions"):

            def _mark_all_read() -> None:
                _unread_state()[ALL_READ_KEY] = datetime.now().timestamp()
                _persist_read_state()
                render_rows()
                ui.notify("Marked all sessions as read", type="positive")

            with ui.row().classes("w-full items-center gap-2"):
                search = (
                    ui.input(placeholder="Search")
                    .props("outlined dense clearable")
                    .classes("flex-grow")
                    .on("change", lambda: _search_or_source_changed())
                )
                source_filter = (
                    ui.select(SOURCES, value="")
                    .props("outlined dense options-dense")
                    .on("update:model-value", lambda: _search_or_source_changed())
                )
                unread_only = ui.switch("Unread").on("update:model-value", lambda: render_rows())
                ui.button(icon="done_all", on_click=_mark_all_read).props("flat round dense").mark(
                    "mark-all-read-button"
                ).tooltip("Mark all sessions as read")
                ui.button(
                    icon="refresh", on_click=lambda: background_tasks.create(_refresh())
                ).props("flat round dense").mark("refresh-button").tooltip("Refresh")
                ui.button("New session", icon="add", on_click=_new_session).props(
                    "unelevated"
                ).mark("new-session-button")

            list_container = ui.list().props("separator").classes("w-full")

            current_sessions: list[Session] = []
            total = 0
            pager = Pager(limit=20)

            def render_preview_row(s: Session, *, unread: bool) -> None:
                def on_deleted() -> None:
                    """Refresh rows after successful delete."""
                    render_rows()

                def _confirm_delete() -> None:
                    with ui.dialog() as dialog, ui.card().classes("w-full max-w-xs"):
                        ui.label(f"Delete session '{s.title or s.id}'?").classes(
                            "text-lg font-bold"
                        )
                        ui.label("This action cannot be undone.").classes("text-sm opacity-70")
                        with ui.row().classes("w-full justify-end gap-2"):
                            ui.button("Cancel", on_click=dialog.close).props("flat").mark(
                                "cancel-delete-button"
                            )
                            ui.button(
                                "Delete",
                                icon="delete",
                                color="negative",
                                on_click=lambda: _delete(s.id, on_deleted) or dialog.close(),
                            ).mark("confirm-delete-button")

                    dialog.open()

                with (
                    ui.item(on_click=partial(ui.navigate.to, f"/sessions/{s.id}"))
                    .props("v-ripple")
                    .mark("session-row")
                ):
                    with ui.item_section().props("avatar"):
                        ui.icon(source_icon(s.source))
                    with ui.item_section():
                        ui.item_label(s.title or "(untitled)").classes(
                            "font-bold" if unread else ""
                        )
                        ui.item_label(s.preview or "No preview").props("caption lines=1")
                    with ui.item_section().props("side top"):
                        if is_running(s):
                            ui.spinner(size="sm", color="positive").mark("session-running")
                            ui.button(
                                icon="stop",
                                color="negative",
                                on_click=partial(_stop_session_list, s.id),
                            ).props("flat dense size=sm").mark("session-list-stop").tooltip(
                                "Stop running session"
                            )
                        ui.label(fmt_age(s.last_active)).classes("text-xs opacity-60")
                        if unread:
                            ui.icon("circle", size="8px", color="primary")
                    with ui.item_section().props("side"):
                        # `click.stop` (Vue's `.stop` modifier) keeps this
                        # from also triggering the row's own `on_click`
                        # navigation — same pattern as the files plugin.
                        ui.button(icon="delete", color="negative").props(
                            "flat dense size=sm"
                        ).on(
                            "click.stop",
                            _confirm_delete,
                        ).mark("delete-session-button").tooltip(f"Delete '{s.title or s.id}'")

            def render_rows() -> None:
                """Render the current page's already-fetched sessions.

                "Unread" is per-browser state (``app.storage.user``), not
                something the daemon's own `state.db` knows about, so unlike
                search/source it can't be pushed into the SQL query -- it
                narrows the current page's rows client-side instead, without
                re-fetching or changing what the pager's own page count is
                based on.
                """
                state = _unread_state()
                rows = current_sessions
                if unread_only.value:
                    rows = [s for s in rows if is_unread(s, state)]
                list_container.clear()
                with list_container:
                    if not rows:
                        ui.label("No sessions.").classes("text-sm opacity-60 q-pa-sm")
                    for s in rows:
                        render_preview_row(s, unread=is_unread(s, state))
                    render_pager(pager, total, lambda: background_tasks.create(fetch()))

            async def fetch() -> None:
                nonlocal total
                list_container.clear()
                with list_container, ui.item():
                    ui.spinner(size="lg")
                try:
                    current_sessions[:], total = await web.current_store().sessions.list(
                        source=source_filter.value or None,
                        search=(search.value or "").strip() or None,
                        limit=pager.limit,
                        offset=pager.offset,
                    )
                except HermesError as exc:
                    list_container.clear()
                    ui.notify(f"Failed to load sessions: {exc}", type="negative")
                    return
                render_rows()

            def _search_or_source_changed() -> None:
                pager.reset()
                background_tasks.create(fetch())

            async def _refresh() -> None:
                pager.reset()
                await fetch()

            # NiceGUI aborts the whole page-build coroutine with a 500 if it
            # doesn't finish within `response_timeout` (default 3s -- the
            # exact number this page used to blow past). `client.connected()`
            # tells NiceGUI to deliver the shell built so far *now* and keep
            # running this coroutine in the background against the socket
            # that connects moments later, instead of racing the real
            # `hermes` subprocess call against that deadline.
            await ui.context.client.connected()
            await fetch()
            logger.debug("sessions list rendered")

    @ui.page("/sessions/{session_id}", title="Session")
    async def session_detail_page(session_id: str) -> None:
        ui.add_css(_TIMELINE_CSS)
        ui.add_css(_UPLOADER_CSS)
        with frame(active="/sessions"):
            await ui.context.client.connected()
            store = web.current_store()
            try:
                session = await store.sessions.get_session(session_id)
                loaded_messages, has_earlier = await store.sessions.get_messages_page(session_id)
            except HermesError as exc:
                ui.label(f"Failed to load session: {exc}")
                return
            _mark_read(session_id)

            def _stats_text() -> str:
                return (
                    f"ID {session.id} · {session.message_count} messages · "
                    f"{session.tool_call_count} tool calls · "
                    f"cost {fmt_cost(session.estimated_cost_usd)} · "
                    f"{fmt_tokens(session.input_tokens)} in / "
                    f"{fmt_tokens(session.output_tokens)} out tokens"
                )

            def _update_running_indicator(active: bool, description: str | None = None) -> None:
                running_indicator.set_visibility(active)
                if active:
                    desc = description or "Working…"
                    running_desc.set_text(oneline(desc, limit=48))
                    running_tooltip.set_text(desc)

            with ui.card():
                with ui.row():
                    ui.label(session.title or session.id)
                    ui.space()
                    ui.badge(session.source or "unknown", color="grey")
                    ui.badge(session.model or "no model", color="primary")
                    with (
                        ui.row().classes("items-center gap-1").mark("session-running")
                    ) as running_indicator:
                        ui.spinner(size="1em", color="positive")
                        ui.label("Running").classes("text-xs font-bold text-positive")
                        running_desc = ui.label("").classes("text-xs opacity-80")
                        with running_desc:
                            running_tooltip = ui.tooltip("")
                    running_indicator.set_visibility(False)
                stats_label = ui.label(_stats_text())
                with ui.row():
                    ui.input(
                        "Rename",
                        placeholder=session.title or "",
                        on_change=lambda e: _rename(session_id, e.value or "", None),
                    ).props("outlined")
                    ui.button(
                        "Delete",
                        icon="delete",
                        color="negative",
                        on_click=partial(_delete, session_id, lambda: ui.navigate.to("/sessions")),
                    )
            _update_running_indicator(is_running(session), session.last_activity_description)

            # Only the latest page of messages is ever fetched/rendered up
            # front -- a session can run to thousands of messages, and
            # mounting a NiceGUI/Quasar element per message for all of them
            # is what actually made huge transcripts slow, not the SQL read.
            # `earlier_row` (a "Load earlier" button, or nothing once there's
            # no more history) sits above `transcript` and never itself gets
            # cleared by a transcript rebuild, so its position on the page is
            # stable across "Load earlier" clicks -- see `_load_earlier`.
            earlier_row = ui.row().classes("w-full justify-center")
            earlier_button: ui.button | None = None
            transcript = ui.column().classes("w-full")
            timeline: ui.timeline | None = None
            loading_more = False

            # Live-turn control state. While a turn streams we keep the compose
            # box enabled and *queue* further messages so the user is never
            # blocked mid-think and nothing is silently dropped; the stop button
            # interrupts the running turn via the gateway's run-scoped stop
            # endpoint, plus a client-side stream abort (which the gateway also
            # treats as an interrupt).
            streaming = False
            stream_task: asyncio.Task | None = None
            current_run_id: str | None = None
            queued_turn: QueuedTurn | None = None
            # Live-streaming render state, owned by `_run_turn` and reset to
            # None/{}/0 the moment a turn ends, so every other render (initial
            # page, "Load earlier", a reconciled turn) is purely historical:
            # - `active_message_id` / `tool_status`: the id of the one
            #   assistant message being streamed right now, and its
            #   per-tool_call_id status map -- consumed by both render halves
            #   below.
            # - `pending_index`: this turn's position in `loaded_messages`.
            #   `loaded_messages[pending_index:]` is exactly "the pending
            #   message plus anything queued after it" -- see `_render_live`.
            #   Valid for the whole turn because nothing is ever inserted
            #   before it while streaming (`_load_earlier` is blocked, see
            #   `_sync_streaming_controls`).
            # - `live_entry_mark`: the DOM child index in `timeline` where the
            #   live region starts -- everything before it is history and is
            #   never touched by `_render_live`.
            active_message_id: int | None = None
            tool_status: dict[str, str] = {}
            pending_index = 0
            live_entry_mark = 0
            # Files picked in the composer, awaiting send: {"name", "path",
            # "size"}. Cleared once they're attached to a message (sent or
            # queued) or individually removed.
            pending_attachments: list[dict[str, Any]] = []

            def _current_last_bubble_id() -> int | None:
                """The most recent "something said" message across the whole
                buffer -- computed once from ``loaded_messages`` and threaded
                into both render halves below, since neither one alone sees
                the full buffer once history and the live tail render
                separately (see :func:`_render_history`/:func:`_render_live`)."""
                return next(
                    (
                        m.id
                        for m in reversed(loaded_messages)
                        if m.role == "user" or (m.role == "assistant" and m.content)
                    ),
                    None,
                )

            def _render_earlier_button() -> None:
                nonlocal earlier_button
                earlier_row.clear()
                earlier_button = None
                if not has_earlier:
                    return
                with earlier_row:
                    earlier_button = (
                        ui.button(
                            "Load earlier messages",
                            icon="expand_less",
                            on_click=lambda: _run_in_client(_load_earlier),
                        )
                        .props("flat dense")
                        .mark("load-earlier-button")
                    )
                    earlier_button.set_enabled(not streaming)

            def _render_history() -> None:
                """Rebuild everything *except* the message currently
                streaming (if any) -- called only when the historical set
                itself changes: initial load, "Load earlier", a turn's start
                (to show its own new user bubble) and a turn's
                ``run.completed`` reconciliation. Sets `live_entry_mark` so a
                subsequent :func:`_render_live` knows where its own region
                starts.
                """
                nonlocal timeline, live_entry_mark
                transcript.clear()
                visible = (
                    [m for m in loaded_messages if m.id != active_message_id]
                    if active_message_id is not None
                    else loaded_messages
                )
                if not visible:
                    with transcript:
                        ui.label("No messages yet.")
                    timeline = None
                    live_entry_mark = 0
                    return
                with transcript, ui.timeline(layout="dense").classes("w-full") as tl:
                    timeline = tl
                    render_transcript_events(
                        visible,
                        last_bubble_id=_current_last_bubble_id(),
                        queued_ids=set(queued_turn.local_ids) if queued_turn else None,
                    )
                live_entry_mark = len(timeline.default_slot.children)

            def _render_live() -> dict[int, LiveEntry]:
                """Rebuild just the in-flight turn's own rows -- the pending
                message plus anything queued after it (see `pending_index`)
                -- in place at the end of the (untouched) history timeline.
                Cheap regardless of transcript length: this only ever
                touches entries at or after `live_entry_mark`, never the
                historical prefix. Returns empty outside a live turn.
                """
                if timeline is None or active_message_id is None:
                    return {}
                stale = list(timeline.default_slot.children[live_entry_mark:])
                for entry in stale:
                    _delete_if_attached(entry)
                with timeline:
                    return render_transcript_events(
                        loaded_messages[pending_index:],
                        active_id=active_message_id,
                        tool_status=tool_status or None,
                        last_bubble_id=_current_last_bubble_id(),
                        queued_ids=set(queued_turn.local_ids) if queued_turn else None,
                    )

            _render_earlier_button()
            _render_history()
            _scroll_to_bottom(transcript)

            def _sync_streaming_controls() -> None:
                """Reflect the `streaming` flag on every control it gates --
                the stop button, and "Load earlier" (which must not run
                concurrently with a live turn's own DOM rebuild -- see
                `_render_live`'s history/live split, which assumes nothing
                is ever inserted before `pending_index`/`live_entry_mark`
                while a turn is in flight)."""
                nonlocal stop_button
                should_show = streaming or is_running(session)
                if should_show and stop_button is None:
                    # Re-create (the element may have been deleted previously)
                    with ui.row():
                        stop_button = (
                            ui.button(icon="stop", color="negative", on_click=_stop)
                            .props("round dense")
                            .mark("chat-stop")
                        )
                elif should_show and stop_button is not None:
                    # Already present and visible; ensure it's attached
                    if _is_attached(stop_button):
                        pass  # fine as-is
                    else:
                        # Was detached by a concurrent teardown; rebuild
                        with ui.row():
                            stop_button = (
                                ui.button(icon="stop", color="negative", on_click=_stop)
                                .props("round dense")
                                .mark("chat-stop")
                            )
                elif not should_show and stop_button is not None:
                    _delete_if_attached(stop_button)
                    stop_button = None
                if earlier_button is not None:
                    earlier_button.set_enabled(not streaming)

            async def _load_earlier() -> None:
                nonlocal has_earlier, loading_more
                if loading_more or not has_earlier or streaming:
                    return
                loading_more = True
                earlier_row.clear()
                with earlier_row:
                    ui.spinner(size="sm")
                try:
                    oldest_id = loaded_messages[0].id if loaded_messages else None
                    older, has_earlier = await store.sessions.get_messages_page(
                        session_id, before_id=oldest_id
                    )
                except HermesError as exc:
                    ui.notify(f"Failed to load earlier messages: {exc}", type="negative")
                else:
                    loaded_messages[:0] = older
                finally:
                    loading_more = False
                _render_earlier_button()
                _render_history()
                # Anchor the view on the load-more control itself (stable
                # across rebuilds -- see the comment above `earlier_row`)
                # instead of jumping to the bottom, so the newly-revealed
                # older messages land in view instead of scrolling past them.
                with earlier_row:
                    ui.run_javascript(f"getHtmlElement({earlier_row.id}).scrollIntoView()")

            local_id_seq = 0

            def _next_local_id() -> int:
                """A locally-sent turn's messages don't have a real `state.db`
                row id yet (the CLI/daemon assigns one on write, which this
                page doesn't re-read). Real ids are always positive
                (`INTEGER PRIMARY KEY`), so a page-scoped decreasing counter
                is a namespace disjoint from any real id -- unlike "one past
                the current max", it can't collide with ids `_load_earlier`
                prepends from a concurrent task, since it never looks at
                `loaded_messages` at all."""
                nonlocal local_id_seq
                local_id_seq -= 1
                return local_id_seq

            def _append_local_message(
                role: str, content: str, timestamp: float | None = None
            ) -> int:
                """Track a locally-rendered message so a later transcript
                rebuild ("Load earlier") doesn't clobber it. Returns its id
                -- callers key turn ownership off this, never off list
                position (see `QueuedTurn`/`_run_turn`)."""
                msg = Message(
                    id=_next_local_id(),
                    session_id=session_id,
                    role=role,
                    content=content,
                    timestamp=timestamp or datetime.now().timestamp(),
                )
                loaded_messages.append(msg)
                return msg.id

            def _queue_message(text: str) -> None:
                """Mid-turn send: show the message immediately, marked queued,
                so nothing the user typed is silently swallowed.

                Appends into ``loaded_messages`` and re-renders through
                :func:`_render_live` (the same path a running turn's own
                events use) rather than poking the DOM directly -- that
                keeps this message subject to the same
                `live_entry_mark`/`pending_index` bookkeeping as everything
                else in the live region, so a subsequent tool-call update
                during the *current* turn rebuilds its own rows without
                disturbing (or losing) this one.
                """
                nonlocal queued_turn
                sent_at = datetime.now().timestamp()
                new_id = _append_local_message("user", text, sent_at)
                if queued_turn is None:
                    queued_turn = QueuedTurn(text, [new_id])
                else:
                    merged_text = f"{queued_turn.text}\n{text}".strip()
                    queued_turn = QueuedTurn(merged_text, [*queued_turn.local_ids, new_id])
                _render_live()
                _scroll_to_bottom(transcript)

            async def _run_turn(text: str, *, own_local_ids: list[int] | None = None) -> None:
                """Stream one turn by folding SSE events into the buffer.

                Turn ownership is an *id set* (`turn_local_ids`), not a list
                position: either a fresh user message is appended now, or
                (for a turn started from `queued_turn`) `own_local_ids`
                already names messages `_queue_message` appended earlier.
                Either way, every id this turn owns -- including the pending
                assistant message, whose id can change mid-stream via
                `message.started` -- stays tracked in `turn_local_ids`, so
                `run.completed`'s reconciliation can swap out exactly this
                turn's own messages by identity, unaffected by whatever
                `_load_earlier` or a queued follow-up does to the rest of
                ``loaded_messages`` around it.

                High-frequency deltas (reasoning/reply token streams) patch
                the live-update handles in place; structural events (tool
                started/completed, a real message id, a queued message)
                rebuild only the live region via :func:`_render_live` --
                never the historical prefix, which only changes at this
                turn's start and its ``run.completed`` reconciliation (see
                :func:`_render_history`). The gateway's completed transcript
                from ``run.completed`` is preferred when available because
                tool results are not present in tool SSE events and the
                daemon is the source of truth.
                """
                nonlocal session, current_run_id, active_message_id, tool_status
                nonlocal pending_index
                current_run_id = None
                _sync_streaming_controls()
                _update_running_indicator(True)
                sent_at = datetime.now().timestamp()
                if own_local_ids:
                    turn_local_ids: set[int] = set(own_local_ids)
                else:
                    turn_local_ids = {_append_local_message("user", text, sent_at)}
                # A plain non-None list (Message.tool_calls is `list | None`);
                # `pending.tool_calls` is assigned this same object so the
                # buffer render sees every append.
                tool_calls: list[dict] = []
                pending = Message(
                    id=_next_local_id(),
                    session_id=session_id,
                    role="assistant",
                    content="",
                    tool_calls=tool_calls,
                    timestamp=sent_at,
                )
                turn_local_ids.add(pending.id)
                loaded_messages.append(pending)
                pending_index = len(loaded_messages) - 1
                active_message_id = pending.id
                tool_status = {}
                handles: dict[int, LiveEntry] = {}
                try:
                    # Show this turn's own new user bubble (or drop the
                    # "Queued" badge, if `own_local_ids` names an
                    # already-queued message) as history, then start the
                    # live region for `pending`.
                    _render_history()
                    handles = _render_live()
                    _scroll_to_bottom(transcript)
                    try:
                        async for event in client.stream_turn(
                            session_id, text, model=session.model
                        ):
                            if event.event == "run.started":
                                current_run_id = event.data.get("run_id") or current_run_id
                            elif event.event == "message.started":
                                # Adopt the daemon's id for the pending message
                                # (only when it's a real id, not a placeholder
                                # string) so the handle dict and any later
                                # reconcile stay keyed consistently. A *second*
                                # `message.started` within the same run means a
                                # genuinely new assistant message started (a
                                # multi-step tool loop -- reason, call a tool,
                                # reason again, call another, only then reply
                                # -- produces one message per step server-side,
                                # confirmed against a real `run.completed`
                                # transcript), not a rename of the one already
                                # streaming: finalize `pending` in place and
                                # start accumulating into a fresh message, so
                                # each step's own reasoning/content stays its
                                # own string instead of every step's text
                                # getting concatenated with no separator
                                # (confirmed live: a two-step turn rendered its
                                # first reply glued directly onto its final
                                # one, `"...memory.Got it — ..."`).
                                message = event.data.get("message")
                                mid = message.get("id") if isinstance(message, dict) else None
                                if isinstance(mid, int) and mid != pending.id:
                                    if pending.id > 0:
                                        tool_calls = []
                                        pending = Message(
                                            id=mid,
                                            session_id=session_id,
                                            role="assistant",
                                            content="",
                                            tool_calls=tool_calls,
                                            timestamp=datetime.now().timestamp(),
                                        )
                                        loaded_messages.append(pending)
                                    else:
                                        turn_local_ids.discard(pending.id)
                                        pending.id = mid
                                    turn_local_ids.add(pending.id)
                                    active_message_id = pending.id
                                    handles = _render_live()
                            elif event.event == "tool.progress":
                                name = event.data.get("tool_name", "tool")
                                delta = str(event.data.get("delta", ""))
                                if name == "_thinking":
                                    # The thinking stream IS the reasoning body
                                    # -- never a tool row.
                                    if delta:
                                        pending.reasoning = (pending.reasoning or "") + delta
                                    handle = handles.get(pending.id)
                                    if handle is None or handle.reasoning_md is None:
                                        handles = _render_live()
                                        handle = handles.get(pending.id)
                                    if handle is not None and handle.reasoning_md is not None:
                                        _set_content_if_attached(
                                            handle.reasoning_md, pending.reasoning or ""
                                        )
                                else:
                                    call_id = event.data.get("tool_call_id") or (
                                        _oldest_running_call_id(tool_calls, tool_status, name)
                                    )
                                    handle = handles.get(pending.id)
                                    row = (
                                        handle.tool_rows.get(call_id)
                                        if handle and call_id
                                        else None
                                    )
                                    if row is not None:
                                        row.label.set_text(oneline(delta, limit=60))
                                    _scroll_to_bottom(transcript)
                            elif event.event == "tool.started":
                                args = event.data.get("args") or event.data.get("preview")
                                call_id = event.data.get("tool_call_id") or (
                                    f"call_{len(tool_calls)}"
                                )
                                tool_calls.append(
                                    {
                                        "id": call_id,
                                        "type": "function",
                                        "function": {
                                            "name": str(event.data.get("tool_name", "tool")),
                                            "arguments": str(args) if args else "",
                                        },
                                    }
                                )
                                tool_status[call_id] = "running"
                                handles = _render_live()
                                _scroll_to_bottom(transcript)
                            elif event.event in {"tool.completed", "tool.failed"}:
                                call_id = event.data.get("tool_call_id") or _oldest_running_call_id(
                                    tool_calls, tool_status, event.data.get("tool_name")
                                )
                                if call_id:
                                    tool_status[call_id] = (
                                        "failed" if event.event == "tool.failed" else "done"
                                    )
                                    handles = _render_live()
                                _scroll_to_bottom(transcript)
                            elif event.event == "assistant.delta":
                                pending.content = (pending.content or "") + event.data.get(
                                    "delta", ""
                                )
                                handle = handles.get(pending.id)
                                if handle is None or handle.reply_md is None:
                                    handles = _render_live()
                                    handle = handles.get(pending.id)
                                if handle is not None and handle.reply_md is not None:
                                    _set_content_if_attached(handle.reply_md, pending.content or "")
                                _scroll_to_bottom(transcript)
                            elif event.event == "assistant.completed":
                                pending.content = event.data.get("content", pending.content or "")
                                handle = handles.get(pending.id)
                                if handle is None or handle.reply_md is None:
                                    handles = _render_live()
                                    handle = handles.get(pending.id)
                                if handle is not None and handle.reply_md is not None:
                                    _set_content_if_attached(handle.reply_md, pending.content or "")
                                _scroll_to_bottom(transcript)
                            elif event.event == "run.completed":
                                # Some gateway versions omit the terminal
                                # tool.completed event when the run ends; the
                                # run boundary still gives the live row a
                                # definitive successful completion state.
                                finalized = False
                                for call in tool_calls:
                                    call_id = call.get("id")
                                    if call_id and tool_status.get(call_id) == "running":
                                        tool_status[call_id] = "done"
                                        finalized = True
                                messages = event.data.get("messages")
                                if isinstance(messages, list) and messages:
                                    # Replace exactly this turn's own messages,
                                    # by id -- not a list-index range -- so a
                                    # concurrent `_load_earlier` prepend or a
                                    # queued follow-up appended after this
                                    # turn can never shift what gets removed.
                                    positions = [
                                        i
                                        for i, m in enumerate(loaded_messages)
                                        if m.id in turn_local_ids
                                    ]
                                    insert_at = positions[0] if positions else len(loaded_messages)
                                    loaded_messages[:] = [
                                        m for m in loaded_messages if m.id not in turn_local_ids
                                    ]
                                    reconciled: list[Message] = []
                                    for message in messages:
                                        # The gateway's `run.completed` payload
                                        # has been observed omitting `id`
                                        # entirely on some entries (confirmed
                                        # live: `_message_response` only
                                        # includes a key when it's already
                                        # present on the source message,
                                        # dropping `id` rather than sending it
                                        # `null`) -- `Message.from_json` needs
                                        # it, so a single malformed entry must
                                        # not take down this whole
                                        # reconciliation (and, uncaught, the
                                        # entire live-rendering coroutine)
                                        # along with every *valid* entry
                                        # after it in the same payload.
                                        if not isinstance(message, dict) or "id" not in message:
                                            logger.warning(
                                                "run.completed: dropping malformed message "
                                                "(missing 'id'): {}",
                                                message,
                                            )
                                            continue
                                        reconciled.append(Message.from_json(message))
                                    loaded_messages[insert_at:insert_at] = reconciled
                                    active_message_id = None
                                    tool_status = {}
                                    _render_history()
                                    _scroll_to_bottom(transcript)
                                elif finalized:
                                    handles = _render_live()
                                    _scroll_to_bottom(transcript)
                    except HermesError as exc:
                        ui.notify(f"Message failed: {exc}", type="negative")
                finally:
                    current_run_id = None
                    active_message_id = None
                    tool_status = {}
                    _sync_streaming_controls()
                    message_input.run_method("focus")

            async def send() -> None:
                nonlocal session, streaming, stream_task, queued_turn
                text = (message_input.value or "").strip()
                if not text and not pending_attachments:
                    return
                message_input.set_value("")
                if pending_attachments:
                    block = attachment_block([att["path"] for att in pending_attachments])
                    text = "\n\n".join(part for part in (text, block) if part)
                    pending_attachments.clear()
                    _render_attachments()
                if streaming:
                    # A turn is already running: hold the message and deliver
                    # it as the next turn instead of firing a second concurrent
                    # agent run against the same session.
                    _queue_message(text)
                    return
                streaming = True
                stream_task = asyncio.current_task()
                try:
                    own_ids: list[int] | None = None
                    while True:
                        await _run_turn(text, own_local_ids=own_ids)
                        if queued_turn is None:
                            break
                        text, own_ids = queued_turn.text, queued_turn.local_ids
                        queued_turn = None
                finally:
                    streaming = False
                    stream_task = None
                # Turn(s) finished (or were stopped) — refresh the header stats.
                try:
                    session = await store.sessions.get_session(session_id)
                except HermesError:
                    pass
                else:
                    stats_label.set_text(_stats_text())
                    _update_running_indicator(
                        is_running(session), session.last_activity_description
                    )
                    _sync_streaming_controls()

            async def _do_stop() -> None:
                nonlocal queued_turn, stream_task, session
                if streaming:
                    run_id = current_run_id
                    if run_id:
                        try:
                            await client.stop_run(run_id)
                        except HermesError as exc:
                            ui.notify(f"Stop request failed: {exc}", type="warning")
                    # Return anything queued to the box so it is never swallowed.
                    if queued_turn is not None:
                        message_input.set_value(queued_turn.text)
                        queued_turn = None
                    # Abort the local stream. The gateway also treats an SSE client
                    # disconnect as an interrupt, so this covers the brief window
                    # before run.started delivered a run_id.
                    task = stream_task
                    if task is not None and not task.done():
                        task.cancel()
                    ui.notify("Stopped", type="info")
                elif is_running(session):
                    try:
                        await client.stop_session(session_id)
                    except HermesError as exc:
                        ui.notify(f"Stop request failed: {exc}", type="warning")
                        return
                    ui.notify("Stopped", type="info")
                    await _refresh_session_state()
                else:
                    return

            def _stop() -> None:
                _run_in_client(_do_stop)

            def _fmt_size(num_bytes: int) -> str:
                if num_bytes >= 1024 * 1024:
                    return f"{num_bytes / (1024 * 1024):.1f} MiB"
                if num_bytes >= 1024:
                    return f"{num_bytes / 1024:.0f} KiB"
                return f"{num_bytes} B"

            def _render_attachments() -> None:
                """Rebuild the pending-attachment chips above the compose box."""
                attachments_row.clear()
                if not pending_attachments:
                    attachments_row.set_visibility(False)
                    return
                attachments_row.set_visibility(True)
                with attachments_row:
                    for index, att in enumerate(pending_attachments):
                        with (
                            ui.row()
                            .classes("items-center gap-1 q-px-sm q-py-xs bg-grey-3 rounded-borders")
                            .mark("chat-attachment")
                        ):
                            ui.icon("attach_file", size="sm")
                            ui.label(att["name"]).classes("text-xs")
                            ui.label(_fmt_size(att["size"])).classes("text-xs opacity-60")
                            ui.button(
                                icon="close",
                                on_click=partial(_remove_attachment, index),
                            ).props("flat round dense size=sm").mark("chat-attachment-remove")

            def _remove_attachment(index: int) -> None:
                """Drop a pending attachment (and its file on disk -- it was
                never sent, so leaving it would just litter the uploads dir)."""
                removed = pending_attachments.pop(index)
                try:
                    Path(removed["path"]).unlink(missing_ok=True)
                except OSError:
                    logger.warning("failed to delete discarded chat attachment {}", removed["path"])
                _render_attachments()

            async def _on_upload(event: UploadEventArguments) -> None:
                """Save one picked file under the uploads dir and chip it.

                The uploader's own ``max_file_size`` prop already rejects
                oversize files client-side; the check here is the server-side
                backstop (the test suite drives ``handle_uploads`` directly,
                bypassing the client).
                """
                file = event.file
                if file.size() > max_upload_bytes:
                    ui.notify(
                        f"{file.name}: too large ({_fmt_size(file.size())} "
                        f"> {_fmt_size(max_upload_bytes)})",
                        type="negative",
                    )
                    return
                if len(pending_attachments) >= _MAX_CHAT_ATTACHMENTS:
                    ui.notify(
                        f"Too many attachments (max {_MAX_CHAT_ATTACHMENTS})",
                        type="negative",
                    )
                    return
                try:
                    target = upload_target(uploads_root, session_id, file.name)
                    await file.save(target)
                except OSError as exc:
                    logger.warning("failed to save chat attachment {}: {}", file.name, exc)
                    ui.notify(f"Failed to save '{file.name}': {exc}", type="negative")
                    return
                pending_attachments.append(
                    {"name": file.name, "path": str(target), "size": file.size()}
                )
                _render_attachments()

            with ui.card().classes("w-full sticky bottom-0 z-10"):
                attachments_row = ui.row().classes("w-full items-center gap-2 flex-wrap")
                attachments_row.set_visibility(False)
                with ui.row().classes("w-full items-center gap-2"):
                    ui.upload(
                        multiple=True,
                        auto_upload=True,
                        max_file_size=max_upload_bytes,
                        max_files=_MAX_CHAT_ATTACHMENTS,
                        on_upload=_on_upload,
                        on_rejected=lambda: ui.notify(
                            f"Attachment rejected (limit {_fmt_size(max_upload_bytes)})",
                            type="warning",
                        ),
                    ).props("flat dense square").classes("chat-uploader").mark("chat-upload")
                    message_input = (
                        ui.input(placeholder="Message Hermes…")
                        .props("outlined dense autofocus")
                        .classes("flex-grow")
                        .on("keydown.enter", lambda: _run_in_client(send))
                        .mark("chat-input")
                    )
                    stop_button = (
                        ui.button(icon="stop", color="negative", on_click=_stop)
                        .props("round dense")
                        .mark("chat-stop")
                    )

                    _sync_streaming_controls()
                    ui.button(icon="send", on_click=lambda: _run_in_client(send)).props(
                        "round dense"
                    ).mark("chat-send")

            async def _refresh_session_state() -> None:
                """Poll: keep the running indicator + header stats live while the agent
                works in this session from any surface, not just turns started here."""
                nonlocal session
                try:
                    fresh = await store.sessions.get_session(session_id)
                except HermesError:
                    return
                session = fresh
                _update_running_indicator(is_running(session), session.last_activity_description)
                _sync_streaming_controls()
                stats_label.set_text(_stats_text())

            ui.timer(5.0, lambda: _run_in_client(_refresh_session_state))
            logger.debug("session detail rendered for {}", session_id)
