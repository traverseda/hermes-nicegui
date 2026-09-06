#!/usr/bin/env python3
"""fitness_snapshot.py — data collector for the Fitness Assistant cronjob.

Reads fitness/food data from Home Assistant (REST, stdlib urllib only) plus
the food log file, and prints a compact snapshot text to stdout for injection
into an agent prompt that writes recommendations. Never crashes on missing
data — degrades gracefully (every entity read is best-effort; on failure the
relevant value prints as unavailable/no data and the script continues).
"""

import datetime
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import quote

ENV_PATH = os.environ.get("HASS_ENV_PATH", os.path.expanduser("~/.hermes/.env"))
LOG_PATH = os.environ.get(
    "FOOD_LOG_PATH", "/var/lib/hermes/workspace/notes-and-research/fitness/food-log.jsonl"
)
WORKOUTS_LOG_PATH = os.environ.get(
    "WORKOUTS_LOG_PATH",
    "/var/lib/hermes/workspace/notes-and-research/fitness/workouts.jsonl",
)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = 10
DAYS = 7
# Fallback targets used ONLY when no body data exists to derive from.
FALLBACK_TARGET_KCAL = 2700
FALLBACK_TARGET_PROTEIN = 145
BULK_SURPLUS = 300  # kcal over TDEE for a moderate bulk
PROTEIN_PER_KG = 1.8  # g protein per kg body weight
CONFIG_PATH = os.environ.get(
    "FITNESS_TARGETS_PATH", "/var/lib/hermes/.hermes/scripts/fitness_targets.json"
)
NUTRIENTS = ("kcal", "protein", "carbs", "fat")

# Only sensor.pixel_8_weight is named in the spec; the rest follow the same
# pixel_8 naming pattern. Adjust here if an entity id differs on the HA side —
# a wrong id degrades to "unavailable", never a crash.
SENSORS = {
    "weight": "sensor.pixel_8_weight",
    "body_fat": "sensor.pixel_8_body_fat",
    "lean_mass": "sensor.pixel_8_lean_body_mass",
    "burned": "sensor.pixel_8_total_calories_burned",
    "steps": "sensor.pixel_8_daily_steps",
    "sleep": "sensor.pixel_8_sleep_duration",
    "bmr": "sensor.pixel_8_basal_metabolic_rate",
    "activity": "sensor.pixel_8_detected_activity",
    "last_workout": "sensor.withings_last_workout_type",
    "workout_calories": "sensor.withings_calories_burnt_last_workout",
}


def load_env(path):
    env = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def load_credentials():
    """Return (base_url, token) or (None, None) — errors go to stderr."""
    try:
        env = load_env(ENV_PATH)
    except OSError as e:
        print(f"error: cannot read {ENV_PATH}: {e}", file=sys.stderr)
        return None, None
    url = env.get("HASS_URL")
    token = env.get("HASS_TOKEN")
    if not url or not token:
        print(f"error: HASS_URL/HASS_TOKEN missing from {ENV_PATH}", file=sys.stderr)
        return None, None
    return url.rstrip("/"), token


def load_config():
    """Return the fitness-targets config dict, or {} when unreadable/invalid."""
    try:
        with open(CONFIG_PATH) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot read {CONFIG_PATH}: {e}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def http_get_json(url, token):
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


def get_state(base_url, token, entity_id):
    """Return the state dict for entity_id or None (network/parse failure)."""
    if not token:
        return None
    url = f"{base_url}/api/states/{entity_id}"
    try:
        data = http_get_json(url, token)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        print(f"error: GET /api/states/{entity_id}: {e}", file=sys.stderr)
        return None
    return data if isinstance(data, dict) else None


