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
modules/hermes-skills.nix        # vendored skills wiring (no bundled skills)
modules/hermes-deploy.nix        # deploy/rollback/watchdog machinery
modules/hermes-ha.nix            # Home Assistant profile api_server + proxy
modules/hermes-dashboard.nix     # web dashboard (hermesagent.lan:9119)
modules/hindsight.nix            # Hindsight memory service (podman, tailnet-only)
modules/exposure.nix             # tailnet-exposure registry (auth enforced)
modules/cloudflare-tunnel.nix    # public exposure via Cloudflare Tunnel (outbound-only)
modules/xaelwiki.nix             # xaelwiki notes MCP server (editable, low-stakes)
modules/llm.nix                  # generic preferred-model config (hermesDeploy.llm)
skills/                          # curated, NixOS-corrected skills (external_dirs)
vendor/xaelWiki/                 # xaelwiki source (git submodule, editable on the LXC)
tests/vm-configuration.nix       # local test VM config
tests/hermes-test.nix            # NixOS integration test (runtime; slow)
tests/hermes-config-check.nix    # fast eval-time deploy/rollback wiring check
tests/hindsight-config-check.nix # fast eval-time Hindsight wiring check
tests/dashboard-config-check.nix # fast eval-time dashboard wiring check
tests/exposure-config-check.nix  # fast eval-time exposure/auth guardrails
tests/cloudflare-tunnel-config-check.nix # fast eval-time tunnel wiring check
scripts/deploy.sh                # deploy to the LXC (--snapshot option)
scripts/rollback.sh              # step back one generation
scripts/test.sh                  # nix flake check / local VM / integration test
scripts/import-proxmox.sh        # build & import the LXC image into Proxmox
scripts/patch-hermes.sh          # rebuild the hermes-agent fork w/ local patches
patches/                         # local hermes source patches (see patches/README.md)
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
git clone --recurse-submodules <this-repo> /var/lib/hermes-deploy
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

## Patching Hermes

Local source patches to hermes-agent's own code (core fixes *or* additive
features) ship through a **fork**: the patch *content* lives in `patches/`
(this repo, rollback-safe), and `scripts/patch-hermes.sh` applies it onto the
pinned upstream rev (`patches/upstream.lock`), pushes the fork's `patched`
branch, points `flake.nix` at the fork, and re-locks. See `patches/README.md`.

```sh
# produce a diff against the pinned rev, save as patches/00-my-fix.patch
HERMES_FORK=github:you/hermes-agent \
HERMES_FORK_REMOTE=git@github.com:you/hermes-agent.git \
  scripts/patch-hermes.sh --dry-run      # verify without pushing
HERMES_FORK=github:you/hermes-agent \
HERMES_FORK_REMOTE=git@github.com:you/hermes-agent.git \
  scripts/patch-hermes.sh --build        # push fork, relock, verify build
git add patches/ flake.nix flake.lock && git commit -m "hermes: apply local patch"
scripts/deploy.sh
```

Rollback: `git revert` the commit that bumped `flake.nix`/`flake.lock`, then
`scripts/deploy.sh`. Drop a patch: delete its `.patch` and re-run
`patch-hermes.sh`. Update upstream: `patch-hermes.sh --update-upstream <rev>`.

## Notes

