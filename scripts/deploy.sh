#!/usr/bin/env bash
# Deploy the Hermes LXC to a new NixOS generation.
#
# Runs from a build machine. Uses `nixos-rebuild switch --target-host`, so:
#   * builds the config locally (hermetic, reproducible),
#   * copies the closure to the LXC,
#   * activates it there as a new generation (rollback = previous generation).
#
# Safety: the deploy is a new immutable generation; the previous generation is
# preserved in the store and can be re-activated at any time (or via the
# auto-rollback watchdog on the target). Optionally snapshots the CT first.
#
# Usage:
#   scripts/deploy.sh [--host hermes@<ip>] [--snapshot] [--no-check]
#
# Env:
#   HERMES_HOST   ssh target (default: hermes@hermes.tailnet  / $HERMES_TAILSCALE_IP)
set -euo pipefail

HOST="${HERMES_HOST:-root@${HERMES_TAILSCALE_IP:-hermes}}"
SNAPSHOT=false
CHECK=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --snapshot) SNAPSHOT=true; shift ;;
    --no-check) CHECK=false; shift ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

PROXMOX_CT="${PROXMOX_CT:-}"   # e.g. 105, used only with --snapshot

cd "$(dirname "$0")/.."

echo "== git state =="
git fetch origin
git status --short
if [ -n "$(git status --porcelain)" ]; then
  echo "WARNING: working tree has uncommitted changes" >&2
fi

if [ "$SNAPSHOT" = true ]; then
  if [ -z "$PROXMOX_CT" ]; then
    echo "error: --snapshot requires PROXMOX_CT=<ctid> env var" >&2
    exit 1
  fi
  echo "== snapshotting Proxmox CT $PROXMOX_CT (external rollback safety net) =="
  ssh root@${PROXMOX_HOST:-pve} "pct snapshot $PROXMOX_CT --description hermes-deploy $(git rev-parse --short HEAD)"
fi

echo "== building & switching on $HOST =="
nixos-rebuild switch \
  --flake .#hermes \
  --target-host "$HOST" \
  --use-remote-sudo \
  ${CHECK:+--print-build-logs}

echo "== running health check + auto-rollback watchdog on target =="
if [ "$CHECK" = true ]; then
  # The watchdog asks the agent itself whether it's healthy; if the new
  # generation is unwell it rolls back exactly one generation.
  ssh "$HOST" "systemctl start hermes-watchdog.service" \
    || { echo "deploy health check FAILED (watchdog may have rolled back)"; exit 1; }
  ssh "$HOST" "hermes-status"
fi

echo "deploy complete"
