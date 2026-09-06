---
name: hermes-infect
description: "Deploy a hermes fork to a new machine over SSH (BYOK)."
version: 0.1.0
author: traverseda, Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [hermes, deploy, nixos-anywhere, ssh, mitosis, byok, friends-and-family, fork, self-hosting]
    related_skills: [hermes-agent]
---

# Hermes Infect (mitosis)

Seed a friend's machine with a **fork** of this hermes-deploy stack over SSH,
then hand it off. Once infected, the machine is **not managed by us anymore**:
the friend owns the fork, their hermes agent self-manages it from its own git
repo forever (via `services.hermes-deploy`). This is mitosis, not fleet
management — we grow a new independent copy and let go.

This skill is the playbook for that one-time act. It is **centered on turning
features on and off** for the new copy: the exact bundle that ships in a branch
changes as our own stack evolves, so treat the feature matrix below as a
living surface, not a fixed recipe. It deliberately does NOT create
per-friend configs or secrets in this repo — the friend's branch is a fork
that owns everything.

## When to Use

- The operator says they're setting up hermes for a friend or family member,
  or hands you a "bundle of tokens" for a new machine.
- A machine needs a fresh hermes install over SSH (bare-metal or VPS).
- You need to reproduce the stack as an independent, self-managing copy.

Don't use for:

- Deploying THIS host (`hermes` LXC) — that's `scripts/deploy.sh`.
- A machine that is already managed by us and stays managed by us.
- Anything where you're expected to keep operating the target afterward.

## Prerequisites

- The friend's **BYOK token bundle**: their own API keys (opencode-go,
  openrouter, etc.), their own tailscale auth key (their tailnet, NOT ours),
  and any dashboard/hindsight credentials. **Never reuse the operator's keys
  or tailscale instances.**
- SSH access to the target: `root@<ip>` or a user with passwordless sudo,
  reachable over the internet or LAN (nixos-anywhere needs kexec; x86_64 or
  aarch64 Linux with ≥1GB RAM).
- `nixos-anywhere` available:
  `terminal(command="nix run github:nix-community/nixos-anywhere -- --help", timeout=120)`
- This repo checked out locally (the operator's copy, which you'll branch).

## Mental model: branch, infect, hand off

```
operator hands you token bundle
        │
        ▼
branch this repo  ──►  add hosts/<friend>/ + flake.nix entry + BYOK secrets
        │
        ▼
infect: nixos-anywhere over SSH (fresh NixOS install)
        │
        ▼
hand off: friend pushes the fork to their own remote; their agent self-manages
```

The one-time act is: **branch → toggle features → wire BYOK secrets → infect →
hand off.** After handoff you have no ongoing responsibility.

## Procedure

1. **Collect the BYOK token bundle.** Ask the operator for the friend's own
   tokens. Confirm explicitly that none of them are the operator's
   (hermes-env keys, tailscale pre-auth keys, dashboard passwords). If
   anything is missing or reuses operator credentials, stop and ask.

2. **Create the fork branch.**
   ```
   terminal(command="git checkout -b infect/<friend>", workdir="<repo>")
   ```

