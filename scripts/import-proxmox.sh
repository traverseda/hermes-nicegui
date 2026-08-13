#!/usr/bin/env bash
# Build the Proxmox LXC image (tarball) and import it into Proxmox VE.
#
# The proxmox-lxc module produces a `system.build.tarball` image. Copy it to
# the Proxmox host as a template, then create the container with `pct`.
#
# Usage:
#   scripts/import-proxmox.sh [--ctid 105] [--pve root@pve] [--storage local-lvm]
#
# After the container is running: enable the deploy machinery by cloning this
# repo into /var/lib/hermes-deploy on the CT, then use scripts/deploy.sh for
# all subsequent changes.
set -euo pipefail

CTID="${CTID:-105}"
PVE="${PVE:-root@pve}"
STORAGE="${STORAGE:-local-lvm}"
BRIDGE="${BRIDGE:-vmbr0}"
ARCH="x86_64-linux"

cd "$(dirname "$0")/.."

echo "== building LXC tarball =="
OUT=$(nix build --no-link --print-out-paths .#nixosConfigurations.hermes.config.system.build.tarball)

TARBALL=$(find "$OUT" -name '*.tar.xz' | head -1)
echo "built: $TARBALL"

echo "== uploading to $PVE =="
TEMPLATE_NAME="nixos-hermes-$(git rev-parse --short HEAD).tar.xz"
scp "$TARBALL" "$PVE:/var/lib/vz/template/cache/$TEMPLATE_NAME"

echo "== creating container $CTID =="
ssh "$PVE" pct create "$CTID" \
  "local:vztmpl/$TEMPLATE_NAME" \
  --arch "$ARCH" \
  --ostype unmanaged \
  --hostname hermes \
  --net0 name=eth0,bridge="$BRIDGE",ip=dhcp,firewall=1 \
  --rootfs "$STORAGE:10" \
  --memory 2048 \
  --swap 0 \
  --cores 2 \
  --features nesting=1 \
  --unprivileged 1 \
  --onboot 1

echo "== starting =="
ssh "$PVE" pct start "$CTID"

echo
echo "container $CTID created. Next steps:"
echo "  1. ssh root@<ct-ip> and set up keys / secrets"
echo "  2. git clone <this-repo> /var/lib/hermes-deploy on the CT"
echo "  3. scripts/deploy.sh  for all future changes (rollback-safe)"
