# Deployment Workflow

## Machine layout

```
azrael.lan (build machine)     hermes.lan (LXC, ssh://root@hermes.lan)
~/Code/personal/               /var/lib/hermes-deploy
hermes-deploy/                 (git repo, bare-ish — has working tree)
```

## Git remotes

Both boxes use the **same `box` remote** pointing to the hermes LXC's repo:

```ini
# azrael & hermes
[remote "box"]
    url = ssh://root@hermes.lan/var/lib/hermes-deploy
    fetch = +refs/heads/*:refs/remotes/box/*
```

Hermes also has a `github` remote for backup:
```ini
[remote "github"]
    url = https://github.com/hermes-3640/hermes-deploy.git
```

### SSH self-connection (hermes pulling from itself)

The `box` remote SSH connection is problematic: hermes pushes to itself via
`ssh root@hermes.lan` (192.168.193.158), which requires root's SSH key to work.

**Required setup on hermes.lan:**
- `/root/.ssh/id_ed25519` + `/root/.ssh/id_ed25519.pub` exist
- `id_ed25519.pub` is in `/root/.ssh/authorized_keys`
- `/root/.ssh/config` has `StrictHostKeyChecking no` for `127.0.0.1` and `hermes.lan` / `192.168.193.158`
- Git has `safe.directory` for `/var/lib/hermes-deploy` in both repo and root config

### SSH from azrael.lan to hermes.lan

Azrael connects using your SSH key. Your keys must be in root's on hermes: `/root/.ssh/authorized_keys`.

## The deployment flow

### 1. Work on azrael

```bash
# In /home/traverseda/Code/personal/hermes-deploy on azrael
git add -A
git commit -m "your message"
git push box main              # pushes to the LXC repo
```

**That's it for the git step.** `git push box main` writes the commit to the
remote repo at `/var/lib/hermes-deploy` on the LXC. Azrael does not need to
`SSH` hermes separately for the push — Git's SSH transport handles it.

### 2. Update submodules (if needed)

```bash
# On azrael:
# If you modified a submodule (hermes-agent, hermes-nicegui, xaelWiki):
cd vendor/hermes-agent
git add -A && git commit -m "fix: ..."
cd ../..
git add vendor/hermes-agent flake.lock
git commit -m "hermes-agent: bump to foo"
git push box main
```

### 3. Pull and deploy on hermes

```bash
ssh root@hermes.lan
# or: ssh root@hermes.lan
cd /var/lib/hermes-deploy
git pull box main              # fetch+merge into working tree
nixos-rebuild switch --flake .#hermes  # or: nixos-rebuild boot --flake .#hermes --rollback
```

If `git pull box main` fails because of SSH self-connection issues:
```bash
# hermes pushes to itself first, then pulls
cd /var/lib/hermes-deploy
git push box main              # creates the commit if azrael pushed
git pull box main              # fetch+merge
```

### 4. Verify

```bash
# On hermes
head -1 /var/lib/hermes/.hermes/config.yaml    # should show json: {"agent":...}
hermes --profile default chat -q "ping" -Q     # test CLI
podman logs hindsight 2>&1 | tail              # check for 401 errors
```

## Why two repos?

The LXC does not run `nix flake check` or do any Nix builds — the build machine
does. The LXC repo is primarily:
1. A **ledger** for the deploy/rollback/watchdog machinery
2. A **content lane** (via `hermes-tool.sh`)

The deploy flow is:
- **Azrael**: `git commit` + `git push box main` → hermes has the code
- **Hermes**: `git pull box main` + `nixos-rebuild switch --flake .#hermes`

Nix fetches hermes-agent from the `vendor/hermes-agent` path inside the repo on the LXC when building, so the LXC repo must have the correct submodule checkouts.
checkouts.

## Troubleshooting

### "dubious ownership" on pull
```bash
# On hermes
git config --add safe.directory /var/lib/hermes-deploy
git config --add safe.directory /var/lib/hermes-deploy/.git
```

### "Host key verification failed"
```bash
ssh-keygen -R hermes.lan   # on azrael
ssh root@hermes.lan         # accept the key
```

### hermes can't SSH to itself for pull
```bash
# On hermes, fix root SSH:
ssh-add -l                       # check if root has keys
ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ""
cat /root/.ssh/id_ed25519.pub >> /root/.ssh/authorized_keys
echo "StrictHostKeyChecking no" > /root/.ssh/config
```

### Wrong model name in config.yaml
The config file is now managed by Nix (JSON from the store, installed at
activation with `install -D` from the `pbf1m6fhp...-hermes-config.yaml` store
path). If the LXC has stale YAML:
```bash
# Force reinstall from the store path the activation script uses:
install -D -o hermes -g hermes -m 0440 /nix/store/<config-store-path>/hermes-config.yaml /var/lib/hermes/.hermes/config.yaml
systemctl restart hermes-agent
```

Find the correct store path:
```bash
grep "hermes-config.yaml" /nix/store/*/activate 2>/dev/null | head -1
```

### Submodule errors on hermes pull
Hermes's submodule remotes point to GitHub. If fetching submodules fails:
```bash
# On hermes, clone each submodule manually:
cd /var/lib/hermes-deploy/vendor/hermes-agent
git fetch --all 2>&1 || git pull origin main 2>&1
cd ../hermes-nicegui
git fetch --all 2>&1 || git pull origin main 2>&1
cd ../xaelWiki
git fetch --all 2>&1 || git pull origin main 2>&1
# Then re-pull the outer repo
cd /var/lib/hermes-deploy
git pull box main --recurse-submodules
```

## Quick reference: azrael → hermes

```bash
# Azrael (build machine)
git add -A && git commit -m "..."      # make changes
git push box main                      # push to she's repo
git push github main                   # optional: backup to GH

# Hermes (LXC) — when connected
git pull box main                      # get azrael's changes
nixos-rebuild switch --flake .#hermes  # deploy
```
