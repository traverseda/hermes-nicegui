#!/usr/bin/env bash
# migrate-secrets — one-time split of the old unified config.age blob into
# one .age file per secret (per secrets/manifest).
#
#   scripts/migrate-secrets.sh --source secrets/config.age --identity ~/.ssh/id_ed25519
#     [--secret-dir secrets] [--values <name=value lines file>]
#
# Reads the plaintext config.age (decrypted with --identity), and for each
# secret in secrets/manifest writes secrets/<NAME>.age encrypted to BOTH
# recipients (operator + LXC host). Secrets NOT present in the source must
# be supplied in --values (name=value lines) — the script refuses to invent
# placeholders. After migration, delete the source blob with
#   git rm secrets/config.age
#
# This is a ONE-TIME migration aid. Day-to-day editing goes through
# `hermes-secrets edit`; validation through `hermes-secrets check|doctor`.

set -euo pipefail
cd "$(dirname "$0")/.."

SECRET_DIR="secrets"
SOURCE=""
IDENTITY=""
VALUES_FILE="/dev/null"

usage() {
  cat <<'EOF'
usage: migrate-secrets.sh --source <config.age> --identity <privkey> [options]
  --source FILE      old unified config.age to decrypt and split
  --identity KEY     private key that can decrypt --source (operator or LXC host)
  --values FILE      file with `NAME=value` lines for secrets missing from source
  --secret-dir DIR   secrets dir to write .age files into (default: secrets)
  --dry-run          print what would be written, don't touch files
EOF
  exit 1
}

DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --source) SOURCE=$2; shift 2 ;;
    --identity) IDENTITY=$2; shift 2 ;;
    --values) VALUES_FILE=$2; shift 2 ;;
    --secret-dir) SECRET_DIR=$2; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) usage ;;
  esac
done

[ -n "$SOURCE" ] || usage
[ -n "$IDENTITY" ] || usage
[ -f "$SOURCE" ] || { echo "source not found: $SOURCE" >&2; exit 1; }
[ -f "$IDENTITY" ] || { echo "identity not found: $IDENTITY" >&2; exit 1; }
[ -f "secrets/manifest" ] || { echo "secrets/manifest missing — run from the repo root" >&2; exit 1; }
[ -f "secrets/operator-pubkey.pub" ] || { echo "secrets/operator-pubkey.pub missing" >&2; exit 1; }
[ -f "secrets/lxc-host-ed25519.pub" ] || { echo "secrets/lxc-host-ed25519.pub missing" >&2; exit 1; }

# age (with ssh support) from the dev shell so nothing depends on host age.
AGE="nix develop --command age"

OP_RECIP=$(awk '/^ssh-ed25519/{print $0}' secrets/operator-pubkey.pub)
LX_RECIP=$(awk '/^ssh-ed25519/{print $0}' secrets/lxc-host-ed25519.pub)

# Decrypt the source blob once.
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
$AGE -d -i "$IDENTITY" "$SOURCE" > "$TMP/plain" 2>/dev/null || {
  echo "failed to decrypt $SOURCE with $IDENTITY" >&2; exit 1
}

# Load any extra values (name=value lines, last-wins).
declare -A EXTRA
while IFS='=' read -r k v; do
  [ -n "$k" ] && EXTRA["$k"]="$v"
done < "$VALUES_FILE"

# awk: name<TAB>consumers<TAB>required<TAB>placeholder<TAB>purpose (blank lines / comments ignored)
missing_count=0
while IFS=$'\t' read -r name consumers required placeholder purpose; do
  case "$name" in ""|\#*) continue ;; esac

  value=""
  if [ -n "${EXTRA[$name]:-}" ]; then
    value="${EXTRA[$name]}"
  else
    value=$(awk -F= -v k="$name" 'BEGIN{found=0} $1==k {found=1; sub(/^[^=]*=/,""); print; exit} END{exit !found}' "$TMP/plain") || {
      echo "MISSING: $name not in source nor --values" >&2
      missing_count=$((missing_count+1))
      continue
    }
  fi

  out="$SECRET_DIR/$name.age"
  if [ "$DRY" = "1" ]; then
    echo "would write: $out (${#value} bytes)"
    continue
  fi
  # Encrypt raw value (no NAME= prefix) so decrypted content is just the value.
  printf '%s' "$value" > "$TMP/v"
  $AGE -e -r "$OP_RECIP" -r "$LX_RECIP" -o "$out" "$TMP/v"
  echo "wrote: $out"
done < secrets/manifest

if [ "$missing_count" -gt 0 ]; then
  echo "ERROR: $missing_count secret(s) missing from source and values — not written" >&2
  exit 1
fi
echo "done. verify with: nix develop --command hermes-secrets doctor --identity $IDENTITY"