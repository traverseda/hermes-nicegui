# hermes-deploy — AGENTS.md

This flake drives a rollback-safe deployment of the Hermes LLM agent onto a
Proxmox LXC. Every change ships as a git commit + a new NixOS generation, with
a health-checked watchdog that auto-rolls-back unwell deploys.

## Repository structure

```
flake.nix                        # top-level: inputs, commonModules, checks, devShell
hosts/hermes/configuration.nix   # the real LXC config (imports all modules)
modules/hermes-service.nix       # hermes-agent + tailscale (shared W/ test VMs)
modules/hermes-deploy.nix        # deploy/rollback/watchdog machinery
modules/hermes-tools.nix         # git-backed content store + hermes-tool (content lane)
modules/hermes-skills.nix        # vendored skills wiring
modules/hermes-dashboard.nix     # web dashboard (hermesagent.lan:9119)
modules/hermes-nicegui.nix       # hermes-nicegui UI (vendor submodule, system lane)
modules/hermes-ha.nix            # Home Assistant profile proxy
modules/hindsight.nix            # Hindsight memory service (podman)
modules/exposure.nix             # tailnet exposure registry (auth-enforced)
modules/cloudflare-tunnel.nix    # public exposure via Cloudflare Tunnel
modules/xaelwiki.nix             # xaelwiki notes MCP server
modules/llm.nix                  # model config shared W/ all services
vendor/                          # git submodules: hermes-agent, hermes-nicegui, xaelWiki
scripts/deploy.sh                # build locally, push closure, activate LXC
scripts/rollback.sh              # step back one generation + git reset
scripts/test.sh                  # nix flake check | vm | test
scripts/hermes-tool.sh           # content-lane CLI (skills, tools, MCP servers)
secrets/                         # agenix encrypted .age files
patches/                         # local hermes-agent patches (see patches/README.md)
tests/                           # config-check nix files + integration test
opencode.json                    # project opencode config (+ @vectorize-io/opencode-hindsight)
```

## Two change lanes

