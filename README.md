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
| Config / settings / skills / docs           | local git commit in `/var/lib/hermes-deploy`| `hermes-rollback` (one commit + generation)|
| New capabilities / packages / services      | `nixos-rebuild switch` → new generation     | `nixos-rebuild switch --rollback`          |
| Secrets (API keys)                          | agenix (encrypted, decrypted on activation) | rotate/rekey, no plaintext ever in store   |
| Anything else that breaks the gateway       | health-checked auto-rollback watchdog       | automatic, one generation per check        |

> **No git remote (by design).** The flake repo is a plain local checkout — no
> `origin`. The build machine is the source of truth: `scripts/deploy.sh`
> **rsyncs** the working tree into `/var/lib/hermes-deploy` on the LXC, then
> activates a new generation. The bot commits and rolls back directly in that
> local checkout. Backup of the repo is handled out-of-band (external), so the
> deploy machinery has no network dependency beyond the Nix closure copy.

### Rollback layers (defence in depth)

1. **Nix generations** — every deploy is an immutable new generation; the
   previous one stays in the store forever (GC keeps 30d).
2. **Health-checked auto-rollback** — `hermes-watchdog.timer` runs regularly
   and asks the agent itself (`hermes doctor`) whether it's healthy. If a
   generation newer than `last-known-good` reports unhealthy, it rolls back
   **exactly one generation** per run — never more.
3. **Manual rollback** — `hermes-rollback` / `scripts/rollback.sh` steps back
   one local git commit (in `/var/lib/hermes-deploy`) and one generation.
4. **Proxmox snapshots** — `scripts/deploy.sh --snapshot` snapshots the CT
   before deploying, giving you a full-disk escape hatch independent of Nix.

### What the bot can't do

Hermes runs in **native mode** (hardened systemd unit, `NoNewPrivileges`,
`ProtectSystem=strict`). It cannot `apt`/`pip`/`npm`-install itself — the only
tools on its PATH are the ones declared in `modules/hermes-service.nix`. To
gain a **system** capability (a new package, daemon, firewall rule, secret),
the bot must edit the flake → git → new generation — that's what makes system
self-change rollback-safe. **Content** (skills, scripts, MCP servers) does NOT
need the flake: it goes through the git-backed content store below, which has
its own mechanical rollback. (If you want a mode where the bot *can* install
packages, flip `services.hermes-agent.container.enable = true` for an isolated
Ubuntu container — documented upstream.)

## Two change lanes: content vs system

The core unit of work on this deployment is **creating new tools** (skills,
scripts, MCP servers) — and most of them are NOT critical for the gateway to
run. Routing every one through a Nix rebuild + new generation is monolithic
and slow. So there are two lanes:

| | System lane (Nix) | Content lane (`hermes-tool`) |
|---|---|---|
| What changes | gateway unit, packages, firewall, secrets, daemons | skills, tool scripts, MCP servers, config.yaml |
| How it ships | flake edit → `nixos-rebuild switch` → generation | `hermes-tool` → auto-commit → git |
| Rollback | `hermes-rollback` (one generation) | `hermes-tool revert` (to `content-good`) |
| Restart cost | full gateway restart | skills: none · bin: none · MCP: gateway restart only |
| Owner | this flake repo | `/var/lib/hermes/content` (own git repo) |

- **Skills** land in `$HERMES_HOME/skills` (symlinked from the store) and are
  picked up next session — no restart. **Tool scripts** are appended to the
  gateway PATH (lowest precedence) — available next turn. **MCP servers**
  register via `hermes mcp add` (survives rebuilds; the config merge preserves
  user keys) and need only a gateway restart, not a rebuild.
- **Code writing is delegated to opencode** — the bot scaffolds, reviews, and
  installs; opencode writes. See the `hermes-tooling` skill.
- The bot *never* edits the system lane directly; `hermes-tool` can't touch it.
- Commands: `hermes-tool skill new / tool add / mcp add / commit /
  mark-good / revert / status` (see `scripts/hermes-tool.sh`).

### Content recovery is still rollback-safe

