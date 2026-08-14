#!/usr/bin/env bash
# Deploy the Hermes LXC to a new NixOS generation.
#
# Runs from a build machine. Uses `nixos-rebuild switch --target-host`, so:
#   * builds the config locally (hermetic, reproducible),
#   * copies the closure to the LXC,
#   * activates it there as a new generation (rollback = previous generation).
#
# The flake repo has NO git remote (by design): the build machine is the
# source of truth, and `deploy.sh` also rsyncs the working tree into
# /var/lib/hermes-deploy on the LXC so the bot's own self-deploy
# (hermes-deploy.service) builds from the same source it was activated from.
# External backup of the repo is handled out-of-band.
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
git status --short
if [ -n "$(git status --porcelain)" ]; then
  echo "WARNING: working tree has uncommitted changes (they WILL be deployed)" >&2
fi

if [ "$SNAPSHOT" = true ]; then
  if [ -z "$PROXMOX_CT" ]; then
    echo "error: --snapshot requires PROXMOX_CT=<ctid> env var" >&2
    exit 1
  fi
  echo "== snapshotting Proxmox CT $PROXMOX_CT (external rollback safety net) =="
  ssh root@${PROXMOX_HOST:-pve} "pct snapshot $PROXMOX_CT --description hermes-deploy $(git rev-parse --short HEAD)"
fi

echo "== syncing flake repo to $HOST:/var/lib/hermes-deploy (no remote; rsync is the transport) =="
# Push the working tree (including .git history and submodules) to the LXC so
# hermes-deploy.service / hermes-rollback.service operate on the same source
# that this deploy activated. Exclusions:
#   .git           the LXC keeps its own local ledger (bot commits / rollbacks)
#   vendor/        editable submodules on the LXC must survive (their own repos)
#   state/         deploy bookkeeping (last-known-good) lives here
#   result*, *.qcow2  build artifacts
#   secrets/lxc-host-ed25519  the LXC's own host key (gitignored; never re-copy)
rsync -a --delete \
  --exclude '/.git/' \
  --exclude '/vendor/' \
  --exclude '/state/' \
  --exclude '/result' \
  --exclude '/result-*' \
  --exclude '*.qcow2' \
  --exclude '*.raw' \
  --exclude '*.img' \
  --exclude '/secrets/lxc-host-ed25519' \
  ./ "$HOST:/var/lib/hermes-deploy/"

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