3. **Add the host config.** Copy `skills/autonomous-ai-agents/hermes-infect/templates/friend-host.nix`
   to `hosts/<friend>/configuration.nix` and set the feature toggles (see
   [Feature toggles](#feature-toggles)). Register it in `flake.nix`:
   ```
   nixosConfigurations."<friend>" = lib.nixosSystem {
     inherit system;
     modules = commonModules ++ [ ./hosts/<friend>/configuration.nix ];
   };
   ```
   Copy the disko layout template
   (`templates/disko-single-disk.nix`) into the host file and set the disk
   device from `lsblk` output on the target.

4. **Generate a fresh host key for the target** (never the operator's).
   ```
   terminal(command="mkdir -p /tmp/infect-<friend>/etc/ssh && ssh-keygen -t ed25519 -N '' -f /tmp/infect-<friend>/etc/ssh/ssh_host_ed25519_key", timeout=30)
   ```

5. **Wire BYOK secrets.** Create per-friend agenix secrets under
   `secrets/<friend>-*.age` encrypted to **the operator's pubkey + the new
   host key** (see `secrets/README.md`). Secrets needed depend on the toggles
   you enabled (matrix below). Fill them with the friend's own values. Never
   copy this host's `secrets/hermes-env.age` etc.

6. **Infect.** Build + install over SSH. This wipes the target disk:
   ```
   terminal(command="nix run github:nix-community/nixos-anywhere -- --flake .#<friend> --target-host root@<ip> --extra-files /tmp/infect-<friend> --generate-hardware-config nixos-facter hosts/<friend>/facter.json", timeout=1800)
   ```
   The `--extra-files` stage drops the fresh host key in place so agenix can
   decrypt at first activation.

7. **Verify the new copy is alive and self-managing.**
   ```
   terminal(command="ssh root@<ip> 'systemctl status hermes-agent --no-pager | head -20'", timeout=60)
   terminal(command="ssh root@<ip> 'systemctl status hermes-deploy --no-pager | head -20'", timeout=60)
   ```
   The agent must be active. The deploy unit is oneshot; the watchdog timer
   (`hermes-watchdog.timer`) should be armed.

8. **Clone the fork onto the new machine** at `/var/lib/hermes-deploy` so the
   agent can self-deploy from git (same as this host). Point its git remote at
   the friend's own repo.

9. **Hand off.** Give the friend: the fork URL + branch, the SSH access they
   now control, and confirmation that the tokens in the bundle are theirs.
   Tell them their agent self-manages from git and can roll itself back. You
   are done — do not keep operating it.

## Feature toggles

Each feature is a `services.<name>.enable` flag. Set them per-friend in
`hosts/<friend>/configuration.nix`. The defaults below are the "friend"
posture (independent, BYOK); the operator's own LXC runs them all.

| Feature | Toggle | What it is | Friend default | Secrets it needs |
|---|---|---|---|---|
| Core agent | `services.hermes-agent.enable` | The hermes gateway + CLI | ON | `hermes-env` (friend's provider keys) |
| Vendored skills | (auto, via `hermes-skills.nix`) | Curated skills in the store | ON | none |
| Self-deploy/rollback | `services.hermes-deploy.enable` | Git-driven switch + auto-rollback watchdog | ON | git remote access on the machine |
| Preferred model | `hermesDeploy.llm.*` | opencode-go / mimo-v2.5 defaults (ships in code) | ON | LLM key lives in the service's env file |
| Hindsight memory | `services.hindsight.enable` | Local retain/recall memory (podman) | ON | `hindsight-env` (friend's LLM + tenant key) |
| Web dashboard | `services.hermes-dashboard.enable` | Admin panel (tailnet-only, basic auth) | Optional | `dashboard-env` (friend's creds) |
| Tailscale | `services.tailscale.enable` | Tailnet membership | ON — friend's OWN tailnet | `tailscale-auth` (friend's pre-auth key) |
| Home Assistant | `services.hermes-ha.enable` | HA api_server + reverse proxy | OFF | `api-server-env` |
| xaelwiki notes | `services.xaelwiki.enable` | Notes MCP server | OFF (points at operator's codeberg) | `xaelwiki-env`, `xaelwiki-ssh` |
| Cloudflare tunnel | `services.cloudflare-tunnel.enable` | Public exposure | OFF | `cloudflare-tunnel` |

Tailnet ports are governed by the exposure registry
(`hermesDeploy.exposure.services` in `modules/exposure.nix`); the modules open
their own ports through it and the build fails if a credentialed service has no
wired credentials — so only enable a feature whose secrets you actually
provisioned.

## Pitfalls

- **Reusing operator secrets/keys/tailscale.** This is the one hard rule. A
  friend's copy must bring its OWN keys. The operator's tailscale auth key and
  hermes-env must never reach a branch that will live on someone else's box.
- **`--extra-files` is required for agenix.** If you skip it, the fresh host
  key never lands in the install and agenix can't decrypt at activation.
  Re-key the secrets or re-run with the extra-files stage.
- **nixos-anywhere wipes the disk.** Confirm the target is disposable before
  running. Use `--phases disko` with `--disko-mode format` (or a VM test:
  `--vm-test`) if you want to rehearse without destroying the target.
- **Submodules.** After cloning the fork onto the new box, run
  `git submodule update --init` (the deploy unit does this, but a manual clone
  does not) or `vendor/xaelWiki` won't materialize.
- **Hardware.** Different machines need different `disko.devices` and the
  facter.json is per-machine; don't copy a friend's facter.json to another
  friend.
- **The skill is a living surface.** The stack changes; re-check the toggle
  table against `modules/` before each infect rather than trusting this file.

## Verification

- The target boots to a login prompt and `hermes-agent` is active.
- `hermes doctor` passes on the target (the deploy health check uses it).
- `hermes-watchdog.timer` is armed (`systemctl list-timers hermes-watchdog`).
- The fork is cloned at `/var/lib/hermes-deploy` with a git remote the friend
  controls, and a manual `systemctl start hermes-deploy` switches a new
  generation and reports OK.
- None of the operator's own secrets/tailnet keys appear anywhere in the
  branch (`search_files` for the operator's key fragments / tailnet domain).
