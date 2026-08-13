# hermes-deploy

Rollback-safe deployment of the [Hermes LLM agent](https://github.com/NousResearch/hermes-agent)
on a **Proxmox LXC**, driven by a **Nix flake**.

> The core principle: **anything the bot changes about itself must be safe,
> and it must be able to roll back.**

## How safety works

Hermes can change itself (skills, config, memory, plugins, packages). Every
one of those paths is routed through a mechanism that can be undone:

| Change the bot makes                        | Path to production                          | Rollback                                   |
| ------------------------------------------- | ------------------------------------------- | ------------------------------------------ |
| Config / settings / skills / docs           | git commit in this flake                    | `git revert` / `hermes-rollback`           |
| New capabilities / packages / services      | `nixos-rebuild switch` → new generation     | `nixos-rebuild switch --rollback`          |
| Secrets (API keys)                          | agenix (encrypted, decrypted on activation) | rotate/rekey, no plaintext ever in store   |
| Anything else that breaks the gateway       | health-checked auto-rollback watchdog       | automatic, one generation per check        |

### Rollback layers (defence in depth)

1. **Nix generations** — every deploy is an immutable new generation; the
   previous one stays in the store forever (GC keeps 30d).
2. **Health-checked auto-rollback** — `hermes-watchdog.timer` runs regularly
   and asks the agent itself (`hermes doctor`) whether it's healthy. If a
   generation newer than `last-known-good` reports unhealthy, it rolls back
   **exactly one generation** per run — never more.
3. **Manual rollback** — `hermes-rollback` / `scripts/rollback.sh` steps back
   one generation (and reverts the flake repo one commit).
4. **Proxmox snapshots** — `scripts/deploy.sh --snapshot` snapshots the CT
   before deploying, giving you a full-disk escape hatch independent of Nix.

### What the bot can't do

Hermes runs in **native mode** (hardened systemd unit, `NoNewPrivileges`,
`ProtectSystem=strict`). It cannot `apt`/`pip`/`npm`-install itself — the only
tools on its PATH are the ones declared in `modules/hermes-service.nix`. To
gain a capability, the bot must edit the flake → git → new generation. That's
what makes every self-change rollback-safe. (If you want a mode where the bot
*can* install packages, flip `services.hermes-agent.container.enable = true`
for an isolated Ubuntu container — documented upstream.)

## Repository layout

```
flake.nix                        # inputs: nixpkgs, hermes-agent, agenix
hosts/hermes/configuration.nix   # Proxmox LXC host config
modules/hermes-service.nix       # hermes-agent + tailscale (shared w/ tests)
modules/hermes-deploy.nix        # deploy/rollback/watchdog machinery
modules/hindsight.nix            # Hindsight memory service (podman, tailnet-only)
modules/llm.nix                  # generic preferred-model config (hermesDeploy.llm)
tests/vm-configuration.nix       # local test VM config
tests/hermes-test.nix            # NixOS integration test (runtime; slow)
tests/hermes-config-check.nix    # fast eval-time deploy/rollback wiring check
tests/hindsight-config-check.nix # fast eval-time Hindsight wiring check
scripts/deploy.sh                # deploy to the LXC (--snapshot option)
scripts/rollback.sh              # step back one generation
scripts/test.sh                  # nix flake check / local VM / integration test
scripts/import-proxmox.sh        # build & import the LXC image into Proxmox
secrets/                         # agenix encrypted secrets (see secrets/README.md)
```

## Getting started

### 1. Test locally first (no Proxmox needed)

```sh
nix flake check          # build toplevel + tarball + run integration test
nix run .#hermes-vm      # boot a local VM with the same modules
```

### 2. Create the Proxmox container

```sh
scripts/import-proxmox.sh --ctid 105 --pve root@pve --storage local-lvm
```

This builds `system.build.tarball` (the proxmox-lxc image), uploads it as a
template, creates an unprivileged CT with tailscale-friendly defaults, and
starts it. It also enables `nesting=1` in case you later opt into Hermes
container mode.

### 3. First-boot setup

```sh
ssh root@<ct-ip>
# add your ssh keys (see hosts/hermes/configuration.nix)
# install the pre-generated host key so agenix can decrypt the secrets
# (see secrets/README.md):
scp secrets/lxc-host-ed25519        root@<ct-ip>:/etc/ssh/ssh_host_ed25519_key
scp secrets/lxc-host-ed25519.pub    root@<ct-ip>:/etc/ssh/ssh_host_ed25519_key.pub
ssh root@<ct-ip> "chmod 0600 /etc/ssh/ssh_host_ed25519_key"
# set up agenix secrets (secrets/README.md) — incl. the tailscale auth key
git clone <this-repo> /var/lib/hermes-deploy
```

From then on the LXC rebuilds *itself* from that git checkout.

### 4. Deploy & roll back

```sh
scripts/deploy.sh                 # build + switch + watchdog health check
scripts/deploy.sh --snapshot      # also snapshot the CT first (external net)
scripts/rollback.sh               # step back one generation
```

On the LXC itself, the bot can run: `hermes-deploy`, `hermes-rollback`,
`hermes-status`.

## Making changes

Edit the flake, commit, and deploy. That's the whole loop — and it's the same
loop whether the change comes from a human or from the bot:

```sh
git add -A && git commit -m "hermes: add skill for <thing>"
scripts/deploy.sh
```

If the gateway breaks, the watchdog notices within minutes and steps back one
generation. If it breaks during the deploy, the deploy itself rolls back.

## Notes

- **Pin your inputs.** `hermes-agent` is pinned to a specific rev in
  `flake.nix` — the project is best-effort and `main` can break the module.
  Update deliberately: `nix flake lock --update-input hermes-agent`.
- **Secrets never go in Nix config.** Use agenix (`secrets/README.md`).
- **`nix flake update`** bumps nixpkgs/hermes-agent/agenix to latest locks —
  treat this as a normal deploy and let the watchdog validate it.

## Hindsight memory service

The LXC also runs a standalone [Hindsight](https://github.com/vectorize-io/hindsight)
memory service (retain / recall / reflect for AI agents) — see
`modules/hindsight.nix`. It's an official container image pinned by digest,
run under rootful podman via `virtualisation.oci-containers`.

- **API:** `http://127.0.0.1:8888` on the host, `http://<tailscale-ip>:8888`
  over the tailnet. Health: `GET /health`.
- **Access control:** tailnet-only (firewall opens 8888/9999 on `tailscale0`
  only). Clients authenticate with `Authorization: Bearer <HINDSIGHT_API_TENANT_API_KEY>`
  from the `hindsight-env` agenix secret.
- **Hermes is wired in** via its official Hindsight memory provider
  (`settings.memory.provider = "hindsight"` + `HINDSIGHT_*` env vars in
  `hosts/hermes/configuration.nix`): auto-recall before each turn,
  auto-retain after each response, plus `hindsight_retain`/`recall`/`reflect`
  tools. It needs `HINDSIGHT_API_KEY` set in the `hermes-env` secret (same
  value as `HINDSIGHT_API_TENANT_API_KEY`).
- **Model:** the memory-extraction LLM is the generic `hermesDeploy.llm`
  option (`modules/llm.nix`) — defaults to opencode-go
  (`https://opencode.ai/zen/go/v1`) / `deepseek-v4-flash`. The key lives in
  the secret, not in Nix config.
- **Data:** embedded PostgreSQL (`pg0`) persists in `/var/lib/hindsight`
  (bind-mounted to the container's `~/.pg0`).
- **Code optimisation (per bank):** Hindsight config is hierarchical
  (global → tenant → bank), and the service is expected to host both code
  and non-code banks, so global settings stay neutral. Code-tuned retain
  config (`retain_extraction_mode=verbatim` + an engineering-focused
  `retain_mission`) is applied to exactly the banks listed in
  `services.hindsight.codeBanks` — here `[ "hermes" ]` — via the per-bank
  config API by the `hindsight-code-bank-config.service` oneshot. Add/remove
  banks there and `systemctl start hindsight-code-bank-config` to re-apply.
- **Control plane (dashboard):** off by default; set
  `services.hindsight.enableControlPlane = true` to run it on :9999.
- **Deploy one-time setup:** create/rotate `secrets/hindsight-env.age` (see
  `secrets/README.md` — the committed copy is a local-key placeholder). Then
  a normal `scripts/deploy.sh` ships it. The container service is
  `podman-hindsight.service`; `podman healthcheck run hindsight` reports its
  health. The first deploy pulls the ~2GB image, so the unit may sit in
  activating/failed state for a while on first boot — the hermes watchdog
  checks the agent (not hindsight), so it won't false-positive roll back.

Other tools on the tailnet can point a `HindsightClient` at
`http://<tailscale-ip>:8888`.