The content store is a **git repo** (`/var/lib/hermes/content`); every
mutation auto-commits. `hermes-tool revert` restores the last-known-good git
tag `content-good` *and* the known-good config.yaml, then restarts the
gateway. It is a **/nix/store binary** — immutable, so it works even if the
agent has deleted every skill or registered a broken MCP server. The deploy
watchdog tries `hermes-tool revert` as a cheap first recovery **before**
spending a Nix generation; generation rollback remains the ultimate floor.
The one rule that keeps this safe: **verify, then `hermes-tool mark-good`**.

## Repository layout

```
flake.nix                        # inputs: nixpkgs, hermes-agent, agenix
hosts/hermes/configuration.nix   # Proxmox LXC host config
modules/hermes-service.nix       # hermes-agent + tailscale (shared w/ tests)
modules/hermes-skills.nix        # vendored skills wiring (no bundled skills)
modules/hermes-deploy.nix        # deploy/rollback/watchdog machinery
modules/hermes-tools.nix         # git-backed content store + hermes-tool fast path
modules/hermes-ha.nix            # Home Assistant profile api_server + proxy
modules/hermes-dashboard.nix     # web dashboard (hermesagent.lan:9119)
modules/hindsight.nix            # Hindsight memory service (podman, tailnet-only)
modules/exposure.nix             # tailnet-exposure registry (auth enforced)
modules/cloudflare-tunnel.nix    # public exposure via Cloudflare Tunnel (outbound-only)
modules/xaelwiki.nix             # xaelwiki notes MCP server (editable, low-stakes)
modules/hermes-nicegui.nix       # hermes-nicegui web UI (submodule, tunnel-public)
modules/llm.nix                  # generic preferred-model config (hermesDeploy.llm)
skills/                          # curated, NixOS-corrected skills (external_dirs)
vendor/xaelWiki/                 # xaelwiki source (git submodule, editable on the LXC)
vendor/hermes-nicegui/           # hermes-nicegui source (git submodule, editable on the LXC)
tests/vm-configuration.nix       # local test VM config
tests/hermes-test.nix            # NixOS integration test (runtime; slow)
tests/hermes-config-check.nix    # fast eval-time deploy/rollback wiring check
tests/hindsight-config-check.nix # fast eval-time Hindsight wiring check
tests/dashboard-config-check.nix # fast eval-time dashboard wiring check
tests/exposure-config-check.nix  # fast eval-time exposure/auth guardrails
  tests/cloudflare-tunnel-config-check.nix # fast eval-time tunnel wiring check
  tests/tools-config-check.nix   # fast eval-time content-store guardrails
scripts/deploy.sh                # deploy to the LXC (--snapshot option)
scripts/rollback.sh              # step back one generation
scripts/hermes-tool.sh           # content-store CLI source (built by hermes-tools.nix)
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
# copy the repo (NO remote — the build machine is the source of truth;
# deploy.sh rsyncs it on every deploy):
rsync -a --exclude '.git/' --exclude 'state/' --exclude 'result*' \
  --exclude '*.qcow2' --exclude 'secrets/lxc-host-ed25519' \
  <this-repo>/ root@<ct-ip>:/var/lib/hermes-deploy/
```

From then on the LXC rebuilds *itself* from that local checkout: the bot
commits there directly and runs `hermes-deploy`, and `scripts/deploy.sh`
re-syncs it from the build machine before each new generation.

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
scripts/deploy.sh      # rsyncs the repo to the LXC, builds, activates
```

On the LXC (what the bot does): edit `/var/lib/hermes-deploy`, commit locally,
then `hermes-deploy` (rebuilds from that checkout). `hermes-rollback` steps the
local repo back one commit and rolls back one generation.

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

## hermes-nicegui web UI

`services.hermes-nicegui` runs the [hermes-nicegui](https://github.com/traverseda/hermes-nicegui)
NiceGUI browser UI (profile switcher, sessions, cron, kanban, terminal, files)
as `hermes-nicegui.service`. It is a *separate* app from `hermes dashboard` —
it reaches Hermes by running the `hermes` CLI as a subprocess and sharing
`$HERMES_HOME` with the gateway, so the profile switcher, session browser, and
cron/chat all see the real agent state.

- **Public via the Cloudflare tunnel.** The unit binds `127.0.0.1:8080`
  (loopback — no tailnet port, the exposure registry is untouched). The
  dedicated remotely-managed `hermes` tunnel (`cloudflared tunnel run --token`,
  same module below) serves **`https://hermes.0u0.ca` → `http://localhost:8080`**
  — hostname, ingress, and DNS are configured in Cloudflare (tunnel + CNAME
  already created), so no dashboard step remains.
