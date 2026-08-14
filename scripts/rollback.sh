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

HOST="${HERMES_HOST:-root@${HERMES_TAILSCALE_IP:-hermes}}"
[[ $# -gt 0 ]] && HOST="$1"

echo "== rolling back one generation on $HOST =="
# hermes-rollback.service does both halves: git reset --hard HEAD~1 in
# /var/lib/hermes-deploy AND nixos-rebuild switch --rollback.
ssh "$HOST" "systemctl start hermes-rollback.service"

echo "== verifying =="
ssh "$HOST" "hermes-status"
