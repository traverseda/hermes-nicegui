#!/usr/bin/env python3
"""Weekly fitness data collector for the Fitness Planner cron.

Emits a compact markdown digest to stdout: food-log 7d totals, HA scale/phone
vitals, recent history, and prior weekly assessments (the running trend).
Everything is read-only. Honest about missing data.
"""
import datetime as dt
import glob
import json
import os
import subprocess
import urllib.request

HOME = os.path.expanduser("~")
LOG_PATH = f"{HOME}/workspace/notes-and-research/fitness/food-log.jsonl"
WEEKLY_DIR = f"{HOME}/workspace/notes-and-research/fitness/weekly"
ENV_PATH = f"{HOME}/.hermes/.env"

BASE = "http://hearth.lan"
UA = "Mozilla/5.0 (weekly-fitness-planner)"


def env_get(key):
    for line in open(ENV_PATH):
        line = line.strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    return None


def ha_get(path):
    token = env_get("HASS_TOKEN")
    req = urllib.request.Request(
        BASE + path, headers={"Authorization": f"Bearer {token}", "User-Agent": UA}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def ha_state(eid):
    try:
        s = ha_get(f"/api/states/{eid}")
        return s.get("state")
    except Exception:
        return None


def food_log_digest(days=7):
    """Per-day kcal/protein/carbs/fat from the JSONL for the last N days."""
    if not os.path.exists(LOG_PATH):
        return ["(no food log yet)"]
    today = dt.date.today()
    by_day = {}
    for line in open(LOG_PATH):
        try:
            e = json.loads(line)
        except Exception:
            continue
        day = e.get("date") or e.get("ts", "")[:10]
        t = e.get("totals", {})
        d = by_day.setdefault(day, {"kcal": 0, "protein": 0, "carbs": 0, "fat": 0})
        d["kcal"] += t.get("kcal", 0)
        d["protein"] += t.get("protein", 0)
        d["carbs"] += t.get("carbs", 0)
        d["fat"] += t.get("fat", 0)
    if not by_day:
        return ["(no logged days)"]
    out = []
    total = {"kcal": 0, "protein": 0, "carbs": 0, "fat": 0}
    n = 0
    for day in sorted(by_day, reverse=True)[:days]:
        d = by_day[day]
        out.append(
            f"- {day}: {d['kcal']:.0f} kcal | P {d['protein']:.0f}g | C {d['carbs']:.0f}g | F {d['fat']:.0f}g"
        )
        for k in total:
            total[k] += d[k]
        n += 1
    if n:
        out.append(
            f"- **{n}-day avg: {total['kcal']/n:.0f} kcal | P {total['protein']/n:.0f}g | C {total['carbs']/n:.0f}g | F {total['fat']/n:.0f}g**"
        )
    return out


def history_points(eid, days=7):
    """Compact daily series: last value per day for the last N days."""
    try:
        rows = ha_get(f"/api/history/period?filter_entity_id={eid}&minimal_response&no_attributes")
        data = rows[0] if rows else []
    except Exception:
        return []
    by_day = {}
    for p in data:
        lc = (p.get("last_changed") or "")[:10]
        if lc:
            by_day[lc] = p.get("state")
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    return [f"{d}: {by_day[d]}" for d in sorted(by_day) if d >= cutoff]


def prior_assessments(n=4):
    """Last N weekly assessment files (the stored trend)."""
    files = sorted(glob.glob(f"{WEEKLY_DIR}/weekly-*.md"), reverse=True)[:n]
    out = []
    for f in files:
        out.append(f"--- {os.path.basename(f)} ---")
        out.append(open(f).read().strip())
    return out


def main():
    print("# Weekly Fitness Data Digest")
    print(f"Generated: {dt.datetime.now().isoformat(timespec='minutes')}")
    print()

    print("## Food intake (last 7d)")
    for l in food_log_digest():
        print(l)
    print()

    vitals = [
        ("withings_weight", "kg", "scale weight"),
        ("withings_fat_ratio", "%", "body fat"),
        ("withings_muscle_mass", "kg", "muscle mass"),
        ("withings_fat_free_mass", "kg", "fat-free mass"),
        ("withings_sleep_score", "pts", "sleep score (last night)"),
        ("pixel_8_daily_steps", "", "steps today"),
        ("pixel_8_sleep_duration", "min", "sleep last night"),
        ("pixel_8_total_calories_burned", "kcal", "kcal burned today"),
        ("pixel_8_heart_rate_variability", "ms", "HRV"),
        ("pixel_8_basal_metabolic_rate", "kcal/day", "BMR"),
        ("withings_last_workout_type", "", "last logged workout"),
        ("withings_last_workout_duration", "min", "last workout duration"),
        ("sensor.calorie_balance", "kcal", "calorie balance (today)"),
        ("sensor.food_calories_consumed", "kcal", "lifetime kcal logged"),
    ]
    print("## Current vitals (HA)")
    for eid, unit, label in vitals:
        v = ha_state(eid)
        if v is not None and v not in ("unknown", "unavailable", "0"):
            print(f"- {label}: {v} {unit}".rstrip())
        else:
            print(f"- {label}: (no data)")
    print()

    print("## Weight history (last 7d)")
    wp = history_points("sensor.withings_weight")
    print("\n".join(wp) if wp else "(no weight history yet)")
    print()

    print("## Steps history (last 7d)")
    sp = history_points("sensor.pixel_8_daily_steps")
    print("\n".join(sp) if sp else "(no steps history yet)")
    print()

    print("## Prior weekly assessments (trend)")
    pa = prior_assessments()
    if pa:
        print("\n".join(pa))
    else:
        print("(none yet — this is the first week)")


if __name__ == "__main__":
    main()
