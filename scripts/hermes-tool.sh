#!/bin/sh
# hermes-tool — fast-path content authoring + recovery for the hermes deployment.
#
# This is the "content" lane, separate from the Nix "system" lane:
#   * skills/   — agent-authored skills, symlinked into $HERMES_HOME/skills
#                 (picked up next session, no restart)
#   * bin/      — agent-authored executables, appended to the gateway PATH
#                 (available next turn, no restart)
#   * config.yaml — git-tracked SNAPSHOT of the live config, refreshed by
#                 commit/mark-good (and the periodic auto-commit). It is
#                 restored to the live path ONLY by an explicit operator
#                 `hermes-tool revert --config` — never automatically.
#
# SAFETY (the point of this tool):
#   * This binary lives in /nix/store — immutable. It depends on NOTHING the
#     agent can edit or delete. Even if every skill is broken, this command
#     still runs.
#   * Every mutation auto-commits to the git-backed content store, so there is
#     always a rollback point.
#   * `hermes-tool revert` restores the last-known-good CONTENT state (git
#     tag content-good) and restarts the gateway. The live config.yaml is
#     NEVER touched by automatic recovery — restoring it requires an explicit
#     operator-invoked `hermes-tool revert --config`. This is deliberate: the
#     watchdog runs `revert` on any health failure, and it must not clobber
#     operator edits to config.yaml with a stale snapshot (R4 t_89f84f69).
#     The operator runs it as root; the agent can run it too (falls back to
#     `hermes gateway restart`).
#
# Placeholders (@contentDir@ etc.) are substituted at build time by the NixOS
# module (pkgs.substituteAll).

set -eu

CONTENT="@contentDir@"
HERMES_HOME="@hermesHome@"
GOOD_TAG="@goodTag@"
GATEWAY_SERVICE="@gatewayService@"

say() { echo "[hermes-tool] $*"; }
die() { echo "[hermes-tool] ERROR: $*" >&2; exit 1; }

require_repo() { [ -d "$CONTENT/.git" ] || die "content store missing at $CONTENT"; }
git_() { git -C "$CONTENT" "$@"; }

snapshot_config() {
  if [ -f "$HERMES_HOME/config.yaml" ]; then
    cp "$HERMES_HOME/config.yaml" "$CONTENT/config.yaml"
  fi
}

commit_all() {
  git_ add -A
  if git_ diff --cached --quiet; then
    say "nothing to commit"
  else
    git_ commit -q -m "$1"
    say "committed: $1"
  fi
}

restart_gateway() {
  # Testability guard (R4 t_89f84f69 Part C): fixture tests exercise this REAL
  # script without a live gateway. If HERMES_TOOL_NO_RESTART=1 is set in the
  # environment, skip all hermes/systemctl interaction entirely.
  if [ "${HERMES_TOOL_NO_RESTART:-0}" = "1" ]; then
    say "gateway restart skipped (HERMES_TOOL_NO_RESTART)"
    return 0
  fi
  # Prefer hermes' own in-place restart (works as the hermes user via RPC);
  # fall back to systemctl (works when run as root, e.g. by the watchdog).
  if command -v hermes >/dev/null 2>&1; then
    HERMES_HOME="$HERMES_HOME" hermes gateway restart >/dev/null 2>&1 || true
  fi
  systemctl restart "$GATEWAY_SERVICE" >/dev/null 2>&1 || true
  say "gateway restart requested"
}

restore_config() {
  if [ -f "$CONTENT/config.yaml" ]; then
    cp "$CONTENT/config.yaml" "$HERMES_HOME/config.yaml"
    say "config.yaml restored from store snapshot"
  fi
}

cmd_status() {
  require_repo
  say "content store: $CONTENT"
  git_ log --oneline -5
  if git_ rev-parse --verify -q "$GOOD_TAG" >/dev/null 2>&1; then
    say "last-known-good: $GOOD_TAG -> $(git_ log --oneline -1 "$GOOD_TAG")"
  else
    say "last-known-good: none"
  fi
  echo "-- skills --"
  find "$CONTENT/skills" -name SKILL.md 2>/dev/null | sed "s#$CONTENT/skills/##; s#/SKILL.md##" | sort
  echo "-- bin --"
  ls -1 "$CONTENT/bin" 2>/dev/null
  echo "-- config.yaml snapshot: $(test -f "$CONTENT/config.yaml" && echo present || echo missing) --"
}

cmd_commit() {
  require_repo
  snapshot_config
  commit_all "${1:-content snapshot}"
}

cmd_mark_good() {
  require_repo
  snapshot_config
  commit_all "mark-good: $(date -Is)"
  git_ tag -f "$GOOD_TAG"
  say "tagged $GOOD_TAG (current state is last-known-good)"
}

