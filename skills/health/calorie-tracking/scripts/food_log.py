#!/usr/bin/env python3
"""food_log.py — food logging CLI for Hermes → Home Assistant.

Logs meals to a permanent JSONL file and pushes today's + lifetime totals
to Home Assistant input_number helpers via REST.
"""

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

LOG_PATH = os.environ.get(
    "FOOD_LOG_PATH", "/var/lib/hermes/workspace/notes-and-research/fitness/food-log.jsonl"
)
FOODS_PATH = os.environ.get(
    "FOODS_PATH", "/var/lib/hermes/workspace/foodlog/foods.json"
)
ENV_PATH = os.environ.get(
    "HASS_ENV_PATH", os.path.expanduser("~/.hermes/.env")
)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = 10

HELPERS_TODAY = {
    "kcal": "input_number.food_calories_today",
    "protein": "input_number.food_protein_today",
    "carbs": "input_number.food_carbs_today",
    "fat": "input_number.food_fat_today",
}
HELPERS_LIFETIME = {
    "kcal": "input_number.food_calories_lifetime",
    "protein": "input_number.food_protein_lifetime",
    "carbs": "input_number.food_carbs_lifetime",
    "fat": "input_number.food_fat_lifetime",
}
# "Missing" signal: on = at least one entry logged today. Template sensors
# for today's totals render `unknown` (—) while this is off, so days with no
# record show as missing instead of a misleading 0.
INPUT_BOOLEAN_LOGGED = "input_boolean.food_logged_today"
NUTRIENTS = ("kcal", "protein", "carbs", "fat")
VALID_CONFIDENCE = ("weighed", "estimated", "guessed")


