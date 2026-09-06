#!/usr/bin/env bash
# install.sh — install the tunarr scripts into this box's runtime location
# (the "content lane": cron scripts live in ~/.hermes/scripts).
#
# Canonical copies live in this flake at skills/media/tunarr/.
# After editing a script here, run this script to re-sync runtime copies:
#   bash skills/media/tunarr/install.sh
#
# Deliberately NOT installed: data files (charters.md, schedules/, logos/,
# shows.json, etc.) — those are user data that lives on the box, not in the
# flake.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${HOME:-/var/lib/hermes}"
SCRIPTS_DIR="$HOME_DIR/.hermes/scripts"
DATA_DIR="$HOME_DIR/.hermes/data/tunarr"

mkdir -p "$SCRIPTS_DIR" "$DATA_DIR"

echo "== tunarr scripts -> $SCRIPTS_DIR =="
for f in tunarr tunarr-channel-audit.py tunarr-monthly-diff tunarr-find-fits.py generate-channel-logo.py; do
  install -m 0755 "$HERE/scripts/$f" "$SCRIPTS_DIR/$f"
  echo "  $f"
done

echo "== data dir -> $DATA_DIR =="
echo "  (charters.md, schedules/, logos/, shows.json etc. NOT copied — user data lives on the box, not in the flake)"

echo "== making tunarr scripts executable =="
for f in tunarr tunarr-channel-audit.py tunarr-monthly-diff tunarr-find-fits.py generate-channel-logo.py; do
  chmod +x "$SCRIPTS_DIR/$f"
  echo "  chmod +x $f"
done

echo "done."