def get_history(base_url, token, entity_id, days_ago=DAYS):
    """Return list of state changes for entity_id since days_ago, oldest first.

    Returns None on failure. Each item is a minimal state dict (state,
    last_changed, last_updated) because of minimal_response&no_attributes.
    """
    if not token:
        return None
    start = datetime.datetime.now().astimezone() - datetime.timedelta(days=days_ago)
    start_iso = start.isoformat(timespec="seconds")
    url = (
        f"{base_url}/api/history/period/{quote(start_iso, safe='')}"
        f"?filter_entity_id={entity_id}&minimal_response&no_attributes"
    )
    try:
        data = http_get_json(url, token)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        print(f"error: GET /api/history/period for {entity_id}: {e}", file=sys.stderr)
        return None
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    return first if isinstance(first, list) else None


def parse_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_food_log():
    """Return list of food-log entries, [] when empty/missing, None unreadable."""
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
                    entries.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"error: corrupt food log line {lineno}: {e}", file=sys.stderr)
    except OSError as e:
        print(f"error: cannot read {LOG_PATH}: {e}", file=sys.stderr)
        return None
    return entries


def load_workout_log():
    """Return list of workouts.jsonl entries, [] when missing/corrupt, None unreadable."""
    if not os.path.exists(WORKOUTS_LOG_PATH):
        return []
    entries = []
    try:
        with open(WORKOUTS_LOG_PATH) as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
    except OSError as e:
        print(f"error: cannot read {WORKOUTS_LOG_PATH}: {e}", file=sys.stderr)
        return None
    return entries


def fmt_int(x):
    return str(int(x))


def fmt1(x):
    return f"{x:.1f}"


