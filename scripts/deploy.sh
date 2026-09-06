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
#   scripts/deploy.sh [--host hermes@<ip>] [--snapshot] [--no-check] \
#     [--force-memory-gate]
#
# Env:
#   HERMES_HOST   ssh target (default: hermes@hermes.tailnet  / $HERMES_TAILSCALE_IP)
#   MEMORY_GATE_FLOOR_MB  target memory-gate floor in MB (default: 2048)
set -euo pipefail

# Reach the box over the LAN (hermes.lan). The tailnet IP is NOT usable for
# deploy: the box runs tailscale with `--ssh`, and the tailnet SSH policy
# rejects every user on this node, so `root@hermes` (which resolves to the
# tailnet IP) is refused. `root@hermes.lan` goes straight to the box's own
# sshd, where the operator key from hosts/hermes/configuration.nix works.
# Override with HERMES_HOST=<user@host> or HERMES_TAILSCALE_IP=<ip>.
HOST="${HERMES_HOST:-root@${HERMES_TAILSCALE_IP:-hermes.lan}}"
SNAPSHOT=false
CHECK=true
FORCE_GATE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --snapshot) SNAPSHOT=true; shift ;;
    --no-check) CHECK=false; shift ;;
    --force-memory-gate) FORCE_GATE=true; shift ;;
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

# R2 (ticket t_68443fdf): TARGET-side memory gate. The 2026-08-26 wedge
# (load 9.37, host bounced) happened on the LXC — the switch TARGET — not on
# this build machine, so measure the box's MemAvailable+SwapFree headroom
# before asking it to activate a new generation. Same floor and exit code as
# the on-box gate in modules/hermes-deploy.nix.
if [ "$FORCE_GATE" = true ]; then
  echo "WARNING: skipping target memory gate (--force-memory-gate)"
else
  echo "== memory gate on $HOST =="
  # Sum MemAvailable + SwapFree remotely; awk keeps it portable (no bc).
  # `|| true` lets an unreachable box fall through to the :-0 guard below
  # instead of dying with ssh's exit code.
  TARGET_MB=$(ssh "$HOST" "awk '/^MemAvailable:/ { printf \"%d\", \$2/1024; f=1 } END { if (!f) print 0 }' /proc/meminfo; awk '/^SwapFree:/ { printf \"%d\", \$2/1024 }' /proc/meminfo" | awk '{s+=$1} END {print s}') || true
  GATE_FLOOR="${MEMORY_GATE_FLOOR_MB:-2048}"
  if [ "${TARGET_MB:-0}" -lt "$GATE_FLOOR" ]; then
    echo "ABORT: target headroom ${TARGET_MB:-0}MB < ${GATE_FLOOR}MB (rebuild wedge risk, see t_68443fdf)"
    echo "Retry when quiet, or bypass: $0 --force-memory-gate ..."
    exit 42
  fi
  echo "target headroom: ${TARGET_MB:-0}MB - OK"
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
