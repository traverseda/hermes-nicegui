#!/usr/bin/env bash
# Rebuild the hermes-agent fork with this repo's local patches applied, and
# point the flake at it.
#
# Why a fork: the hermes-agent flake builds from its own source tree
# (nix/lib.nix hardcodes repoRoot = ./..) and its package takes no src/patches
# override hook, so there's no clean way to inject patches into a pinned
# github: input. Instead the patch CONTENT lives in ./patches (versioned here,
# rollback-safe) and is applied here onto the pinned upstream rev, committed to
# the fork's `patched` branch, and pinned via flake.lock. See patches/README.md.
#
# Usage:
#   HERMES_FORK=github:you/hermes-agent \
#   HERMES_FORK_REMOTE=git@github.com:you/hermes-agent.git \
#     scripts/patch-hermes.sh [--dry-run] [--build] [--update-upstream REV]
#
# Options:
#   --dry-run            apply patches to a temp clone and print the resulting
#                        commit, but do NOT push or touch flake.nix/flake.lock.
#   --build              after relocking, verify the toplevel still builds
#                        (nix build .#hermes.config.system.build.toplevel).
#   --update-upstream REV  record REV as the new upstream base
#                        (patches/upstream.lock) before rebuilding the patchset.
#
# Env:
#   HERMES_FORK          flake input URL of your fork, base repo only, no rev:
#                        e.g. github:you/hermes-agent. The built rev is appended.
#   HERMES_FORK_REMOTE   git URL to push the `patched` branch to,
#                        e.g. git@github.com:you/hermes-agent.git.
#   HERMES_UPSTREAM      upstream git URL (default: https://github.com/NousResearch/hermes-agent)
#
# Rollback: `git revert` the flake.nix/flake.lock change and redeploy — the
# previous generation is a normal Nix generation. To drop a patch, delete its
# .patch file and re-run this script.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

DRY_RUN=false
BUILD=false
UPDATE_UPSTREAM=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --build) BUILD=true; shift ;;
    --update-upstream) UPDATE_UPSTREAM="$2"; shift 2 ;;
    -h|--help) sed -n '1,40p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; echo "try: $0 --help" >&2; exit 1 ;;
  esac
done

UPSTREAM="${HERMES_UPSTREAM:-https://github.com/NousResearch/hermes-agent}"
UPSTREAM_REV="$(cat patches/upstream.lock)"

if [[ -n "$UPDATE_UPSTREAM" ]]; then
  echo "== updating upstream base: ${UPSTREAM_REV} -> ${UPDATE_UPSTREAM} =="
  UPSTREAM_REV="$UPDATE_UPSTREAM"
  if [[ "$DRY_RUN" != true ]]; then
    echo "$UPDATE_UPSTREAM" > patches/upstream.lock
  fi
fi

if [[ "$DRY_RUN" != true ]]; then
  : "${HERMES_FORK:?set HERMES_FORK (flake URL of your fork, e.g. github:you/hermes-agent)}"
  : "${HERMES_FORK_REMOTE:?set HERMES_FORK_REMOTE (git URL to push, e.g. git@github.com:you/hermes-agent.git)}"
fi

PATCH_FILES=()
shopt -s nullglob
for p in patches/*.patch patches/*.diff; do
  PATCH_FILES+=("$p")
done
shopt -u nullglob
if [[ ${#PATCH_FILES[@]} -eq 0 ]]; then
  echo "no patches in patches/*.{patch,diff}; nothing to do"
  exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== cloning $UPSTREAM (rev ${UPSTREAM_REV}) =="
git clone --quiet --filter=blob:none --no-checkout "$UPSTREAM" "$TMP/hermes-agent"
git -C "$TMP/hermes-agent" checkout --quiet "$UPSTREAM_REV"
git -C "$TMP/hermes-agent" switch --quiet -c patched

for p in "${PATCH_FILES[@]}"; do
  echo "== applying ${p} =="
  git -C "$TMP/hermes-agent" apply --whitespace=warn "$REPO_ROOT/$p"
  git -C "$TMP/hermes-agent" add -A
  git -C "$TMP/hermes-agent" commit --quiet -m "hermes-deploy: apply ${p}"
done

NEW_REV="$(git -C "$TMP/hermes-agent" rev-parse HEAD)"
echo "patched commit: ${NEW_REV}"
git -C "$TMP/hermes-agent" show --stat --oneline HEAD

if [[ "$DRY_RUN" == true ]]; then
  echo "== dry-run complete — fork and flake untouched =="
  exit 0
fi

echo "== pushing patched to ${HERMES_FORK_REMOTE} =="
git -C "$TMP/hermes-agent" remote add fork "$HERMES_FORK_REMOTE"
git -C "$TMP/hermes-agent" push --force fork patched

NEW_URL="${HERMES_FORK}/${NEW_REV}"
echo "== pointing hermes-agent input at ${NEW_URL} =="
perl -pi -e 'if (/hermes-agent/) { s|url = "[^"]*"|url = "'"$NEW_URL"'"| }' flake.nix
grep -n 'url =' flake.nix

echo "== updating flake.lock =="
nix flake lock --update-input hermes-agent

if [[ "$BUILD" == true ]]; then
  echo "== verifying toplevel build =="
  nix build .#hermes.config.system.build.toplevel
fi

cat <<EOF

Patches applied to ${UPSTREAM_REV} and pushed as fork branch 'patched'
(${NEW_REV}). flake.nix now points at ${NEW_URL}; flake.lock updated.

Next steps:
  git add patches/ flake.nix flake.lock && git commit -m "hermes: apply local patch(es)"
  scripts/deploy.sh
To roll back: git revert that commit, then scripts/deploy.sh.
To drop a patch: remove its patches/*.patch and re-run this script.
EOF
