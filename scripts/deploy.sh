#!/usr/bin/env bash
# Deploy the Hermes LXC configuration from the build machine.
#
# Builds locally on the build machine (azrael), pushes the closure to the
# LXC via `nix copy`, then activates it via `nixos-rebuild switch`.
#
# Health-checked with auto-rollback.
#
# Usage:  scripts/deploy.sh [--snapshot] [--force-memory-gate] [--dry-run]
#
#   --snapshot        also snapshot the Proxmox CT first (need PROXMOXCT env)
#   --force-memorygate  bypass the 2048 MB memory gate (exit 42)
#   --dry-run         build locally but do not deploy to LXC
#
# Environment:
#   HERMES_HOST       override default address (default: root@hermes.lan)
#   HERMES_TAILSCALE_IP  alternate tailnet address
#   PROXMOX_CT        Proxmox container ID for --snapshot
#
set -euo pipefail

HOST="${HERMES_HOST:-root@192.168.193.158}"

DRY_RUN=false
SNAPSHOT=false
FORCE_MEMORY=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run)           DRY_RUN=true; shift ;;
    --snapshot)          SNAPSHOT=true; shift ;;
    --force-memorygate)  FORCE_MEMORY=true; shift ;;
    *) echo "unknown option $1" >&2; exit 1 ;;
  esac
done

echo "== deploying to $HOST =="

# ── 0. Proxmox snapshot ────────────────────────────────────────────────
if [[ "$SNAPSHOT" == "true" && -z "${PROXMOX_CT:-}" ]]; then
  echo "ERROR: PROXMOX_CT is required for --snapshot" >&2
  exit 1
fi

if [[ "$SNAPSHOT" == "true" ]]; then
  echo "== snapshotting Proxmox CT $PROXMOX_CT =="
  ssh root@proxmox "pvesm snapshot \"$PROXMOX_CT\" hermes-deploy-$(date +%s) \
    --description \"Deploy before $(date +%Y%m%dT%H%M%S)\""
  echo "== snapshot done =="
fi

# ── 1. Memory gate (--force-memorygate bypasses) ──────────────────────
if [[ "$FORCE_MEMORY" != "true" ]]; then
  echo "== checking free memory on $HOST =="
  FREE_MB=$(ssh "$HOST" 'free -t | awk \"/^total:/ { print \\$7 - \\$8 }\"')
  # If SwapFree is 0, just check MemAvailable
  SWAP_FREE=$(ssh "$HOST" 'free -m | awk \"/^Swap:/ { print \\$4 }\"')
  MEM_AVAIL=$(ssh "$HOST" 'awk \"/MemAvailable:/ { print int(\\$2/1024) }\" /proc/meminfo')

  # headroom = MemAvailable + SwapFree
  if [[ -z "$SWAP_FREE" || "$SWAP_FREE" == "0" ]]; then
    HEADROOM="$MEM_AVAIL"
  else
    HEADROOM=$(( MEM_AVAIL + SWAP_FREE ))
  fi

  echo "  MemAvailable: ${MEM_AVAIL} MB"
  echo "  SwapFree:     ${SWAP_FREE} MB"
  echo "  Headroom:     ${HEADROOM} MB"

  if [[ "$HEADROOM" -lt 2048 ]]; then
    echo "ERROR: headroom ${HEADROOM} MB < 2048 MB (exit 42)" >&2
    echo "Bypass with --force-memorygate" >&2
    exit 42
  fi
  echo "== memory gate passed =="
fi

# ── 2. Build the hermes config locally ────────────────────────────────
if "$DRY_RUN"; then
  echo "== dry-run: building locally =="
fi

echo "== building nixosConfigurations.hermes =="
NIX_BUILD_OUTPUT=$(nix build .#nixosConfigurations.hermes.config.system.build.toplevel \
  --print-out-paths --no-link 2>&1)

# Extract the resulting store path
STORE_PATH=$(echo "$NIX_BUILD_OUTPUT" | tail -1)

if [[ ! -d "$STORE_PATH" ]]; then
  echo "ERROR: build produced no output at $STORE_PATH" >&2
  exit 1
fi

echo "== build complete: $STORE_PATH =="

# ── 3. Push closure to hermes ─────────────────────────────────────────
ssh "$HOST" 'mkdir -p /nix/var/nix/gcroots/systems'

echo "== copying closure to $HOST =="
nix copy --to ssh://"$HOST" "$STORE_PATH"

echo "== closure copied =="

# ── 4. Activate on the LXC ────────────────────────────────────────────
if [[ "$DRY_RUN" == "true" ]]; then
  echo "== dry-run: skipping activation =="
  echo "== done =="
  exit 0
fi

echo "== activating on $HOST =="
ssh "$HOST" "$STORE_PATH/bin/switch-to-configuration switch 2>&1 | tee -a /var/log/hermes-deploy.log"

echo "== activation complete =="

# Sync the bot's workspace copy to the deployed repo.
# The bot self-deploys from /var/lib/hermes/workspace/hermes-deploy, which
# otherwise drifts from the system flake at /var/lib/hermes-deploy.
ssh "$HOST" 'git -C /var/lib/hermes/workspace/hermes-deploy reset --hard 2>/dev/null || true'

# ── 5. Health check ───────────────────────────────────────────────────
echo "== health check =="
ssh "$HOST" "
  # Verify hermes-agent is running
  systemctl is-active hermes-agent.service
  # Verify tailscale is up
  systemctl is-active tailscale.service
"
echo "== health check passed =="

# ── 6. Start the rollback watchdog ────────────────────────────────────
echo "== starting rollback watchdog =="
ssh "$HOST" "systemctl start hermes-rollback.service"

echo "== deploy complete =="
