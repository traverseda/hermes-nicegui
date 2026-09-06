#!/usr/bin/env python3
"""Fitness Targets Calculator — reads body composition from HA, computes TDEE, pushes targets."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HA_BASE = "http://hearth.lan"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
ACTIVITY_MULTIPLIER = 1.55

SENSORS = {
    "fat_free_mass": "sensor.withings_fat_free_mass",
    "weight": "sensor.withings_weight",
    "fat_ratio": "sensor.withings_fat_ratio",
}

HELPERS = {
    "calories": "input_number.target_calories",
    "protein": "input_number.target_protein",
}

CALORIE_RANGE = (1500, 4000)
PROTEIN_RANGE = (100, 300)


def load_token():
    token = os.environ.get("HASS_TOKEN")
    if token:
        return token
    env_path = os.path.expanduser("~/.hermes/.env")
    if not os.path.isfile(env_path):
        print("ERROR: HASS_TOKEN not set and ~/.hermes/.env not found", file=sys.stderr)
        sys.exit(1)
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("HASS_TOKEN="):
                token = line.split("=", 1)[1].strip().strip("'\"")
                if token:
                    return token
    print("ERROR: HASS_TOKEN not found in env or ~/.hermes/.env", file=sys.stderr)
    sys.exit(1)


def ha_request(method, path, token, body=None):
    url = f"{HA_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} from HA: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Connection error: {e.reason}", file=sys.stderr)
        sys.exit(1)


def get_sensor(token, entity_id):
    state = ha_request("GET", f"/api/states/{entity_id}", token)
    return state.get("state")


def read_mode():
    mode_path = os.path.expanduser("~/.hermes/scripts/fitness_mode.json")
    if os.path.isfile(mode_path):
        try:
            with open(mode_path) as f:
                data = json.load(f)
            mode = data.get("mode", "").lower()
            if mode in ("bulk", "cut"):
                return mode
        except (json.JSONDecodeError, OSError):
            pass
    return "bulk"


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def calculate_targets(lean_mass_kg, mode):
    bmr = 370 + (21.6 * lean_mass_kg)
    tdee = bmr * ACTIVITY_MULTIPLIER
    if mode == "bulk":
        calories = tdee + 400
        protein = lean_mass_kg * 2.0
    elif mode == "cut":
        calories = tdee - 400
        protein = lean_mass_kg * 2.2
    else:
        calories = tdee
        protein = lean_mass_kg * 1.8
    calories = clamp(int(round(calories)), *CALORIE_RANGE)
    protein = clamp(int(round(protein)), *PROTEIN_RANGE)
    return calories, protein, bmr, tdee


def main():
    parser = argparse.ArgumentParser(description="Calculate fitness calorie/protein targets")
    parser.add_argument("--dry-run", action="store_true", help="Print targets without calling HA")
    parser.add_argument("--mode", choices=["bulk", "cut"], help="Override mode from config file")
    args = parser.parse_args()

    mode = args.mode if args.mode else read_mode()
    token = load_token()

    lean_mass_str = get_sensor(token, SENSORS["fat_free_mass"])
    if lean_mass_str is None:
        print(f"ERROR: {SENSORS['fat_free_mass']} unavailable", file=sys.stderr)
        sys.exit(1)
    lean_mass_kg = float(lean_mass_str)

    calories, protein, bmr, tdee = calculate_targets(lean_mass_kg, mode)

    print(f"Mode:       {mode}")
    print(f"Lean mass:  {lean_mass_kg:.2f} kg")
    print(f"BMR:        {bmr:.0f} kcal")
    print(f"TDEE:       {tdee:.0f} kcal")
    print(f"Calories:   {calories}")
    print(f"Protein:    {protein} g")

    if args.dry_run:
        print("\n[dry-run] Targets not pushed to HA")
        return

    ha_request(
        "POST",
        "/api/services/input_number/set_value",
        token,
        {"entity_id": HELPERS["calories"], "value": calories},
    )
    ha_request(
        "POST",
        "/api/services/input_number/set_value",
        token,
        {"entity_id": HELPERS["protein"], "value": protein},
    )
    print("\nTargets pushed to HA")


if __name__ == "__main__":
    main()