def build_snapshot(base_url, token):
    lines = []

    state = {}
    for name, entity_id in SENSORS.items():
        state[name] = get_state(base_url, token, entity_id)

    weight_grams = None
    if state["weight"] is not None:
        weight_grams = parse_float(state["weight"].get("state"))
    weight_kg = weight_grams / 1000.0 if weight_grams is not None else None

    body_fat = parse_float(state["body_fat"].get("state")) if state["body_fat"] else None
    lean_mass = parse_float(state["lean_mass"].get("state")) if state["lean_mass"] else None
    burned = parse_float(state["burned"].get("state")) if state["burned"] else None
    steps = parse_float(state["steps"].get("state")) if state["steps"] else None
    sleep = parse_float(state["sleep"].get("state")) if state["sleep"] else None
    bmr = parse_float(state["bmr"].get("state")) if state["bmr"] else None

    # Derived targets: TDEE = today's burned (or BMR*1.2 fallback) + bulk
    # surplus; protein = 1.8 g/kg body weight. target_kcal/target_protein in
    # the config file (when positive numbers) override these defaults.
    tdee = None
    if burned is not None:
        tdee = burned
    elif bmr is not None:
        tdee = bmr * 1.2

    def _positive(value):
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > 0
        )

    config = load_config()
    cfg_kcal = config.get("target_kcal")
    cfg_protein = config.get("target_protein")
    kcal_is_config = _positive(cfg_kcal)
    protein_is_config = _positive(cfg_protein)
    target_kcal = (
        int(cfg_kcal)
        if kcal_is_config
        else (int(tdee + BULK_SURPLUS) if tdee is not None else FALLBACK_TARGET_KCAL)
    )
    target_protein = (
        int(cfg_protein)
        if protein_is_config
        else (
            round(PROTEIN_PER_KG * weight_kg)
            if weight_kg is not None
            else FALLBACK_TARGET_PROTEIN
        )
    )

    hist = get_history(base_url, token, SENSORS["weight"]) if weight_grams is not None else None
    hist_grams = []
    if hist:
        for h in hist:
            v = parse_float(h.get("state"))
            if v is not None:
                hist_grams.append(v)
    ago_grams = None
    distinct = set(hist_grams)
    if weight_grams is not None:
        distinct.add(weight_grams)
    if len(distinct) >= 2 and hist_grams:
        ago_grams = hist_grams[0]
    delta_kg = None
    if ago_grams is not None and weight_grams is not None:
        delta_kg = weight_grams / 1000.0 - ago_grams / 1000.0

    last_weighin_date = None
    if state["weight"] is not None:
        lu = state["weight"].get("last_updated")
        if lu:
            try:
                last_weighin_date = datetime.date.fromisoformat(lu[:10])
            except ValueError:
                last_weighin_date = None

    food_log = load_food_log()
    entries = food_log if isinstance(food_log, list) else []
    today = datetime.date.today()
    today_iso = today.isoformat()
    day_range = {today_iso}
    for i in range(1, DAYS):
        day_range.add((today - datetime.timedelta(days=i)).isoformat())

    today_entries = [e for e in entries if e.get("day") == today_iso]
    if today_entries:
        t = {
            k: sum(int(e.get("totals", {}).get(k, 0)) for e in today_entries)
            for k in NUTRIENTS
        }
    else:
        t = None

    window = [e for e in entries if e.get("day") in day_range]
    per_day = {}
    for e in window:
        d = e.get("day")
        if not d:
            continue
        day_totals = per_day.setdefault(d, {k: 0 for k in NUTRIENTS})
        for k in NUTRIENTS:
            day_totals[k] += int(e.get("totals", {}).get(k, 0))
    logged_days = sorted(per_day)

    lines.append("## Today")
    kcal_target_txt = f"{target_kcal} (configured)" if kcal_is_config else str(target_kcal)
    protein_target_txt = (
        f"{target_protein} (configured)" if protein_is_config else str(target_protein)
    )
    if t is not None:
        lines.append(
            f"- kcal: {t['kcal']} (target {kcal_target_txt}) | protein: {t['protein']} g "
            f"(target {protein_target_txt} g) | carbs: {t['carbs']} g | fat: {t['fat']} g"
        )
    else:
        lines.append("- kcal: no data | protein: no data | carbs: no data | fat: no data")

    def _kcal_desc():
        if kcal_is_config:
            return f"{target_kcal} kcal/day (configured)"
        if tdee is not None:
            return f"{target_kcal} kcal/day (TDEE {tdee:.0f} + {BULK_SURPLUS} surplus)"
        return f"{target_kcal} kcal/day (fallback)"

    def _protein_desc():
        if protein_is_config:
            return f"{target_protein} g protein (configured)"
        if weight_kg is not None:
            return f"{target_protein} g protein ({PROTEIN_PER_KG} g/kg x {weight_kg:.1f} kg)"
        return f"{target_protein} g protein (fallback)"

    if kcal_is_config or protein_is_config:
        if kcal_is_config and protein_is_config:
            lines.append(
                f"- configured targets: {target_kcal} kcal/day | {target_protein} g protein"
            )
        else:
            lines.append(f"- targets: {_kcal_desc()} | {_protein_desc()}")
    elif tdee is not None:
        lines.append(
            f"- derived targets: {target_kcal} kcal/day (TDEE {tdee:.0f} + {BULK_SURPLUS} surplus) | "
            f"{target_protein} g protein ({PROTEIN_PER_KG} g/kg x {weight_kg:.1f} kg)"
        )
    else:
        lines.append(
            f"- derived targets: unavailable (fallback {target_kcal} kcal / {target_protein} g protein)"
        )
    act = state["activity"].get("state") if state["activity"] else None
    if act:
        lw = state["last_workout"].get("state") if state["last_workout"] else None
        wc = (
            parse_float(state["workout_calories"].get("state"))
            if state["workout_calories"]
            else None
        )
        wline = f"- activity: {act}"
        if lw and lw.lower() not in ("unknown", "unavailable", "none"):
            wline += f" | last workout: {lw}"
            if wc is not None:
                wline += f" ({wc:.0f} kcal)"
        lines.append(wline)
    if t is not None and burned is not None:
        lines.append(
            f"- calorie balance (consumed minus burned): {t['kcal'] - int(burned):+d} kcal"
        )
    else:
        lines.append("- calorie balance (consumed minus burned): no data")
    lines.append(
        f"- burned: {fmt_int(burned) if burned is not None else 'unavailable'} kcal | "
        f"steps: {fmt_int(steps) if steps is not None else 'unavailable'} | "
        f"sleep last night: {fmt_int(sleep) if sleep is not None else 'unavailable'} min"
    )

    lines.append("")
    lines.append("## 7-day intake (from food log)")
    if not logged_days:
        lines.append("- no entries")
    else:
        n = len(logged_days)
        avg_kcal = round(sum(per_day[d]["kcal"] for d in logged_days) / n)
        avg_protein = round(sum(per_day[d]["protein"] for d in logged_days) / n)
        lines.append(
            f"- avg kcal/day: {avg_kcal} | avg protein/day: {avg_protein} g | "
            f"days logged: {n}/{DAYS}"
        )
        rows = ", ".join(
            f"{d} kcal={per_day[d]['kcal']} protein={per_day[d]['protein']}"
            for d in logged_days
        )
        lines.append(f"- per-day rows: {rows}")

    lines.append("")
    lines.append("## Body (latest)")
    w = fmt1(weight_kg) if weight_kg is not None else "unavailable"
    bf = fmt1(body_fat) if body_fat is not None else "unavailable"
    lean_kg = lean_mass / 1000.0 if lean_mass is not None else None
    lm = fmt1(lean_kg) if lean_kg is not None else "unavailable"
    lines.append(f"- weight: {w} kg | body fat: {bf}% | lean mass: {lm} kg")
    if bmr is not None:
        lines.append(f"- basal metabolic rate: {fmt_int(bmr)} kcal/day")
    if ago_grams is not None and delta_kg is not None:
        lines.append(
            f"- weight 7 days ago: {fmt1(ago_grams / 1000.0)} kg | delta: {delta_kg:+.1f} kg"
        )
    else:
        lines.append("- weight 7 days ago: no data | delta: no data")
    lines.append(
        f"- last weigh-in: {last_weighin_date.isoformat() if last_weighin_date else 'no data'}"
    )

    lines.append("")
    lines.append("## Training (last 7d)")
    workout_log = load_workout_log()
    workouts_missing = not os.path.exists(WORKOUTS_LOG_PATH)
    workout_entries = workout_log if isinstance(workout_log, list) else []
    workout_window = [
        e for e in workout_entries
        if isinstance(e, dict) and e.get("date") in day_range
    ]
    workout_window.sort(
        key=lambda e: (e.get("date", ""), e.get("ts", "")), reverse=True
    )
    if not workout_window:
        lines.append("- no workouts logged in last 7 days")
    else:
        last_w = workout_window[0]
        lines.append(
            f"- sessions: {len(workout_window)} | last: {last_w.get('session')} ({last_w.get('date')})"
        )
        for e in workout_window:
            lines.append(f"- {e.get('date')}: {e.get('session')}")

    lines.append("")
    lines.append("## Gaps / flags")
    flags = []
    if workouts_missing:
        flags.append("workouts log missing")
    elif not workout_window:
        flags.append("no workouts logged in 7 days")
    if food_log is None:
        flags.append("food log unreadable")
    elif not entries:
        flags.append("food log empty")
    elif today_iso not in per_day:
        flags.append("no food logged today")
    if weight_kg is None:
        flags.append("weight: unavailable")
    else:
        if last_weighin_date is None:
            flags.append("last weigh-in: unknown")
        elif (today - last_weighin_date).days >= DAYS:
            flags.append("no weigh-in in 7+ days")
        if ago_grams is None:
            flags.append("weight delta: no data")
    if body_fat is None:
        flags.append("body fat: unavailable")
    if lean_mass is None:
        flags.append("lean mass: unavailable")
    if burned is None:
        flags.append("burned: unavailable")
    if steps is None:
        flags.append("steps: unavailable")
    if sleep is None:
        flags.append("sleep: unavailable")
    if state["activity"] is None:
        flags.append("activity: unavailable")
    if not flags:
        flags.append("none")
    for f in flags:
        lines.append(f"- {f}")

    return lines


def main():
    base_url, token = load_credentials()
    lines = build_snapshot(base_url, token)
    if os.environ.get("DRY_RUN") == "1":
        print("[DRY_RUN]")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
