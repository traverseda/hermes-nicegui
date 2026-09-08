"""Pure helpers for the sessions plugin: no NiceGUI, no gateway.

Formatting/filtering/attachment-path rules live here so they can be
unit-tested directly (see ``tests/test_unread.py`` and
``tests/test_session_upload_logic.py``) without going through the NiceGUI
``user`` test harness. Anything that touches ``app.storage`` or the gateway
belongs in ``ui.py`` instead.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import yaml

from hermes_nicegui.gateway import Session


class _BlockStringDumper(yaml.SafeDumper):
    """A YAML dumper that renders multi-line strings as ``|`` block scalars.

    PyYAML's default style re-escapes embedded newlines as literal ``\\n``
    sequences -- the same unreadable form JSON is stuck with, since it never
    picks block style on its own. This is the standard representer override
    to make it do so, which is the entire reason for using YAML here.
    """


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_BlockStringDumper.add_representer(str, _represent_str)


def _strip_trailing_whitespace(value: object) -> object:
    """Recursively rstrip each line of every string in a JSON-shaped value.

    PyYAML's literal block style (``|``) silently refuses to trigger if
    *any* line in the string has trailing whitespace -- common in real
    command output and markdown -- and falls back to an unreadable
    double-quoted, line-wrapped scalar instead. This is a display-only
    transform (it doesn't touch the underlying message data) that makes
    block style actually usable for realistic content.
    """
    if isinstance(value, str):
        return "\n".join(line.rstrip() for line in value.split("\n"))
    if isinstance(value, list):
        return [_strip_trailing_whitespace(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_trailing_whitespace(v) for k, v in value.items()}
    return value


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def fmt_age(ts: float | None) -> str:
    if not ts:
        return ""
    delta = datetime.now().timestamp() - ts
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    return datetime.fromtimestamp(ts).strftime("%b %d")


def fmt_cost(cost: float | None) -> str:
    if cost is None:
        return ""
    return f"${cost:.4f}"


def fmt_tokens(count: int) -> str:
    """Compact token count, e.g. ``12.3k`` -- session totals can run into
    the hundreds of thousands, where a raw digit count is hard to scan."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


#: Reserved key in the per-browser ``seen`` map: a timestamp that means
#: "everything active before this is read", regardless of per-session entries.
ALL_READ_KEY = "__all__"


def is_unread(session: Session, seen: dict[str, float]) -> bool:
    """A session is unread if it has activity newer than the last time it was seen.

    ``seen`` maps session ids to their last-seen timestamps; the reserved
    ``ALL_READ_KEY`` (``"__all__"``) entry, when present, also marks anything
    active before its timestamp as read -- whichever of the two thresholds is
    higher wins.
    """
    if not session.message_count or not session.last_active:
        return False
    last_seen = max(seen.get(session.id, 0.0), seen.get(ALL_READ_KEY, 0.0))
    return session.last_active > last_seen


def is_running(session: Session) -> bool:
    """Whether an agent is actively working in the session right now.

    The daemon writes ``last_activity_description`` (rate-limited) during an
    in-flight turn and clears it in the turn's ``finally`` (hermes
    ``StateStore.clear_session_activity_labels``), so a non-empty label on an
    un-ended session is the durable signal of live work from any surface --
    webui chat, cron, API, kanban worker. An ended session never counts.
    """
    return session.ended_at is None and bool(session.last_activity_description)


_SOURCE_ICONS = {
    "webui": "language",
    "api_server": "api",
    "cron": "schedule",
    "kanban": "view_kanban",
    "cli": "terminal",
    "telegram": "send",
    "discord": "forum",
    "slack": "chat",
    "desktop": "desktop_windows",
    "dashboard": "dashboard",
    "hermes_browser": "public",
    "browser": "public",
}


def source_icon(source: str | None) -> str:
    """Material icon for all gateway-normalized session sources."""
    return _SOURCE_ICONS.get(source or "", "help_outline")


def oneline(text: str, limit: int = 88) -> str:
    """``text`` squashed to a single line and trimmed to ``limit`` chars --
    a collapsed row's summary. Collapses *all* whitespace (not just takes
    the first line): pretty-printed JSON stored with real newlines (e.g.
    ``'{\\n  "output": "OK",\\n  ...\\n}'``) has ``"{"`` alone as its first
    line, which as a summary tells you nothing -- squashing the whole thing
    to one line surfaces the actual content instead."""
    collapsed = " ".join(text.split())
    if not collapsed:
        return "(empty)"
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def pretty_yaml(text: str) -> str:
    """Render ``text`` as YAML if it's valid JSON; otherwise return it unchanged.

    Tool call arguments and results arrive as compact single-line JSON --
    fine for the wire, unreadable in a code viewer with room to spare. YAML
    over re-indented JSON specifically because a JSON string can only ever
    hold a newline as a literal ``\\n`` escape, even pretty-printed; YAML's
    block-scalar style (see ``_BlockStringDumper``) renders an embedded
    multi-line value -- a command's stdout, say -- as actual lines. Non-JSON
    text (plain command output with no JSON wrapper) passes through as-is.
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    parsed = _strip_trailing_whitespace(parsed)
    return yaml.dump(parsed, Dumper=_BlockStringDumper, sort_keys=False, allow_unicode=True)


# -- chat attachments ----------------------------------------------------


def sanitize_filename(name: str) -> str:
    """A safe filename: basename only, no path separators or control chars.

    Some browsers report a full path (``C:\\fakepath\\x``, ``/home/u/x``);
    reducing to the basename is what stops an upload from escaping the
    uploads directory through its own name. Control characters and the
    Windows trailing-dot/space hazard are stripped too. Falls back to
    ``upload`` when nothing usable remains.
    """
    basename = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", basename).strip(" .")
    return cleaned or "upload"


_UPLOAD_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_component(value: str, fallback: str) -> str:
    """Scrub a free-form string (a session id) down to a safe path component."""
    cleaned = _UPLOAD_COMPONENT_RE.sub("-", value or "").strip("-.")
    return cleaned or fallback


def upload_target(uploads_root: Path, session_id: str, filename: str) -> Path:
    """Collision-safe destination for one chat attachment.

    Files land in ``<uploads_root>/<sanitized-session-id>/``; a name already
    present gets a `` (1)``/`` (2)`` suffix instead of being silently
    overwritten. The parent directory is not created here -- the save itself
    does that -- so a name is only ever de-duplicated against files that
    already exist.
    """
    safe_name = sanitize_filename(filename)
    directory = uploads_root / _safe_component(session_id, "session")
    stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
    candidate = directory / safe_name
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


def attachment_block(paths: Sequence[str]) -> str:
    """The text appended to a user message for its attached files.

    A short explicit block (not bare paths) so the agent sees the files are
    intentional attachments, each an absolute path its own file tools can
    open -- the same path-reference convention every other Hermes surface
    uses for inbound media.
    """
    if not paths:
        return ""
    lines = ["Attached files:"]
    lines += [f"- {path}" for path in paths]
    return "\n".join(lines)
