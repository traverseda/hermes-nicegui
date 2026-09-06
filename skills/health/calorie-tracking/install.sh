#!/usr/bin/env bash
# install.sh — install the calorie-tracking scripts/data into this box's
# runtime locations (the "content lane": cron scripts live in ~/.hermes/scripts,
# the foodlog tool lives in ~/workspace/foodlog, the log in
# ~/workspace/notes-and-research/fitness).
#
# Canonical copies live in this flake at skills/health/calorie-tracking/.
# After editing a script here, run this script to re-sync runtime copies:
#   bash skills/health/calorie-tracking/install.sh
#
# Deliberately NOT installed: data/fitness_targets.json (stale side file —
# see REVIEW.md; HA input_number.target_* is the source of truth).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${HOME:-/var/lib/hermes}"
SCRIPTS_DIR="$HOME_DIR/.hermes/scripts"
FOODLOG_DIR="$HOME_DIR/workspace/foodlog"
FITNESS_DIR="$HOME_DIR/workspace/notes-and-research/fitness"

mkdir -p "$SCRIPTS_DIR" "$FOODLOG_DIR" "$FITNESS_DIR"

echo "== cron/planner scripts -> $SCRIPTS_DIR =="
for f in fitness_snapshot.py fitness_planner.py fitness_targets.py fitness_daily_check.py weekly_fitness_collect.py reset-food-today.sh; do
  install -m 0755 "$HERE/scripts/$f" "$SCRIPTS_DIR/$f"
  echo "  $f"
done

echo "== foodlog tool + library -> $FOODLOG_DIR =="
install -m 0755 "$HERE/scripts/food_log.py" "$FOODLOG_DIR/food_log.py"
install -m 0644 "$HERE/data/foods.json" "$FOODLOG_DIR/foods.json"
echo "  food_log.py, foods.json"

echo "== food log history (seed, never overwrite) -> $FITNESS_DIR =="
if [ ! -f "$FITNESS_DIR/food-log.jsonl" ]; then
  install -m 0644 "$HERE/data/food-log.jsonl" "$FITNESS_DIR/food-log.jsonl"
  echo "  food-log.jsonl (seeded)"
else
  echo "  food-log.jsonl exists — left untouched"
fi

echo "done. (fitness_targets.json NOT installed — stale; see REVIEW.md)"