- **Pin your inputs.** `hermes-agent` is pinned to a specific rev in
  `flake.nix` — the project is best-effort and `main` can break the module.
  Update deliberately: `nix flake lock --update-input hermes-agent`. If you
  carry local patches, the input is your fork's `patched` branch instead — see
  [Patching Hermes](#patching-hermes).
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
- **Per-bank tuning (code vs chat):** Hindsight config is hierarchical
  (global → tenant → bank), and the service is expected to host both code and
  non-code banks. The `code` bank (used by OpenCode) gets byte-exact code
  tuning via the per-bank config API (`hindsight-code-bank-config.service`
  oneshot): `retain_extraction_mode=verbatim` + `retain_chunk_size=800`
  (source chunks stored byte-exact), an engineering-focused `retain_mission`
  + `observations_mission` (preserve exact symbols, cite error text verbatim,
  never paraphrase identifiers, extract "Technical preferences and
  conventions" like loguru-over-stdlib). The `hermes` bank (the agent's
  general/chat bank) keeps untouched upstream defaults. Add/remove banks in
  `services.hindsight.codeBanks` and `systemctl start
  hindsight-code-bank-config` to re-apply.
- **Shared global changes (help every bank):** `simple` text-search dictionary
  (`HINDSIGHT_API_TEXT_SEARCH_EXTENSION_NATIVE_LANGUAGE=simple` — kills English
  stemming so BM25 matches `get_user_id` exactly); ONNX
  `intfloat/multilingual-e5-small` embeddings (384d, same dims as the old
  bge-small, so no dimension-change wipe/re-embed of existing facts); and a
  `bm25:high` recall boost (`HINDSIGHT_API_RECALL_STRATEGY_BOOSTS`) so code
  recall is keyword-driven. Consolidation nuance: `enable_auto_consolidation`
  is deliberately left ON — consolidation LLM-resummarises clustered code
  memories into abstracted observations on top of the byte-exact chunks
  (additive, not destructive); set
  `services.hindsight.codeBankEnableAutoConsolidation = false` for true
  byte-exact-only.
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

## Hermes web dashboard

The agent's web admin panel (`hermes dashboard`) runs as
`hermes-dashboard.service` and is exposed on **`http://hermesagent.lan:9119`**
over the tailnet only (firewall opens 9119 on `tailscale0`; everything else
is firewalled off).

- **Auth:** binding a non-loopback host always engages the dashboard's own
  auth gate (there is no unauthenticated public-bind option). We use the
  bundled `basic` username/password provider — sessions are stateless
  HMAC-signed tokens, no OAuth IDP needed.
- **Credentials:** `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` /
  `_PASSWORD_HASH` / `_SECRET` live in the `dashboard-env` agenix secret and
  are appended to the default profile's `.env` (`secrets/README.md`). No
  secrets in Nix config.
- **Rotate the password:** `agenix -e secrets/dashboard-env.age`, replace
  `_PASSWORD_HASH` (see secrets/README.md for the scrypt one-liner), commit,
  deploy.
- The unit is named `hermes-dashboard.service` — the canonical name `hermes
  update` restarts a managed dashboard instead of raw-killing the PID.

## Tailnet exposure registry (no credentials, no deploy)

Every port opened on the tailnet goes through a single registry —
`hermesDeploy.exposure.services` in `modules/exposure.nix` — so "expose a
service" and "prove it has credentials" are one declaration. The registry
**fails the build** (not warns) when:

- a credentialed exposure (`auth.type = bearer|basic|oauth`) is enabled but
  its credential source (agenix secret / env file) is not wired;
- a service is exposed with `auth.type = "none"` unless it explicitly opts
  in with `auth.insecure = true` **and** the global
  `hermesDeploy.exposure.allowInsecure = true` is set (a double opt-in);
- any port is opened on `tailscale0` that is not declared in the registry
  (so a host config or a new module can't silently open an extra port).

The firewall rule itself is *derived* from the registry — service modules no
longer touch `networking.firewall.interfaces.tailscale0` directly, so an open
port and a credentialed service can't drift apart. `tests/exposure-config-check.nix`
proves all three guardrails fire.

Current exposures: hindsight API `:8888` (bearer key), hindsight control
plane `:9999` (off unless enabled; bearer key), Home Assistant proxy `:8444`
(bearer `API_SERVER_KEY`), web dashboard `:9119` (basic auth).

## Cloudflare Tunnel (public exposure)

Public exposure is opt-in and outbound-only: `cloudflared` on the LXC dials
out to Cloudflare, so **no inbound port is opened** and the tailnet-exposure
registry above is untouched. `services.cloudflare-tunnel` (in
`modules/cloudflare-tunnel.nix`) wires the pinned nixpkgs
`services.cloudflared` module:

- **Locally-managed tunnel.** The tunnel credentials (a JSON file with
  `AccountTag`/`TunnelID`/`TunnelSecret`) come from the `cloudflare-tunnel`
  agenix secret — never Nix config. Set the real `tunnelId` to match the
  credentials file (see `secrets/README.md`).
- **Ingress is credential-enforced — same mechanism as the tailnet.** Each
  ingress value must be a `name` from `hermesDeploy.exposure.services`, and
  the public URL is derived from that exposure's port. The build fails if an
  ingress references an unknown exposure, an uncredentialed one, or an
  `auth.type = "none"` service (the insecure opt-in is tailnet-only, never
  allowed on the public internet).
- **Nothing is public until you add an ingress rule.** The module starts with
  an empty `ingress` table and a `http_status:404` catch-all, so "wired but
  not exposed" genuinely exposes nothing. Going public is one deliberate edit:
  ```nix
  services.cloudflare-tunnel.ingress = {
    "dashboard.0u0.ca" = "hermes-dashboard";   # name from the exposure registry
  };
  ```
  The module warns when ingress is non-empty.
- **Front it with credentials.** Hostnames published through the tunnel still
  hit the service's own auth (dashboard basic-auth, hindsight bearer, HA
  `API_SERVER_KEY`). The public hostnames on `0u0.ca` / `outsidecontext.solutions`
  live in the tunnel ingress table and/or Cloudflare's dashboard.

## xaelwiki notes MCP server (editable, low-stakes)

`services.xaelwiki` runs the [xaelwiki](https://github.com/traverseda/xaelWiki)
notes MCP server as a tailnet-only HTTP service, wired into the hermes agent as
an MCP server so the agent can search, capture, and **edit** shared markdown
notes. It is designed to be **fast to iterate on and independently revertible**
— it is explicitly non-critical:

- **Editable source, no rebuild.** xaelwiki's code ships as a git **submodule**
  (`vendor/xaelWiki/`) and the service runs the checkout directly from a Nix
  python env (`PYTHONPATH` points at the run-location symlink). Edit
  `vendor/xaelWiki/src/xaelwiki/*.py` on the LXC, then
  `systemctl restart xaelwiki` — no `nixos-rebuild`. A broken xaelwiki only
  takes down notes, never the agent.
- **Roll back separately.** The submodule is its own git repo. Roll it back
  with `git -C /var/lib/hermes-deploy/vendor/xaelWiki checkout <good-rev>` (or
  `git revert`), independent of Nix generations. The deploy flow runs
  `git submodule update --init` (never `--force`), so local edits survive
  deploys.
- **Shared notes vault.** Notes live in their own git repo, cloned from
  `ssh://git@codeberg.org/traverseda/notes.git` with a dedicated SSH deploy key
  (`xaelwiki-ssh` secret). The server auto-pulls before each mutation and
  auto-pushes after, keeping the LXC vault in sync with the operator's
  `~/Code/personal/xaelWiki/notes`.
- **Editable by default.** `XAEL_READ_ONLY` is off — the agent has the full
  write surface (capture / append / update / move / tag / undo). Safety comes
  from xaelwiki itself: no delete (archive instead), auto-commit on every
  mutation, optimistic concurrency via `revision`, git-backed undo.
- **Credential-enforced.** The MCP endpoint is registered in the exposure
  registry (bearer auth); hermes-agent connects over `127.0.0.1` with the same
  token from the `xaelwiki-env` secret, interpolated at runtime — the literal
  token never reaches config.yaml or the store.

**OpenCode is wired in** via the repo's `opencode.json`: the
`@vectorize-io/opencode-hindsight` plugin points at
`http://hermesagent.lan:8888` with bank `code` (replacing the global remote
`hindsight-api.0u0.ca` endpoint). The API key is not in the config — export
`HINDSIGHT_API_TOKEN` (same value as `HINDSIGHT_API_TENANT_API_KEY` in
`hindsight-env`) in the shell before launching opencode.
