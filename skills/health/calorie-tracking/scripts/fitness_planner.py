#!/usr/bin/env python3
"""fitness_planner.py — archive completed HA todo.fitness items into workouts.jsonl.

Purely reactive: this planner records what the user actually did. It never
invents session names, prescribes a routine, assigns due dates, or bumps
anything on the todo list. It is an archivist, not a trainer.

Stdlib-only CLI (sync|log|next|status). Bare invocation defaults to `sync`.
"""

import argparse
import datetime
import json
import os
import statistics
import sys
import urllib.error
import urllib.request

ENV_PATH = os.environ.get("HASS_ENV_PATH", os.path.expanduser("~/.hermes/.env"))
DEFAULT_BASE_URL = "http://hearth.lan"
LOG_PATH = (
    os.environ.get("WORKOUTS_LOG_PATH")
    or os.environ.get("WORKOUTS_PATH")
    or "/var/lib/hermes/workspace/notes-and-research/fitness/workouts.jsonl"
)
CONFIG_PATH = os.environ.get(
    "FITNESS_CONFIG_PATH",
    "/var/lib/hermes/.hermes/scripts/fitness_config.json",
)
DEFAULT_TODO_ENTITY = "todo.fitness"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = 10


def err(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def usage_err(parser, msg):
    print(f"error: {msg}", file=sys.stderr)
    parser.print_usage(sys.stderr)
    sys.exit(2)


def dry_run():
    return os.environ.get("DRY_RUN") == "1"


def out(msg):
    prefix = "[DRY_RUN] " if dry_run() else ""
    print(prefix + msg)


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
    except OSError as e:
        err(f"cannot read {path}: {e}")
    return env


def load_credentials():
    env = load_env(ENV_PATH)
    url = env.get("HASS_URL")
    token = env.get("HASS_TOKEN")
    if not url or not token:
        err(f"HASS_URL/HASS_TOKEN missing from {ENV_PATH}")
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
                return resp.status, {}
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
            raw = parsed.get("message", raw)
        except json.JSONDecodeError:
            pass
        err(f"POST {path}: HTTP {e.code}: {raw[:500]}")
    except (urllib.error.URLError, OSError) as e:
        err(f"POST {path}: {e}")


def get_items(base_url, token, entity_id):
    _, resp = http_post(
        base_url,
        token,
        "/api/services/todo/get_items",
        {"entity_id": entity_id},
        query="return_response=true",
    )
    service_response = resp.get("service_response", {}) if isinstance(resp, dict) else {}
    for value in service_response.values():
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            return value["items"]
    return []


def remove_item(base_url, token, entity_id, uid):
    http_post(
        base_url,
        token,
        "/api/services/todo/remove_item",
        {"entity_id": entity_id, "item": uid},
    )


def load_config():
    try:
        with open(CONFIG_PATH) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def cfg_todo_entity(cfg):
    entity = os.environ.get("FITNESS_TODO_ENTITY")
    if entity:
        return entity
    entity = cfg.get("todo_entity")
    if isinstance(entity, str) and entity:
        return entity
    return DEFAULT_TODO_ENTITY


def load_log():
    if not os.path.exists(LOG_PATH):
        return []
    entries = []
    try:
        with open(LOG_PATH) as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as e:
                    print(f"error: corrupt workouts log line {lineno}: {e}", file=sys.stderr)
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
    except OSError as e:
        err(f"cannot read {LOG_PATH}: {e}")
    return entries


def append_log(entry):
    if dry_run():
        return
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as e:
        err(f"cannot append to {LOG_PATH}: {e}")


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def already_logged(log_entries, date_str, session):
    for entry in log_entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("date") == date_str and entry.get("session") == session:
            return True
    return False


def last_logged(log_entries):
    for entry in reversed(log_entries):
        if not isinstance(entry, dict):
            continue
        session = entry.get("session")
        date_raw = entry.get("date")
        if not isinstance(session, str) or not session:
            continue
        if not isinstance(date_raw, str):
            continue
        try:
            date_obj = datetime.date.fromisoformat(date_raw)
        except ValueError:
            continue
        return session, date_obj
    return None, None


def valid_log_dates(log_entries):
    dates = []
    for entry in log_entries:
        if not isinstance(entry, dict):
            continue
        date_raw = entry.get("date")
        if not isinstance(date_raw, str):
            continue
        try:
            dates.append(datetime.date.fromisoformat(date_raw))
        except ValueError:
            continue
    return dates


def median_gap(log_entries):
    dates = sorted(valid_log_dates(log_entries))
    if len(dates) < 2:
        return None
    gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
    return statistics.median(gaps)


def run_sync(cfg):
    base_url, token = load_credentials()
    log_entries = load_log()
    entity = cfg_todo_entity(cfg)
    today = datetime.date.today()
    logged = []

    items = get_items(base_url, token, entity)

    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("status") != "completed":
            continue
        summary = item.get("summary")
        if not isinstance(summary, str) or not summary:
            continue
        due = item.get("due")
        if isinstance(due, str) and len(due) == 10:
            date_str = due
        else:
            date_str = today.isoformat()
        if not already_logged(log_entries, date_str, summary):
            entry = {
                "ts": now_iso(),
                "date": date_str,
                "session": summary,
                "source": "todo",
            }
            if dry_run():
                out(f"would append {summary} ({date_str}) to workouts log")
            else:
                append_log(entry)
                log_entries.append(entry)
            logged.append((summary, date_str))
        uid = item.get("uid")
        if dry_run():
            out(f"would remove {summary!r} (uid {uid})")
        else:
            remove_item(base_url, token, entity, uid)

    if logged:
        parts = ", ".join(f"{s} ({d})" for s, d in logged)
        out(f"Logged: {parts}.")


def cmd_log(args, parser):
    session = (args.session or "").strip()
    if not session:
        usage_err(parser, "--session is required and cannot be empty")
    if args.date:
        try:
            datetime.date.fromisoformat(args.date)
        except ValueError:
            usage_err(parser, f"invalid --date {args.date!r} (expected YYYY-MM-DD)")
    date_str = args.date or datetime.date.today().isoformat()

    base_url, token = load_credentials()
    cfg = load_config()
    log_entries = load_log()

    if already_logged(log_entries, date_str, session):
        out(f"ALREADY_LOGGED: {session} ({date_str})")
        return

    entry = {"ts": now_iso(), "date": date_str, "session": session, "source": "chat"}
    if dry_run():
        out(f"would append {session} ({date_str}) to workouts log")
    else:
        append_log(entry)

    entity = cfg_todo_entity(cfg)
    items = get_items(base_url, token, entity)
    prefix = session + " \u00b7 "
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("status") != "needs_action":
            continue
        summary = item.get("summary")
        if not isinstance(summary, str):
            continue
        if summary != session and not summary.startswith(prefix):
            continue
        uid = item.get("uid")
        if dry_run():
            out(f"would remove {summary!r} (uid {uid})")
        else:
            remove_item(base_url, token, entity, uid)

    out(f"Logged: {session} ({date_str}).")


def cmd_next():
    log_entries = load_log()
    last_session, last_date = last_logged(log_entries)
    if last_session:
        last_txt = f"{last_session} ({last_date.isoformat()})"
    else:
        last_txt = "none"
    gap = median_gap(log_entries)
    if gap is None:
        gap_txt = "n/a"
    elif gap == int(gap):
        gap_txt = f"{int(gap)}d"
    else:
        gap_txt = f"{gap}d"
    out(f"last: {last_txt} | sessions: {len(log_entries)} | median gap: {gap_txt}")


def cmd_status(cfg):
    log_entries = load_log()
    entity = cfg_todo_entity(cfg)
    out(f"workouts log: {LOG_PATH}")
    out(f"entries: {len(log_entries)}")
    for entry in log_entries[-3:]:
        date = entry.get("date", "?")
        session = entry.get("session", "?")
        source = entry.get("source", "?")
        out(f"  {date} {session} ({source})")
    out(f"todo entity: {entity}")


def main():
    parser = argparse.ArgumentParser(
        prog="fitness_planner.py",
        description="Archive completed HA todo items into the workout log.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("sync", help="archive completed todo items into the workout log")

    p_log = sub.add_parser("log", help="log a completed workout")
    p_log.add_argument("--session", help="session name (required)")
    p_log.add_argument("--date", help="date YYYY-MM-DD (default: today)")

    sub.add_parser("next", help="factual readout of the workout log")
    sub.add_parser("status", help="show workout log and config")

    args = parser.parse_args()
    cfg = load_config()
    command = args.command or "sync"
    if command == "sync":
        run_sync(cfg)
    elif command == "log":
        cmd_log(args, p_log)
    elif command == "next":
        cmd_next()
    elif command == "status":
        cmd_status(cfg)


if __name__ == "__main__":
    main()