cmd_revert() {
  require_repo
  # Content-only by default: the watchdog auto-recovery path calls plain
  # `revert`, which must NEVER restore config.yaml over deliberate operator
  # edits. Restoring the live config is an explicit operator action only.
  restore_config_flag=0
  for arg in "$@"; do
    case "$arg" in
      --config) restore_config_flag=1 ;;
      *) die "unknown revert option: $arg (usage: hermes-tool revert [--config])" ;;
    esac
  done
  if ! git_ rev-parse --verify -q "$GOOD_TAG" >/dev/null 2>&1; then
    die "no $GOOD_TAG tag — nothing to restore to"
  fi
  say "reverting content to $GOOD_TAG"
  git_ reset --hard "$GOOD_TAG"
  if [ "$restore_config_flag" = 1 ]; then
    restore_config
  else
    say "live config.yaml left untouched (explicit 'revert --config' restores it)"
  fi
  restart_gateway
  say "revert complete"
}

cmd_skill_new() {
  require_repo
  [ $# -ge 1 ] || die "usage: hermes-tool skill new <name> [<category>]"
  NAME=$1
  CAT=${2:-general}
  DIR="$CONTENT/skills/$CAT/$NAME"
  [ -e "$DIR" ] && die "skill already exists: $DIR"
  mkdir -p "$DIR"
  cat > "$DIR/SKILL.md" <<EOF
---
name: $NAME
description: ""
version: 0.1.0
platforms: [linux]
metadata:
  hermes:
    tags: []
---
# $NAME
EOF
  commit_all "skill: add $CAT/$NAME"
  say "created $DIR (active next session — no gateway restart needed)"
}

cmd_tool_add() {
  require_repo
  [ $# -ge 1 ] || die "usage: hermes-tool tool add <src>"
  SRC=$1
  [ -f "$SRC" ] || die "no such file: $SRC"
  mkdir -p "$CONTENT/bin"
  cp "$SRC" "$CONTENT/bin/$(basename "$SRC")"
  chmod +x "$CONTENT/bin/$(basename "$SRC")"
  commit_all "tool: add $(basename "$SRC")"
  say "installed $(basename "$SRC") into content bin (available next turn — no restart)"
}

cmd_mcp_add() {
  require_repo
  [ $# -ge 1 ] || die "usage: hermes-tool mcp add <name> <url|cmd...>"
  command -v hermes >/dev/null 2>&1 || die "hermes CLI not found"
  snapshot_config
  HERMES_HOME="$HERMES_HOME" hermes mcp add "$@"
  snapshot_config
  commit_all "mcp: add $1"
  restart_gateway
  say "mcp server $1 registered"
}

cmd_mcp_rm() {
  require_repo
  [ $# -ge 1 ] || die "usage: hermes-tool mcp rm <name>"
  command -v hermes >/dev/null 2>&1 || die "hermes CLI not found"
  snapshot_config
  HERMES_HOME="$HERMES_HOME" hermes mcp remove "$1" || true
  snapshot_config
  commit_all "mcp: remove $1"
  restart_gateway
  say "mcp server $1 removed"
}

cmd_doctor() {
  require_repo
  git_ fsck --no-dangling >/dev/null 2>&1 || say "WARNING: git fsck failed"
  [ -f "$HERMES_HOME/config.yaml" ] && say "config.yaml present" || say "WARNING: no config.yaml"
  command -v hermes >/dev/null 2>&1 && say "hermes CLI present" || say "hermes CLI missing"
  git_ log --oneline -1
  if git_ rev-parse --verify -q "$GOOD_TAG" >/dev/null 2>&1; then
    say "last-good: $GOOD_TAG"
  else
    say "WARNING: no $GOOD_TAG tag — run 'hermes-tool mark-good' after verifying health"
  fi
}

usage() {
  cat <<'EOF'
usage: hermes-tool <command> [args]

status                    show store state, last-good commit, inventory
commit [message]          snapshot skills/bin/config.yaml into git
mark-good                 tag current state as content-good (after verifying it works)
revert                    restore the content STORE to content-good + restart gateway.
                          The live config.yaml is NEVER touched (safe for watchdog use).
revert --config           additionally restore live config.yaml from the store snapshot —
                          EXPLICIT operator action only; never run automatically.
skill new <name> [<cat>]  scaffold a new skill in the store
tool add <src>            install an executable into the store's bin
mcp add <name> <url>      register an MCP server (+ restart gateway)
mcp rm <name>             remove an MCP server (+ restart gateway)
doctor                    sanity-check the store and config.yaml
EOF
}

[ $# -ge 1 ] || { usage; exit 1; }
cmd=$1
shift
case "$cmd" in
  status) cmd_status ;;
  commit) cmd_commit "$@" ;;
  mark-good) cmd_mark_good ;;
  revert) cmd_revert "$@" ;;
  skill) cmd_skill_new "$@" ;;
  tool) cmd_tool_add "$@" ;;
  mcp)
    [ $# -ge 1 ] || die "usage: hermes-tool mcp <add|rm> ..."
    sub=$1
    shift
    case "$sub" in
      add) cmd_mcp_add "$@" ;;
      rm | remove) cmd_mcp_rm "$@" ;;
      *) die "unknown mcp subcommand: $sub" ;;
    esac
    ;;
  doctor) cmd_doctor ;;
  *) usage; exit 1 ;;
esac
