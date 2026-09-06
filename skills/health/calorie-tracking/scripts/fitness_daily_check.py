#!/usr/bin/env python3
"""fitness_daily_check.py — gatekeeper for the Fitness Daily Planner cron.

Runs BEFORE the agent. Reads the current todo.fitness, compares to a stored
snapshot, and decides whether the agent should run.

Exit codes:
  0 (empty stdout) — nothing to do, agent suppressed (no tokens spent)
  0 (with stdout)  — agent should run with this context block

After the agent runs, this script saves a new snapshot.

Stdlib-only, no external dependencies.
"""

import datetime
import json
import os
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ENV_PATH = os.environ.get("HASS_ENV_PATH", os.path.expanduser("~/.hermes/.env"))
DEFAULT_BASE_URL = "http://hearth.lan"
WORKOUTS_LOG = (
    os.environ.get("WORKOUTS_LOG_PATH")
    or os.environ.get("WORKOUTS_PATH")
    or "/var/lib/hermes/workspace/notes-and-research/fitness/workouts.jsonl"
)
SNAPSHOT_PATH = os.environ.get(
    "FITNESS_SNAPSHOT_PATH",
    "/var/lib/hermes/.hermes/scripts/fitness_daily_snapshot.json",
)
STALE_DAYS = 7
TODO_ENTITY = os.environ.get("FITNESS_TODO_ENTITY", "todo.fitness")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = 10

# ---------------------------------------------------------------------------
# HA helpers
# ---------------------------------------------------------------------------


def load_env(path):
    env = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    except OSError:
        pass
    return env


def load_credentials():
    env = load_env(ENV_PATH)
    url = env.get("HASS_URL")
    token = env.get("HASS_TOKEN")
    if not url or not token:
        print("error: HASS_URL/HASS_TOKEN missing", file=sys.stderr)
        sys.exit(1)
    return url.rstrip("/"), token


def http_post(base_url, token, path, body, query=None):
    url = base_url + path
    if query:
        url += "?" + query
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {}
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        print(f"warning: HA request failed: {e}", file=sys.stderr)
        return {}


