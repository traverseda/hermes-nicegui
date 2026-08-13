#!/usr/bin/env bash
# Test the deploy machinery locally before touching the real LXC.
#
#  * nix flake check  — validates the flake, builds toplevel/tarball, runs the
#                       NixOS integration test.
#  * nix run .#hermes-vm — boot the local test VM (interactive).
#
# Usage:  scripts/test.sh [check|vm]
set -euo pipefail

MODE="${1:-check}"

case "$MODE" in
  check)
    echo "== nix flake check =="
    nix flake check
    ;;
  vm)
    echo "== building & booting local test VM =="
    nix run .#hermes-vm
    ;;
  test)
    echo "== running integration test =="
    nix build .#checks.x86_64-linux.hermes-integration
    ;;
  *)
    echo "usage: $0 [check|vm|test]" >&2
    exit 1
    ;;
esac
