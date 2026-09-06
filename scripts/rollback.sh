#!/usr/bin/env bash
# Roll back the Hermes LXC to the previous NixOS generation.
#
# This is the last line of defence and should normally be unnecessary — the
# auto-rollback watchdog handles bad deploys. Use it when you want to step
# back one generation manually (e.g. after a bad config or a bot misfire).
#
# It rolls back exactly ONE generation per invocation (and steps the local
# git checkout in /var/lib/hermes-deploy back one commit). Run repeatedly to
# step further back, mirroring the "never more than one at a time" safety rule.
#
# Usage:  scripts/rollback.sh [--host hermes@<ip>]
set -euo pipefail

HOST="${HERMES_HOST:-root@${HERMES_TAILSCALE_IP:-hermes.lan}}"
[[ $# -gt 0 ]] && HOST="$1"

echo "== rolling back one generation on $HOST =="
# hermes-rollback.service does both halves: git reset --hard HEAD~1 in
# /var/lib/hermes-deploy AND nix-env --rollback + switch-to-configuration.
# R2 (ticket t_68443fdf): the service now runs DETACHED (transient
# hermes-rollback-run, launcher WITHOUT --collect) so it survives its own
# switch — which means `systemctl start` returns immediately and the verify
# step below would race a still-running rollback unless we wait for the
# transient first. A FAILED transient is deliberately kept inspectable.
ssh "$HOST" "systemctl start hermes-rollback.service"

# Wait for the detached rollback to finish before verifying (up to ~4
# minutes): keep polling while `systemctl is-active` prints active or
# activating; any other verdict (inactive/gone/failed) ends the loop.
ssh "$HOST" 'for i in $(seq 1 120); do
    st=$(systemctl is-active hermes-rollback-run.service 2>/dev/null) || true
    case "$st" in
      active|activating) sleep 2 ;;
      *) exit 0 ;;
    esac
  done'

# The kept-failed transient is the authoritative verdict: a rollback that
# died mid-flight must fail loudly here instead of "verifying" a broken box.
if ssh "$HOST" "systemctl is-failed hermes-rollback-run.service"; then
  echo "hermes-rollback: rollback transient FAILED on $HOST (see journalctl -u hermes-rollback-run)" >&2
  exit 1
fi

echo "== verifying =="
ssh "$HOST" "hermes-status"