**System lane** = Nix rebuild + new generation. Owner: this flake repo (plus
vendor/* submodules). Changes: gateway unit, packages, firewall, secrets,
daemons, vendored source (`vendor/hermes-agent`, `vendor/hermes-nicegui`,
`vendor/xaelWiki`). Rollback: `hermes-rollback` (one generation + resets
vendor/* submodules to the matching tag).

**Content lane** = `hermes-tool` → auto-commit → git. Owner:
`/var/lib/hermes/content` (own git repo). Changes: skills, tool scripts, MCP
servers. Restart cost: skills (none), bin (none), MCP (gateway restart only).
Rollback: `hermes-tool revert` (restores `content-good` tag).

Vendored source (`vendor/*`) is system lane, NOT content lane — it's real
in-process code that can break the gateway on a bad edit.

## CRITICAL: DATA SAFETY — NEVER DESTROY REMOTE GIT OR STATE

**These operations will DESTROY data and are ABSOLUTELY FORBIDDEN on remote systems:**

1. **NEVER `rsync` a `.git/` directory to a remote** — overwrites the entire git database of objects and refs, destroying any commits that exist only locally on the remote. Always `git push`, `git clone`, or `git pull` — never binary-replace git state.
2. **NEVER `git push --force`** to any remote branch — destroys commits on the remote.
3. **NEVER `git rm --cached vendor/` then `git submodule add` on hermes's live repo** — replaces submodules in the index and can lose commits/changes.
4. **NEVER use `rsync -a .git/` to sync the flake repo** — if the remote has local commits (the bot makes them), they are destroyed. Use `git worktree` or `git push/pull` instead.
5. **ALWAYS assume the remote's git repo has unpushed commits** — the bot self-deploys and commits directly. These commits are the ONLY record of content-lane changes (skills, tools, MCP servers). Losing them breaks rollback.

If you need to update something on a remote repo, you must use non-destructive operations: `git push`, `git fetch + git pull --rebase`, or explicitly state what will be lost and get confirmation.

## Commands

```sh
nix flake check --no-build              # eval-only: fast checks, no builds
nix flake check                          # full build + all *-config-check tests
nix flake check --no-build               # eval-only — use this by default
nix run .#hermes-vm                      # boot local test VM
scripts/test.sh check                    # same as nix flake check
scripts/test.sh vm                       # same as nix run .#hermes-vm
scripts/deploy.sh [--dry-run]            # build locally, push closure, activate LXC
scripts/rollback.sh                      # step back one generation + git reset
scripts/patch-hermes.sh --dry-run        # verify patches (see patches/README.md)
```

> **Rule:** Always use `nix flake check --no-build` unless a real build is explicitly needed. The `--no-build` flag runs eval-time config checks instantly (seconds) without building derivations. Only run without `--no-build` when you specifically need to verify that derivations build correctly.

## Editing vendor submodules (hermes-agent / hermes-nicegui / xaelWiki)

**Every vendored edit must be committed INSIDE the submodule first**, then
`nix flake lock --update-input` bumps flake.lock:

```sh
cd vendor/hermes-agent
# ... edit files ...
git add -A && git commit -m "fix: <whatever>"
cd ../..
nix flake lock --update-input hermes-agent
git add vendor/hermes-agent flake.lock
git commit -m "hermes-agent: fix <whatever>"
scripts/deploy.sh
```

Same pattern for `vendor/hermes-nicegui` (input: `hermes-nicegui-src`) and
`vendor/xaelWiki` (input: `xaelwiki-src`). An uncommitted submodule edit is
invisible to the build — this has shipped broken before.

### Automated vendor-deploy script

On the LXC, use `scripts/vendor-deploy` to commit a submodule, update
flake.lock, validate, and optionally deploy in one command. This is
recommended over the manual 4-step sequence to avoid forgetting the
`nix flake lock` step:

```sh
# Commit and validate only:
vendor-deploy vendor/hermes-agent "fix: <whatever>" --dry-run

# Commit, validate, AND deploy:
vendor-deploy vendor/hermes-agent "fix: <whatever>" --deploy
```

Submodule names: `hermes-agent`, `hermes-nicegui`, `xaelWiki`.
The script maps these automatically to their flake input names.

## SSH access & deploy host

`root@hermes.lan` (zerotier — resolves via zerotier DNS) or `root@192.168.193.158` (zerotier IP) both connect to the box's SSH daemon.
`root@hermes` (100.123.226.74, tailnet IP) does NOT work: tailscale runs
`--ssh` and the policy rejects every user on this node. Deploy scripts default
to `root@192.168.193.158`. Override with `HERMES_HOST`.

## Deploy flows

### On the LXC (self-deploy — the bot's own path)

The bot runs from within the LXC using:
- `hermes-deploy` (triggers the systemd oneshot unit)
- OR: `nixos-rebuild --flake /var/lib/hermes-deploy#hermes`

**Critical: before ANY deploy, fix vendor flake.lock if you edited vendor/\*:**

```
# Step 1: commit vendor submodule, then update flake.lock
cd vendor/<submodule-name>
git add -A && git commit -m "fix: <whatever>"
cd ../..
nix flake lock --update-input <submodule-name>  # ← THIS IS REQUIRED
# Step 2: verify the fix
nix flake check --no-build                       # ← always do this before deploying
# Step 3: deploy
hermes-deploy                                    # or: nixos-rebuild --flake /var/lib/hermes-deploy#hermes
```

If `nix flake check --no-build` fails with a "hash mismatch" error, it means the
narHash in `flake.lock` no longer matches the content of the vendor submodule.
The fix is always `nix flake lock --update-input <input>` followed by committing
the updated `flake.lock`. This is the #1 cause of self-deploy failure.

**Why this works on the LXC despite "read-only root filesystem":**
NixOS root FS is read-only/immutable (normal for NixOS). `nixos-rebuild switch`
does NOT write to `/` — it builds to the Nix store (which IS writable), then
switches the system profile to point to the new generation. The store is a copy-on-wriite hash store, not a writable filesystem. The bot (hermes) is a `trusted-users` in nix.conf and has passwordless sudo, so it can run `nixos-rebuild` and `hermes-deploy` directly.

**What the deployScript does on the LXC:**
1. `git -C /var/lib/hermes-deploy add -A && commit` (records any uncommitted edits)
2. `nix flake check --no-build` (fails fast if vendor narHash drift or syntax error)
3. `nixos-rebuild switch --max-jobs 1 --cores 2 --flake /var/lib/hermes-deploy#hermes`
4. Health-check (`hermes-agent active` + `hermes doctor`) with grace period
5. If unhealthy → content-recovery (`hermes-tool revert`) → then generation rollback (one step back)
6. `sync_repo_to_gen` — resets git + vendor/ submodules to the rolled-back generation's commit

### From azrael (build machine)

```sh
HERMES_HOST=root@hermes.lan scripts/deploy.sh [--dry-run] [--snapshot]
```

1. Optional Proxmox snapshot (`--snapshot`, requires `PROXMOX_CT`)
2. Memory gate — checks hermes has ≥2048 MB headroom (MemAvailable + SwapFree), exit 42 if not
3. `nix build .#nixosConfigurations.hermes.config.system.build.toplevel` locally
4. `nix copy --to ssh://$HERMES_HOST "$STORE_PATH"` pushes the closure
5. `nixos-rebuild switch --store-paths "$STORE_PATH"` on hermes (no local build)
6. Health-check — hermes-agent active + `hermes doctor` passes
7. Starts `hermes-rollback.service` watchdog

The build machine is the source of truth for the git repo history — it runs
full nixpkgs-based builds and copies the closure via `nix copy`. The LXC also
has its own git repo for the bot's local commits and can self-deploy directly.
Default host is `root@192.168.193.158` (zerotier), override with `HERMES_HOST`.

Both paths create `gen-<N>` tags in the git ledger and update
`/var/lib/hermes-deploy/last-known-good`. The watchdog runs every 15 minutes,
checking the agent, and only rolls back past the last-known-good generation
(so deliberate stops never trigger auto-rollback).

## Secrets

One encrypted `.age` file **per secret** in `secrets/` (see `secrets/manifest`
for the registry + consumers). `modules/hermes-secrets.nix` generates an
`age.secrets."<NAME>"` entry per manifest line (→ `/run/agenix/<NAME>` at
activation) and builds one env file per consumer from the manifest
(`/run/agenix/hermes.env`, `/run/agenix/hindsight.env`, …) — services point
directly at those paths; no secret touches the Nix store. Edit/rotate via

```sh
nix develop
hermes-secrets edit <NAME>        # decrypt → $EDITOR → re-encrypt → validate
hermes-secrets check              # deploy gate (also wired into hermes-deploy)
```

`hermes-deploy` runs `hermes-secrets check` before every switch and aborts on
a missing/undecryptable/empty required secret. Recipients: the operator's SSH
pubkey (`secrets/operator-pubkey.pub`) + the LXC host key
(`secrets/lxc-host-ed25519.pub`). The private key is gitignored and must be
uploaded to the LXC during initial provisioning.

## Content-lane workflow

```sh
hermes-tool skill new <name> [<category>]   # scaffold a skill
hermes-tool tool add <src>                   # install an executable
hermes-tool mcp add <name> <url>             # register an MCP server
hermes-tool commit [message]                 # snapshot to git
hermes-tool mark-good                        # tag current state as content-good
hermes-tool revert [--config]                # restore content-good (use --config for live config.yaml)
hermes-tool doctor                           # sanity check the store
```

The watchdog tries `hermes-tool revert` as a first recovery attempt before a
Nix generation rollback. `config.yaml` snapshot is in the store but is NEVER
auto-restored (operator-only `revert --config`).

## Testing

```sh
nix flake check             # all checks: build, tarball, VM build, config checks
nix build .#checks.<sys>.<name>   # a specific check (config checks are fast eval-time)
nix run .#hermes-vm         # boot VM for runtime iteration
nix build .#checks.x86_64-linux.hermes-integration   # slow runtime integration test
```

`tests/*-config-check.nix` are eval-time assertions on generated systemd units
(no VM boot). `tests/hermes-test.nix` is the slow runtime integration test.

## Vendored source live-editing

`hermes-agent` can run from a LIVE source checkout on the LXC (not the store
package): edit via the nicegui Files tab → `vendor/hermes-agent`, then
`systemctl restart hermes-agent`. Roll back with `git -C /var/lib/hermes-deploy/vendor/hermes-agent checkout -- .
+ git clean -fd`. A broken edit crash-loops the gateway until reverted.
Disable by setting `hermesDeploy.editableAgentSrc = null` in the flake.

## opencode on this repo

`opencode.json` configures the `@vectorize-io/opencode-hindsight` plugin
(pointing at `http://hermesagent.lan:8888`, bank `code`). Export
`HINDSIGHT_API_TOKEN` in the shell before launching opencode (same value as
`HINDSIGHT_API_TENANT_API_KEY` in `hindsight-env`).

The LXC's global opencode config (installed at activation) also pins the model
to `quanttrio/Qwen3.6-35b-a3b-awq` globally and loads the hindsight plugin
worldwide.

## The bot is root

Hermes has passwordless sudo and is a trusted nix user. The systemd sandbox
(`NoNewPrivileges`, `ProtectSystem`) is relaxed so root is real root. Safety
comes from the rollback machinery: git ledger + Nix generations + health-checked
auto-rollback. **Do not add permission gates** — the operator's job is keeping
rollback working, not fencing the bot off.

## Merge criteria

See `MERGE_CRITERIA.md` for the full set of hard gates (must be reversible,
no secrets outside agenix, every exposed port registered with auth, watchable
failure modes) and judgment factors. A test deleted to pass a change is an
automatic reject. Code-lane changes that ship without a matching `flake.lock`
bump are also auto-rejects.

### Patching hermes-agent

Local patches ship through a fork. Patch content lives in `patches/` and is
applied onto the upstream rev from `patches/upstream.lock`. See `patches/README.md` for the full workflow. The bot (on the LXC) cannot push to the fork — patch authoring runs on the build machine.
