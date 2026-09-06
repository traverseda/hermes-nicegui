# Fitness Targets Calculator — Spec

## Goal
Create `fitness_targets.py` — a Python script that reads body composition from HA, calculates calorie/protein targets using Katch-McArdle TDEE, and pushes the targets to HA input_number helpers.

## Body composition sensors (Withings scale, read via HA REST API)
- `sensor.withings_fat_ratio` — body fat % (e.g. 16.572)
- `sensor.withings_weight` — body weight in kg (e.g. 72.049)
- `sensor.withings_fat_free_mass` — lean mass in kg (e.g. 60.11)

## HA helpers to set (via `input_number.set_value`)
- `input_number.target_calories` (kcal, integer)
- `input_number.target_protein` (grams, integer)

## Mode determination
- Read mode from `~/.hermes/scripts/fitness_mode.json`: `{"mode": "bulk"}` or `{"mode": "cut"}`
- If file doesn't exist or has no valid mode, default to "bulk"
- The user can manually change mode by editing this file

## Katch-McArdle TDEE calculation
```
lean_mass_kg = sensor.withings_fat_free_mass (preferred, in kg)
BMR = 370 + (21.6 × lean_mass_kg)
activity_multiplier = 1.55 (moderate activity — user goes to gym 2x/week)
TDEE = BMR × activity_multiplier
```

### Targets by mode
- **Bulk**: calories = TDEE + 400, protein = lean_mass_kg × 2.0
- **Cut**: calories = TDEE - 400, protein = lean_mass_kg × 2.2 (minimum 2.0g/kg)
- **Maintain** (if implemented): calories = TDEE, protein = lean_mass_kg × 1.8

### Sanity clamps
- Calories: min 1500, max 4000
- Protein: min 100, max 300

## Script interface
```
python3 fitness_targets.py [--dry-run] [--mode bulk|cut]
```
- `--dry-run`: print what would be set, don't call HA
- `--mode`: override the mode from the JSON file

## Output
- Print a summary to stdout (mode, lean mass, BMR, TDEE, targets)
- Exit 0 on success

## HA API details
- Base URL: `http://hearth.lan` (NEVER https — Cloudflare blocks scripts)
- Auth: Bearer token from env HASS_TOKEN (loaded from `~/.hermes/.env`)
- User-Agent header: `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36`
- GET sensor: `GET /api/states/{entity_id}`
- SET helper: `POST /api/services/input_number/set_value` with `{"entity_id": "...", "value": N}`
- Standard stdlib urllib only, no external deps

## File locations
- Script goes in: `/var/lib/hermes-deploy/skills/health/calorie-tracking/scripts/fitness_targets.py`
- Mode config: `~/.hermes/scripts/fitness_mode.json`
- Install target: `~/.hermes/scripts/fitness_targets.py`

## SANDBOX RULE
Never read/ls/cat any absolute path outside the build dir; build against the schemas in this spec. The deployed copies get replaced at deploy time.