def err(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def usage_err(parser, msg):
    print(f"error: {msg}", file=sys.stderr)
    parser.print_usage(sys.stderr)
    sys.exit(2)


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


def load_entries():
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
                    err(f"corrupt log line {lineno}: {e}")
                entries.append(entry)
    except OSError as e:
        err(f"cannot read {LOG_PATH}: {e}")
    return entries


def load_foods():
    """Load the name→macros library as a dict. Returns {} if the file is missing."""
    if not os.path.exists(FOODS_PATH):
        return {}
    try:
        with open(FOODS_PATH) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        err(f"cannot read {FOODS_PATH}: {e}")
    if not isinstance(data, dict):
        err(f"{FOODS_PATH} must contain a JSON object")
    return data


def save_foods(foods):
    """Persist the library. Skipped under DRY_RUN, printing what would happen."""
    if os.environ.get("DRY_RUN") == "1":
        print(
            f"DRY_RUN: would write {len(foods)} foods to {FOODS_PATH}"
        )
        return
    try:
        with open(FOODS_PATH, "w") as fh:
            json.dump(foods, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except OSError as e:
        err(f"cannot write {FOODS_PATH}: {e}")


def lookup_food(foods, query):
    """Match query against foods.json keys. Returns (key, record) or None.

    Exact case-insensitive match wins; otherwise a lowercase substring match
    is used if it hits exactly one key. Multiple matches exit 2 with the
    candidate list.
    """
    ql = query.lower()
    for key in foods:
        if key.lower() == ql:
            return key, foods[key]
    matches = [k for k in foods if ql in k.lower()]
    if len(matches) == 1:
        return matches[0], foods[matches[0]]
    if len(matches) > 1:
        print(f"AMBIGUOUS: multiple foods match \"{query}\":")
        for k in sorted(matches):
            print(f"  {k}")
        sys.exit(2)
    return None


def remember_items(items, confidence, total_weight=None):
    """Upsert each non-zero-kcal item into foods.json. DRY_RUN-safe.

    total_weight: grams for the whole item. When set, stored into each saved
    record; when None, any existing total_weight on the record is preserved
    (upsert semantics — never dropped).
    """
    foods = load_foods()
    today = datetime.date.today().isoformat()
    remembered = []
    for item in items:
        if int(item.get("kcal", 0)) == 0:
            continue
        name = item["name"]
        existing = foods.get(name)
        if confidence == "weighed":
            new_conf = "weighed"
        elif existing and existing.get("confidence"):
            new_conf = existing.get("confidence")
        else:
            new_conf = confidence
        record = {
            "kcal": int(item.get("kcal", 0)),
            "protein": int(item.get("protein", 0)),
            "carbs": int(item.get("carbs", 0)),
            "fat": int(item.get("fat", 0)),
            "confidence": new_conf,
            "source": today,
        }
        if total_weight is not None:
            record["total_weight"] = int(total_weight)
        elif existing and "total_weight" in existing:
            record["total_weight"] = existing["total_weight"]
        foods[name] = record
        remembered.append((name, foods[name]))
    save_foods(foods)
    for name, rec in remembered:
        extra = f" total_weight={rec['total_weight']}" if "total_weight" in rec else ""
        print(
            f"remembered \"{name}\" kcal={rec['kcal']} "
            f"protein={rec['protein']} carbs={rec['carbs']} fat={rec['fat']} "
            f"confidence={rec['confidence']}{extra}"
        )


def read_lines_and_entries():
    """Return (raw_lines, entries, lineno_map); entries[i] maps to raw line lineno_map[i]."""
    if not os.path.exists(LOG_PATH):
        return [], [], {}
    try:
        with open(LOG_PATH) as fh:
            raw_lines = fh.readlines()
    except OSError as e:
        err(f"cannot read {LOG_PATH}: {e}")
    entries = []
    lineno_map = {}
    for lineno, raw in enumerate(raw_lines):
        line = raw.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            err(f"corrupt log line {lineno + 1}: {e}")
        lineno_map[len(entries)] = lineno
        entries.append(entry)
    return raw_lines, entries, lineno_map


def empty_totals():
    return {k: 0 for k in NUTRIENTS}


def sum_totals(entries):
    totals = empty_totals()
    for entry in entries:
        for k in NUTRIENTS:
            totals[k] += int(entry.get("totals", {}).get(k, 0))
    return totals


def today_totals(entries):
    today = datetime.date.today().isoformat()
    return sum_totals(e for e in entries if e.get("day") == today)


def push_helpers(values):
    """values: iterable of (entity_id, value). Returns True on full success."""
    if os.environ.get("DRY_RUN") == "1":
        for entity, value in values:
            print(f"DRY_RUN: would push {entity}={value}")
        return True

    base_url, token = load_credentials()
    endpoint = base_url + "/api/services/input_number/set_value"
    ok = True
    for entity, value in values:
        body = json.dumps({"entity_id": entity, "value": value}).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            print(f"error: failed to push {entity}: {e}", file=sys.stderr)
            ok = False
    return ok


def push_logged_flag(entries):
    """Set INPUT_BOOLEAN_LOGGED = on iff any entry exists for today.

    Returns True on success. DRY_RUN-aware like push_helpers.
    """
    today = datetime.date.today().isoformat()
    logged = any(e.get("day") == today for e in entries)
    action = "turn_on" if logged else "turn_off"

    if os.environ.get("DRY_RUN") == "1":
        print(f"DRY_RUN: would push {INPUT_BOOLEAN_LOGGED}={action}")
        return True

    base_url, token = load_credentials()
    endpoint = f"{base_url}/api/services/input_boolean/{action}"
    body = json.dumps({"entity_id": INPUT_BOOLEAN_LOGGED}).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        print(f"error: failed to push {INPUT_BOOLEAN_LOGGED}: {e}", file=sys.stderr)
        return False
    print(f"pushed {INPUT_BOOLEAN_LOGGED}={action}")
    return True


def push_totals(entries):
    t_today = today_totals(entries)
    t_lifetime = sum_totals(entries)
    pushes = [(HELPERS_TODAY[k], t_today[k]) for k in NUTRIENTS]
    pushes += [(HELPERS_LIFETIME[k], t_lifetime[k]) for k in NUTRIENTS]
    if not push_helpers(pushes):
        err("push failed (see above)")
    if not push_logged_flag(entries):
        err("push failed (see above)")
    print(f"pushed today kcal={t_today['kcal']} lifetime={t_lifetime['kcal']}")


def cmd_add(args, parser):
    entry_confidence = args.confidence
    if args.weight is not None:
        if args.weight <= 0:
            usage_err(parser, "--weight must be a positive integer")
        if not args.food:
            usage_err(parser, "--weight requires --food")
        if args.remember or args.library_only:
            usage_err(
                parser,
                "--weight cannot be combined with --remember/--library-only",
            )
        if args.total_weight is not None:
            usage_err(parser, "--weight and --total-weight are mutually exclusive")
    if args.total_weight is not None:
        if args.total_weight <= 0:
            usage_err(parser, "--total-weight must be a positive integer")
        if not (args.remember or args.library_only):
            usage_err(
                parser,
                "--total-weight requires --remember or --library-only",
            )
    if args.library_only and args.food and not args.total_weight:
        usage_err(parser, "--library-only cannot be combined with --food")
    if args.food:
        if args.item or args.kcal or args.protein or args.carbs or args.fat:
            usage_err(
                parser,
                "--food cannot be combined with --item/--kcal/--protein/--carbs/--fat",
            )
        foods = load_foods()
        match = lookup_food(foods, args.food)
        if match is None:
            print(f"NOT_IN_LIBRARY: {args.food}")
            sys.exit(2)
        key, record = match
        names = [key]
        entry_confidence = record.get("confidence", args.confidence)
        if args.weight is not None:
            total_weight = record.get("total_weight")
            if total_weight is None:
                print(
                    f'NO_TOTAL_WEIGHT: "{key}" has no total_weight; set it with: '
                    f'food_log.py add --food "{key}" --total-weight <grams> '
                    f"--library-only"
                )
                sys.exit(2)
            ratio = args.weight / int(total_weight)
            scaled = {k: round(record.get(k, 0) * ratio) for k in NUTRIENTS}
            ints = {k: [scaled[k]] for k in NUTRIENTS}
            print(
                f"matched \"{args.food}\" → \"{key}\" {args.weight}g of "
                f"{total_weight}g kcal={scaled['kcal']} protein={scaled['protein']} "
                f"carbs={scaled['carbs']} fat={scaled['fat']}"
            )
        else:
            ints = {k: [record.get(k, 0)] for k in NUTRIENTS}
            print(
                f"matched \"{args.food}\" → \"{key}\" "
                f"kcal={record.get('kcal', 0)} protein={record.get('protein', 0)} "
                f"carbs={record.get('carbs', 0)} fat={record.get('fat', 0)}"
            )
    else:
        names = args.item or []
        ints = {k: args.__dict__.get(k) or [] for k in NUTRIENTS[1:]} | {
            "kcal": args.kcal or []
        }
        counts = [len(names)] + [len(v) for v in ints.values()]
        if not names:
            usage_err(parser, "add requires at least one --item")
        if len(set(counts)) != 1:
            usage_err(
                parser,
                "each --item needs matching --kcal/--protein/--carbs/--fat "
                "(same number of each flag)",
            )
    if entry_confidence not in VALID_CONFIDENCE:
        usage_err(
            parser,
            f"--confidence must be one of {', '.join(VALID_CONFIDENCE)}",
        )

    items = []
    totals = empty_totals()
    try:
        for i, name in enumerate(names):
            item = {k: int(ints[k][i]) for k in NUTRIENTS}
            if args.weight is not None:
                item["weight"] = args.weight
            for k in NUTRIENTS:
                if item[k] < 0:
                    raise ValueError(f"{k} must be >= 0")
                totals[k] += item[k]
            items.append({"name": name, **item})
    except ValueError as e:
        usage_err(parser, f"invalid nutrient value: {e}")

    if args.library_only:
        remember_items(items, entry_confidence, args.total_weight)
        return

    now = datetime.datetime.now().astimezone()
    entry = {
        "ts": now.isoformat(timespec="seconds"),
        "day": now.date().isoformat(),
        "items": items,
        "totals": totals,
        "confidence": entry_confidence,
    }

    if os.environ.get("DRY_RUN") == "1":
        print(f"DRY_RUN: would append entry for {entry['day']}")
    else:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        try:
            with open(LOG_PATH, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError as e:
            err(f"cannot write {LOG_PATH}: {e}")

    if args.remember:
        remember_items(items, entry_confidence, args.total_weight)

    entries = load_entries()
    t_today = today_totals(entries)
    t_lifetime = sum_totals(entries)

    pushes = [(HELPERS_TODAY[k], t_today[k]) for k in NUTRIENTS]
    pushes += [(HELPERS_LIFETIME[k], t_lifetime[k]) for k in NUTRIENTS]

    if not push_helpers(pushes):
        err("push failed (see above)")
    if not push_logged_flag(entries):
        err("push failed (see above)")
    print(f"pushed today kcal={t_today['kcal']} lifetime={t_lifetime['kcal']}")


def cmd_foods(args):
    foods = load_foods()
    if args.search:
        ql = args.search.lower()
        foods = {k: v for k, v in foods.items() if ql in k.lower()}
    if not foods:
        print("(empty library)")
    for name in sorted(foods):
        rec = foods[name]
        print(
            f"{name} | {rec.get('kcal', 0)} | {rec.get('protein', 0)} | "
            f"{rec.get('carbs', 0)} | {rec.get('fat', 0)} | "
            f"{rec.get('total_weight', '-')} | "
            f"{rec.get('confidence', '')}"
        )


def cmd_today(args):
    entries = load_entries()
    t = today_totals(entries)
    print(f"kcal={t['kcal']} protein={t['protein']} carbs={t['carbs']} fat={t['fat']}")


def cmd_reset(args):
    pushes = [(entity, 0) for entity in HELPERS_TODAY.values()]
    if not push_helpers(pushes):
        err("push failed (see above)")
    if not push_logged_flag([]):
        err("push failed (see above)")
    print("pushed today kcal=0 protein=0 carbs=0 fat=0 (reset, logged flag off)")


def cmd_status(args):
    entries = load_entries()
    t_today = today_totals(entries)
    t_lifetime = sum_totals(entries)
    print(f"log path: {LOG_PATH}")
    print(f"entries: {len(entries)}")
    print(
        "today: kcal={kcal} protein={protein} carbs={carbs} fat={fat}".format(**t_today)
    )
    print(
        "lifetime: kcal={kcal} protein={protein} carbs={carbs} fat={fat}".format(
            **t_lifetime
        )
    )


def cmd_edit(args, parser):
    day = args.day or datetime.date.today().isoformat()
    raw_lines, entries, lineno_map = read_lines_and_entries()
    target = None
    for i, entry in enumerate(entries):
        if entry.get("day") == day:
            target = i
    if target is None:
        usage_err(parser, f"no entry found for day {day}")

    items = entries[target].get("items", [])
    if not 0 <= args.item < len(items):
        usage_err(parser, f"item index {args.item} out of range (0..{len(items) - 1})")

    if args.remove:
        del items[args.item]
    else:
        if args.name is not None:
            items[args.item]["name"] = args.name
        for k in NUTRIENTS:
            value = args.__dict__.get(k)
            if value is not None:
                items[args.item][k] = int(value)

    if items:
        totals = empty_totals()
        for item in items:
            for k in NUTRIENTS:
                totals[k] += int(item.get(k, 0))
        entries[target]["totals"] = totals
        raw_lines[lineno_map[target]] = json.dumps(entries[target]) + "\n"
    else:
        del raw_lines[lineno_map[target]]

    if os.environ.get("DRY_RUN") == "1":
        print(f"DRY_RUN: would rewrite entry for {day}")
    else:
        try:
            with open(LOG_PATH, "w") as fh:
                fh.writelines(raw_lines)
        except OSError as e:
            err(f"cannot write {LOG_PATH}: {e}")

    push_totals(load_entries())


def cmd_undo(args, parser):
    raw_lines, entries, lineno_map = read_lines_and_entries()
    if not entries:
        usage_err(parser, "log is empty, nothing to undo")
    removed = entries[-1]
    if os.environ.get("DRY_RUN") == "1":
        print(
            f"DRY_RUN: would remove {removed.get('day')} "
            f"kcal={removed.get('totals', {}).get('kcal', 0)}"
        )
    else:
        del raw_lines[lineno_map[len(entries) - 1]]
        try:
            with open(LOG_PATH, "w") as fh:
                fh.writelines(raw_lines)
        except OSError as e:
            err(f"cannot write {LOG_PATH}: {e}")
        print(
            f"removed {removed.get('day')} "
            f"kcal={removed.get('totals', {}).get('kcal', 0)}"
        )
    push_totals(load_entries())


def build_parser():
    parser = argparse.ArgumentParser(prog="food_log.py")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_add = sub.add_parser("add", help="append a meal and push totals")
    p_add.add_argument("--item", action="append", metavar="DESC")
    for k in NUTRIENTS:
        p_add.add_argument(f"--{k}", type=int, action="append", metavar="N")
    p_add.add_argument(
        "--confidence",
        choices=VALID_CONFIDENCE,
        default="estimated",
        help=f"one of {', '.join(VALID_CONFIDENCE)} (default: estimated)",
    )
    p_add.add_argument(
        "--food",
        metavar="NAME",
        help="log a previously remembered meal from foods.json (case-insensitive, "
        "fuzzy match); overrides --item/--kcal/--protein/--carbs/--fat",
    )
    p_add.add_argument(
        "--remember",
        action="store_true",
        help="save this meal's name+macros into foods.json for fast re-logging",
    )
    p_add.add_argument(
        "--library-only",
        action="store_true",
        help="only remember the items into foods.json; do not append to the log "
        "or push Home Assistant helpers",
    )
    p_add.add_argument(
        "--weight",
        type=int,
        metavar="N",
        help="log a weighed portion of a --food library entry, scaling its macros "
        "by N / total_weight (requires --food; the entry must have total_weight)",
    )
    p_add.add_argument(
        "--total-weight",
        type=int,
        metavar="N",
        help="set the whole-item weight in grams on remembered library records "
        "(requires --remember or --library-only)",
    )

    p_foods = sub.add_parser("foods", help="list the foods.json library")
    p_foods.add_argument(
        "--search", metavar="TERM", help="only show names containing TERM"
    )

    sub.add_parser("today", help="print today totals from the log")
    sub.add_parser("reset", help="zero the *_today helpers")
    sub.add_parser("status", help="print log path, counts and totals")

    p_edit = sub.add_parser("edit", help="edit or remove an item and re-push totals")
    p_edit.add_argument("--day", metavar="YYYY-MM-DD", help="day to edit (default: today)")
    p_edit.add_argument("--item", type=int, default=0, metavar="N", help="item index (default: 0)")
    p_edit.add_argument("--remove", action="store_true", help="remove the item instead of editing it")
    p_edit.add_argument("--name", metavar="NAME", help="new item name")
    for k in NUTRIENTS:
        p_edit.add_argument(f"--{k}", type=int, metavar="N", help=f"new {k} value")

    sub.add_parser("undo", help="remove the last entry and re-push totals")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.mode == "add":
        cmd_add(args, parser)
    elif args.mode == "foods":
        cmd_foods(args)
    elif args.mode == "today":
        cmd_today(args)
    elif args.mode == "reset":
        cmd_reset(args)
    elif args.mode == "status":
        cmd_status(args)
    elif args.mode == "edit":
        cmd_edit(args, parser)
    elif args.mode == "undo":
        cmd_undo(args, parser)


if __name__ == "__main__":
    main()
