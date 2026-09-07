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

## SSH access & deploy host

`root@hermes.lan` (zerotier — resolves via zerotier DNS) or `root@192.168.193.158` (zerotier IP) both connect to the box's SSH daemon.
`root@hermes` (100.123.226.74, tailnet IP) does NOT work: tailscale runs
`--ssh` and the policy rejects every user on this node. Deploy scripts default
to `root@192.168.193.158`. Override with `HERMES_HOST`.

## Deploy flows

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

The build machine is the source of truth — no git repo on the LXC. Bypass
with `--force-memorygate`. Default host is `root@192.168.193.158` (zerotier),
override with `HERMES_HOST`.

### From hermes (self-modify)

The bot (hermes) can build, deploy, and roll back itself using the `hermes-deploy`
systemd unit. This is the "self-managing" path.

```sh
# On the LXC:
hermes-deploy            # triggers the hermes-deploy.service
# or equivalent:
nixos-rebuild switch --flake /var/lib/hermes-deploy#hermes
```

The deployScript does:
1. `git -C /var/lib/hermes-deploy add -A && commit` (records any uncommitted edits)
2. `git submodule update` (ensures vendor/ submodules match the ledger)
3. `nixos-rebuild switch --max-jobs 1 --cores 2 --flake "${cfg.repoDir}#${cfg.flakeAttr}"`
4. Health-check (`hermes-agent active` + `hermes doctor`) with ${cfg.gracePeriod}s grace
5. If unhealthy → content-recovery (`hermes-tool revert`) → then generation rollback (one step back)
6. `sync_repo_to_gen` — resets git + vendor/ submodules to the rolled-back generation's commit

The bot is a trusted nix user (`trusted-users = [ "hermes" ]`), so it can
run `nixos-rebuild` directly. The systemd unit (`hermes-deploy`) is the
convenient wrapper. Every activation creates a new generation the watchdog
can roll back.

Both paths create `gen-<N>` tags in the git ledger and update
`/var/lib/hermes-deploy/last-known-good`. The watchdog runs every 15 minutes,
checking the agent, and only rolls back past the last-known-good generation
(so deliberate stops never trigger auto-rollback).

## Secrets

agenix `.age` files in `secrets/`. Edit/rotate via `nix develop` then
`agenix -e secrets/<name>.age`. Recipients: your local SSH pubkey + the
LXC's pre-generated host key (`secrets/lxc-host-ed25519.pub`). The private key
is gitignored and must be uploaded to the LXC during initial provisioning.

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