- **Auth is the app's own login.** `HERMES_AUTH_ENABLED` gates the whole app
  behind a username/password admin account (created on first visit). That
  single login is the only thing between the public tunnel and a file browser
  + terminal, so keep it on.
- **Editable source, no rebuild.** Code ships as the `vendor/hermes-nicegui`
  git submodule and is symlinked into `/var/lib/hermes-nicegui/src`; edit it
  on the LXC and `systemctl restart hermes-nicegui` — no `nixos-rebuild`. A
  broken UI only takes down the UI, never the agent. Roll it back with
  `git -C /var/lib/hermes-deploy/vendor/hermes-nicegui checkout <good-rev>`.
- **Plugins work without a pip install.** The built-in plugins are discovered
  through an `importlib.metadata` entry-point group; the module ships a
  generated `dist-info` on `PYTHONPATH` next to the live checkout so they
  resolve against the editable source.
- **Kanban needs dashboard creds.** The kanban plugin logs into the Hermes
  dashboard web server with the *plaintext* username/password — the
  `dashboard-env` secret only has the scrypt hash, so the plaintext lives in a
  separate `hermes-nicegui-env` agenix secret
  (`HERMES_KANBAN_USERNAME`/`HERMES_KANBAN_PASSWORD`).
- **Files is confined to the workspace.** `HERMES_FILES_ROOT` defaults to the
  agent's workspace (`/var/lib/hermes/workspace`); don't widen it past that,
  the app is public.
- **Runs as the hermes user**, colocated with the agent — same user/group,
  same `$HERMES_HOME`, so the CLI subprocess and direct state reads resolve
  exactly like the gateway's.

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
`modules/cloudflare-tunnel.nix`) runs a **remotely-managed** tunnel:

- **Dashboard-managed tunnel.** The tunnel and its public hostnames are
  created in Cloudflare (Zero Trust → Networks → Tunnels, or the API). The
  LXC only runs `cloudflared tunnel run --token` with the tunnel's connector
  token, which comes from the `cloudflare-tunnel` agenix secret — never Nix
  config. systemd `LoadCredential` hands it to the unit as
  `$CREDENTIALS_DIRECTORY/token` (0600, DynamicUser), so the literal token
  never reaches `/nix/store`.
- **The `hermes` tunnel serves `hermes.0u0.ca`.** A dedicated remotely-managed
  `hermes` tunnel (id `93799893-ed0d-4c10-8122-83adf32f3134`) routes
  `https://hermes.0u0.ca` → `http://localhost:8080` (the hermes-nicegui UI).
  Tunnel, ingress, and the `hermes` CNAME are all configured in Cloudflare
  already; the LXC just connects with the token in the secret.
- **Publishing a hostname is a Cloudflare action, not a flake edit.** Add the
  tunnel's public hostnames in Cloudflare (dashboard or API), pointing each at
  the local service (e.g. `dashboard.0u0.ca` → `http://localhost:9119`). The
  service's own auth still gates the endpoint (dashboard basic-auth, hindsight
  bearer, HA `API_SERVER_KEY`, hermes-nicegui's own login).
- **Guardrail trade-off.** Because ingress lives in the dashboard, the
  exposure-registry credential enforcement does NOT apply to the public path
  — Cloudflare routes whatever hostnames the dashboard says. This is the
  deliberate trade-off of remotely-managed mode (the old locally-managed
  module is in git history).
- **Rotate the token:** rotate it in the Cloudflare dashboard, re-encrypt the
  new `cfut_…` into `secrets/cloudflare-tunnel.age` (see `secrets/README.md`),
  and redeploy.

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
