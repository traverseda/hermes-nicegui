#!/usr/bin/env bash
#
# R4 ticket t_89f84f69 Part C — behavioral fixture test for `hermes-tool
# revert`.
#
# Encodes DONE MEANS item 3(b): "external/live edit survives watchdog
# recovery". It runs the REAL scripts/hermes-tool.sh (with @placeholders@
# substituted into a tmp fixture dir) so the shipped binary logic itself is
# under test — bash + git + sed only, no nix needed on this box.
#
# Cases:
#   1. `revert` (default, the watchdog/contentRecovery path): content store
#      resets to content-good, but the LIVE config.yaml is UNTOUCHED. This is
#      the incident clobberer (2026-08-26 01:25:06 stale-snapshot restore) and
#      it must stay dead.
#   2. `revert --config`: explicit operator restore — live config.yaml IS
#      overwritten with the store snapshot.
#   3. `revert --config` with NO snapshot present: content still resets,
#      live config.yaml untouched (restore_config is a no-op without a
#      snapshot); likewise asserted for plain `revert`.
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
SRC="$SCRIPT_DIR/../scripts/hermes-tool.sh"

[ -f "$SRC" ] || { echo "FAIL: source script missing: $SRC" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

CONTENT="$TMP/content"
LIVE="$TMP/home"
TOOL="$TMP/hermes-tool"
fail() { echo "FAIL: $*" >&2; exit 1; }

mkdir -p "$CONTENT" "$LIVE"

# Build the substituted copy exactly like pkgs.replaceVars does.
sed -e "s|@contentDir@|$CONTENT|g" \
    -e "s|@hermesHome@|$LIVE|g" \
    -e "s|@goodTag@|content-good|g" \
    -e "s|@gatewayService@|hermes-agent|g" \
    "$SRC" > "$TOOL"
chmod +x "$TOOL"

# Sanity: the substituted script must parse before anything runs.
sh -n "$TOOL" || fail "substituted hermes-tool.sh failed syntax check"

# ── Fixture setup ────────────────────────────────────────────────────────
git init -q "$CONTENT"
git -C "$CONTENT" config user.name fixture
git -C "$CONTENT" config user.email fixture@localhost

echo "# known-good skill" > "$CONTENT/skills-known-good.md"
echo "stale_model: true" > "$CONTENT/config.yaml"
git -C "$CONTENT" add -A
git -C "$CONTENT" commit -q -m "fixture: last known good (incl. stale config snapshot)"
git -C "$CONTENT" tag content-good

# Drift AFTER the good tag: a new tracked skill that a revert must remove.
echo "# drift skill (post-tag)" > "$CONTENT/skills-drift.md"
git -C "$CONTENT" add skills-drift.md
git -C "$CONTENT" commit -q -m "fixture: drifted state"

# Live config holds deliberate operator edits — these must survive recovery.
echo "live_model: operator-value" > "$LIVE/config.yaml"

# ── Case 1: default `revert` = DONE MEANS 3(b) survival ─────────────────
HERMES_TOOL_NO_RESTART=1 "$TOOL" revert >/dev/null
[ "$(git -C "$CONTENT" rev-parse HEAD)" = "$(git -C "$CONTENT" rev-parse content-good)" ] \
  || fail "case 1: content tree did not reset to content-good"
[ ! -f "$CONTENT/skills-drift.md" ] || fail "case 1: drift skill survived revert"
grep -q "live_model: operator-value" "$LIVE/config.yaml" \
  || fail "case 1: live config.yaml was clobbered by plain revert"
! grep -q "stale_model" "$LIVE/config.yaml" \
  || fail "case 1: live config.yaml contains stale snapshot data"
echo "PASS case 1: 'revert' reset content to content-good; live config.yaml kept operator-value"

# Re-create drift for the next case.
echo "# drift skill round 2" > "$CONTENT/skills-drift.md"
git -C "$CONTENT" add skills-drift.md
git -C "$CONTENT" commit -q -m "fixture: drifted again"

# ── Case 2: explicit `revert --config` restores the snapshot ────────────
HERMES_TOOL_NO_RESTART=1 "$TOOL" revert --config >/dev/null
grep -q "stale_model: true" "$LIVE/config.yaml" \
  || fail "case 2: explicit 'revert --config' did not restore the snapshot"
[ ! -f "$CONTENT/skills-drift.md" ] || fail "case 2: content was not reset"
echo "PASS case 2: 'revert --config' restored live config.yaml from the store snapshot"

# ── Case 3: no snapshot present → restore is a safe no-op ───────────────
# Move the good tag to a state with NO config.yaml in the store (as if the
# last-known-good predates any snapshot): otherwise `reset --hard` would
# re-materialize the snapshot from the tagged commit before restore_config
# even runs.
echo "# drift skill round 3" > "$CONTENT/skills-drift.md"
rm "$CONTENT/config.yaml"
git -C "$CONTENT" add -A
git -C "$CONTENT" commit -q -m "fixture: operator removed snapshot, drifted again"
git -C "$CONTENT" tag -f content-good >/dev/null 2>&1
[ ! -f "$CONTENT/config.yaml" ] || fail "case 3 setup: snapshot still present"
echo "live_model: operator-value-again" > "$LIVE/config.yaml"

HERMES_TOOL_NO_RESTART=1 "$TOOL" revert >/dev/null
grep -q "live_model: operator-value-again" "$LIVE/config.yaml" \
  || fail "case 3a: default revert touched config.yaml even with no snapshot"

HERMES_TOOL_NO_RESTART=1 "$TOOL" revert --config >/dev/null
[ "$(git -C "$CONTENT" rev-parse HEAD)" = "$(git -C "$CONTENT" rev-parse content-good)" ] \
  || fail "case 3b: content tree did not reset to content-good"
grep -q "live_model: operator-value-again" "$LIVE/config.yaml" \
  || fail "case 3b: 'revert --config' clobbered config.yaml despite missing snapshot"
echo "PASS case 3: with no snapshot, 'revert' and 'revert --config' reset content but never touch config.yaml"

echo "ALL PASS: hermes-tool revert keeps watchdog recovery content-only (R4 t_89f84f69 3b)"