def http_get(base_url, token, path):
    req = urllib.request.Request(
        base_url + path,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {}
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return {}


def get_todo_items(base_url, token):
    resp = http_post(
        base_url,
        token,
        "/api/services/todo/get_items",
        {"entity_id": TODO_ENTITY},
        query="return_response=true",
    )
    service_response = resp.get("service_response", {}) if isinstance(resp, dict) else {}
    for value in service_response.values():
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            return value["items"]
    return []


def get_sensor(base_url, token, entity_id):
    """Read a sensor value as a float, or None if unavailable."""
    data = http_get(base_url, token, f"/api/states/{entity_id}")
    state = data.get("state") if isinstance(data, dict) else None
    if state is None or state in ("unknown", "unavailable"):
        return None
    try:
        return float(state)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Snapshot I/O
# ---------------------------------------------------------------------------


def load_snapshot():
    try:
        with open(SNAPSHOT_PATH) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_snapshot(todo_items, last_agent_run, feedback="", rpe=None):
    data = {
        "todo_summary": [item.get("summary", "") for item in todo_items if isinstance(item, dict)],
        "last_agent_run": last_agent_run,
        "feedback": feedback,
        "rpe": rpe,
        "saved_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        with open(SNAPSHOT_PATH, "w") as fh:
            json.dump(data, fh)
    except OSError as e:
        print(f"warning: could not save snapshot: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Workout history
# ---------------------------------------------------------------------------


def load_workouts():
    if not os.path.exists(WORKOUTS_LOG):
        return []
    entries = []
    try:
        with open(WORKOUTS_LOG) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if isinstance(entry, dict):
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return entries


def last_workout_date(workouts):
    """Return the date of the most recent workout, or None."""
    for entry in reversed(workouts):
        if not isinstance(entry, dict):
            continue
        date_str = entry.get("date")
        if isinstance(date_str, str) and len(date_str) == 10:
            try:
                return datetime.date.fromisoformat(date_str)
            except ValueError:
                continue
    return None


def gap_since_last_workout(workouts):
    """Return days since last workout, or None if no workouts."""
    last = last_workout_date(workouts)
    if last is None:
        return None
    return (datetime.date.today() - last).days


def format_workout_history(workouts, max_entries=10):
    """Format recent workouts for context output."""
    if not workouts:
        return "No workout history recorded."
    recent = workouts[-max_entries:]
    lines = []
    for entry in recent:
        date = entry.get("date", "?")
        session = entry.get("session", "?")
        source = entry.get("source", "unknown")
        lines.append(f"  {date}: {session} (source={source})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


def build_context(todo_items, workouts, rpe, feedback, weight, body_fat, lean_mass, gap, is_stale):
    lines = []
    lines.append("=== FITNESS DAILY CHECK ===")
    lines.append(f"Date: {datetime.date.today().isoformat()}")
    lines.append("")

    # User profile
    lines.append("[User Profile]")
    if weight is not None:
        lines.append(f"  Weight: {weight:.1f} kg")
    if body_fat is not None:
        lines.append(f"  Body fat: {body_fat:.1f}%")
    if lean_mass is not None:
        lines.append(f"  Lean mass: {lean_mass:.1f} kg")
    lines.append("  Equipment: Fit4Less (dumbbells, barbells, cable machines, standard machines)")
    lines.append("  Schedule: intends 2 days/week, may skip weeks or months")
    lines.append("  Goals: body composition + strength")
    lines.append("  Upper body pushing is very weak (2 assisted push-ups max)")
    lines.append("  Lower body is OK (60 lb hip thrusts)")
    lines.append("")

    # Gap info
    lines.append("[Workout Gap]")
    if gap is None:
        lines.append("  No workouts recorded yet — this is the first session.")
    else:
        lines.append(f"  Days since last workout: {gap}")
        if gap >= 60:
            lines.append("  LONG ABSENCE: Scale way back. Conservative baseline, very light.")
            lines.append("  Focus on: movement quality, not load. Start at ~50% of last known working weight.")
        elif gap >= 28:
            lines.append("  Extended break (4+ weeks). Reduce volume and load by ~30%.")
            lines.append("  Rebuild gradually over 2-3 sessions.")
        elif gap >= 14:
            lines.append("  Two weeks off. Slight deload (~15-20% reduction).")
        elif gap >= 7:
            lines.append("  Normal rest period. Ready for full session.")
        else:
            lines.append("  Recent workout. Consider rest day or different emphasis.")
    lines.append("")

    # Feedback
    lines.append("[Session Feedback]")
    if rpe is not None and rpe != 5.0:
        lines.append(f"  RPE: {rpe}")
    else:
        lines.append("  RPE: none recorded (default)")
    if feedback and feedback.strip():
        lines.append(f"  Notes: {feedback}")
    else:
        lines.append("  Notes: none")
    lines.append("")

    # Current todo
    lines.append("[Current todo.fitness]")
    if todo_items:
        for i, item in enumerate(todo_items):
            summary = item.get("summary", "?")
            status = item.get("status", "?")
            lines.append(f"  {i + 1}. [{status}] {summary}")
    else:
        lines.append("  (empty — no items)")
    lines.append("")

    # Workout history
    lines.append("[Workout History]")
    lines.append(format_workout_history(workouts))
    lines.append("")

    # Decision context
    lines.append("[Decision Context]")
    if is_stale:
        lines.append("  NOTE: todo has not changed in 7+ days. User may have been away.")
        lines.append("  Scale exercises conservatively if returning.")
    lines.append("  The todo list should ALWAYS have an appropriate plan waiting.")
    lines.append("  If the user just worked out (gap <= 1), set rest day.")
    lines.append("  If no items exist or they are stale, create a new session plan.")
    lines.append("  If the todo is current and appropriate, do nothing.")
    lines.append("  Feedback is one-shot: read it, use it, then clear RPE to 5 and notes to empty.")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    base_url, token = load_credentials()

    # 1. Get current state
    todo_items = get_todo_items(base_url, token)
    workouts = load_workouts()

    # Read body metrics from HA
    weight = get_sensor(base_url, token, "sensor.withings_weight")
    body_fat = get_sensor(base_url, token, "sensor.withings_fat_ratio")
    lean_mass = get_sensor(base_url, token, "sensor.withings_muscle_mass")

    # Read session feedback
    rpe_state = get_sensor(base_url, token, "input_number.workout_effort_rpe")
    feedback_data = http_get(base_url, token, "/api/states/input_text.workout_feedback")
    feedback = ""
    if isinstance(feedback_data, dict):
        feedback = feedback_data.get("state", "")

    # 2. Load snapshot and compare
    snapshot = load_snapshot()
    old_summary = snapshot.get("todo_summary", [])
    new_summary = [item.get("summary", "") for item in todo_items if isinstance(item, dict)]

    todo_changed = sorted(old_summary) != sorted(new_summary)
    has_items = len(todo_items) > 0

    # Detect new feedback (non-empty feedback or non-default RPE)
    old_feedback = snapshot.get("feedback", "")
    old_rpe = snapshot.get("rpe")
    has_new_feedback = bool(feedback and feedback.strip()) and (feedback.strip() != (old_feedback or "").strip())
    has_new_rpe = (rpe_state is not None and rpe_state != 5.0 and rpe_state != old_rpe)

    # Calculate gap
    gap = gap_since_last_workout(workouts)

    # Check staleness
    is_stale = False
    last_agent_run = snapshot.get("last_agent_run")
    if last_agent_run and not todo_changed:
        try:
            lr = datetime.datetime.fromisoformat(last_agent_run)
            days_since_agent = (datetime.datetime.now().astimezone() - lr).days
            if days_since_agent >= STALE_DAYS:
                is_stale = True
        except (ValueError, TypeError):
            pass

    # 3. Decide whether agent should run
    # Agent runs when:
    #   a) todo changed (user completed items or todo was modified)
    #   b) todo unchanged but >= 7 days since last agent run (stale check)
    #   c) no items in todo (need to populate)
    #   d) new workout feedback or RPE left by user
    should_run = todo_changed or is_stale or not has_items or has_new_feedback or has_new_rpe

    if not should_run:
        # Suppress: exit 0 with empty stdout
        sys.exit(0)

    # 4. Build and output context
    context = build_context(
        todo_items, workouts, rpe_state, feedback,
        weight, body_fat, lean_mass, gap, is_stale,
    )
    print(context)

    # 5. Save snapshot for next comparison
    # Note: the agent should save a new snapshot after running,
    # but we save now too so the next check can diff against today's state.
    save_snapshot(todo_items, datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                  feedback=feedback or "", rpe=rpe_state)


if __name__ == "__main__":
    main()
