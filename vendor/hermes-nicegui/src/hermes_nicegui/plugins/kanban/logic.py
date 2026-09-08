"""Pure helpers and board-stage metadata for the kanban plugin: no NiceGUI, no I/O.

Mirrors ``plugins/cron/logic.py``'s split, except task timestamps from the
dashboard API are Unix epoch seconds (``created_at: 1786668908``) rather than
ISO-8601 strings, hence separate ``fmt_epoch*`` helpers instead of reusing
cron's ``fmt_iso*``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime


def fmt_epoch(value: float | int | None) -> str:
    """Human-readable local time for a Unix epoch timestamp."""
    if not value:
        return "—"
    return datetime.fromtimestamp(value, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M")


def fmt_epoch_age(value: float | int | None) -> str:
    """A short relative label (``in 5m``, ``2h ago``) for a Unix epoch timestamp."""
    if not value:
        return ""
    delta = value - datetime.now(UTC).timestamp()
    future = delta >= 0
    delta = abs(delta)
    if delta < 60:
        return "now"
    if delta < 3600:
        label = f"{int(delta // 60)}m"
    elif delta < 86400:
        label = f"{int(delta // 3600)}h"
    else:
        label = datetime.fromtimestamp(value, tz=UTC).astimezone().strftime("%b %d")
    return f"in {label}" if future else f"{label} ago"


# The board's fixed column set, in the order the dashboard's own `GET /board`
# renders them -- hardcoded here (rather than trusting whatever order a given
# response happens to list) so the "Move to" selector always offers all eight
# statuses in a stable order, even for a task whose status isn't among the
# columns a particular board response included.
CANONICAL_COLUMNS = [
    "triage",
    "todo",
    "scheduled",
    "ready",
    "running",
    "review",
    "blocked",
    "done",
]

STAGE_HELP: dict[str, str] = {
    "triage": (
        "Inbox for new tasks — an empty body can be auto-fleshed-out by the specifier, "
        "which then promotes the task to To Do."
    ),
    "todo": (
        "Specified, but not ready to run — waits here while its parent dependencies are open, "
        "then auto-promotes to Ready."
    ),
    "scheduled": "Parked on the calendar — waiting on time, not on human input.",
    "ready": (
        "Picked up by the live dispatcher (usually within ~60s), which claims it and spawns a "
        "real agent run."
    ),
    "running": "A profile has claimed the task and an agent is actively working on it right now.",
    "review": (
        "Implementation is done and handed off — a reviewer approves it (→ Done) or sends it "
        "back with requested changes."
    ),
    "blocked": (
        "Stuck and waiting on something external — a human decision, missing credentials, a "
        "dependency, or a transient failure. Unblock to re-queue it."
    ),
    "done": "Finished and accepted — the work is complete and the task is closed.",
}

_COLUMN_META: dict[str, tuple[str, str, str]] = {
    "triage": ("Triage", "inbox", "grey"),
    "todo": ("To Do", "checklist", "grey"),
    "scheduled": ("Scheduled", "event", "primary"),
    "ready": ("Ready", "play_circle", "primary"),
    "running": ("Running", "autorenew", "secondary"),
    "blocked": ("Blocked", "block", "negative"),
    "review": ("Review", "rate_review", "warning"),
    "done": ("Done", "check_circle", "positive"),
}


def column_meta(status: str | None) -> tuple[str, str, str]:
    """(label, icon, color) for a task/column status."""
    return _COLUMN_META.get(status or "", ((status or "Unknown").title(), "help_outline", "grey"))


def fmt_priority(priority: int | None) -> str:
    return f"P{priority}" if priority is not None else "—"


def should_auto_specify(body: str | None, status: str | None) -> bool:
    """Whether a task just created from this UI should be auto-specified.

    True when the description is empty/whitespace AND the task landed in
    Triage — the only column the triage specifier (`hermes kanban specify`)
    will touch. A non-triage creation with an empty body can't be
    auto-specified by the built-in tool, so callers should warn instead.
    """
    return not (body or "").strip() and status == "triage"


def parse_specify_outcome(stdout: str) -> tuple[bool, str, str | None]:
    """Parse one `--json` line from ``hermes kanban specify``.

    Returns ``(ok, reason, new_title)``. The CLI emits exactly one JSON
    object per task on stdout (even on failure, with an ``ok: false``
    payload), so the last non-empty line is the outcome. Falls back to a
    ``(False, ...)`` verdict on any parse failure rather than raising —
    callers surface the reason to the user.
    """
    try:
        line = next(
            ln for ln in reversed((stdout or "").splitlines()) if ln.strip()
        )
        blob = json.loads(line)
    except (ValueError, StopIteration, json.JSONDecodeError):
        return False, "no JSON output from specifier", None
    if not isinstance(blob, dict):
        return False, "unexpected specifier output", None
    ok = bool(blob.get("ok"))
    reason = blob.get("reason") or ("specified" if ok else "specify failed")
    new_title = blob.get("new_title")
    return ok, str(reason), str(new_title) if isinstance(new_title, str) else None
