# Secrets

Secrets are encrypted with [agenix](https://github.com/ryantm/agenix) and
committed to the repo as `.age` files — **one file per secret**. They are
decrypted on the target host at activation time into `/run/agenix/<NAME>` —
never into `/nix/store` (world-readable) and never into this repo in
plaintext.

## Layout

```
secrets/
  manifest               plaintext registry (name → consumers, required, placeholder, purpose)
  operator-pubkey.pub    recipient 1: operator (build machine / azrael)
  lxc-host-ed25519.pub   recipient 2: the LXC host key (ll ages decrypt at activation)
  <NAME>.age             one encrypted file per secret (value only, no "NAME=" prefix)
  tailscale-auth.age     standalone: tailscale auth key (file-backed, not in manifest)
  cloudflare-tunnel.age  standalone: CF tunnel token (file-backed)
  xaelwiki-ssh.age       standalone: xaelwiki SSH deploy key (file-backed)
```

## The manifest is the contract

`secrets/manifest` is a TAB-separated table — the single source of truth for
which secrets exist and who consumes them:

```
NAME<TAB>consumers<TAB>required<TAB>placeholder<TAB>purpose
```

- **consumers** — comma-separated env-file groups that need this secret:
  `hermes` (default profile `.env`; gateway + dashboard + ha profile +
  opencode children), `hindsight` (container), `nicegui` (web UI unit),
  `xaelwiki` (notes MCP server).
- **required=1** — `hermes-secrets check` (the deploy gate) FAILS if the
  file is missing, undecryptable, or empty.
- **placeholder** — if the decrypted value equals this literal, the gate
  prints a loud warning (not a failure — a placeholder is operator rotation
  backlog, not an accidental drop). Left blank = no placeholder allowed.
- **purpose** — human description of what the secret is for.

`modules/hermes-secrets.nix` reads the manifest at eval time and generates:

1. one `age.secrets."<NAME>"` entry per line → `/run/agenix/<NAME>`;
2. a per-consumer aggregate env file, `/run/agenix/<consumer>.env`,
   containing only that consumer's `KEY=value` lines (assembled AFTER agenix
   decrypts, BEFORE `hermes-agent-setup` builds `$HERMES_HOME/.env`).

Service modules point at the aggregates, e.g.
`services.hermes-agent.environmentFiles = [ "/run/agenix/hermes.env" ]`.
The ha profile, dashboard, and opencode children get the hermes aggregate
because they read `$HERMES_HOME/.env` directly.

## Recipients

Every `.age` file is encrypted to **two** recipients:

| Recipient | Key file | Why |
| --------- | -------- | --- |
| Operator   | `secrets/operator-pubkey.pub` | lets you `hermes-secrets edit` / decrypt on the build machine |
| The LXC    | `secrets/lxc-host-ed25519.pub` | decrypts at activation on the host |

The operator pubkey is pulled from `root@hermes.lan` at first deploy:

```sh
ssh root@hermes.lan "grep ssh-ed25519 /root/.ssh/authorized_keys" \
  > secrets/operator-pubkey.pub
```

`secrets/lxc-host-ed25519` (private, gitignored) is a pre-generated SSH host
keypair for the LXC. **It must be uploaded to the LXC as its SSH host key
during initial creation** (see Setup) — otherwise agenix can't decrypt.

> **Important:** recipients are the **raw SSH public keys** (`ssh-ed25519 AAAA…`),
> *not* the `age1…` strings from `ssh-to-age`. With age ≥1.3 an `ssh-to-age`
> (X25519) recipient cannot be decrypted using an SSH private-key identity,
> which is exactly what agenix uses on the host. This was verified empirically;
> encrypt with `age -e -r "$(cat key.pub)"` or `age -R key.pub`.

## The hermes-secrets CLI

Ships to the box with the flake (and runs from the repo on the build
machine). Needs `age` on PATH — use `nix develop` on azrael.

```sh
hermes-secrets list                 # name<TAB>consumers<TAB>status
hermes-secrets doctor               # full status table + alias checks
hermes-secrets check                # deploy gate: FAIL on missing/unreadable/empty
hermes-secrets edit <NAME>          # decrypt to $EDITOR, re-encrypt to both recipients
```

- `check` is wired into `hermes-deploy` as a **pre-switch gate** (run after
  `nix flake check --no-build`, aborts the deploy on failure). Identity
  resolution: `--identity > $HERMES_SECRETS_IDENTITY > /etc/ssh/ssh_host_ed25519_key`
  (chosen only when `/run/agenix` exists — i.e. on the LXC) `> ~/.ssh/id_ed25519`.
- `doctor` also verifies read-only **alias invariants** (documented below).

```sh
# Edit one secret:
nix develop
hermes-secrets edit LLM_API_KEY       # decrypt → $EDITOR → re-encrypt → validate
```

## Editing / rotating a secret

```sh
nix develop
hermes-secrets edit <NAME>
git add secrets/<NAME>.age
git commit -m "secrets: rotate <NAME>"
# deploy; the gate runs hermes-secrets check before switching
```

To add a NEW secret:
1. encrypt it to both recipients (see Migration below for the exact incantation),
2. add a row to `secrets/manifest`,
3. commit both and deploy.

## Migration from the old unified config.age

If you are still on the old single-blob layout: decrypt `config.age`, split
per key, and re-encrypt one file per secret. The helper in this repo does
exactly that:

```sh
bash scripts/migrate-secrets.sh \
  --source secrets/config.age --identity ~/.ssh/id_ed25519 \
  --values <(printf '%s\n' 'NEW_SECRET=value')
```

then `git rm secrets/config.age` and add the manifest row. The helper
refuses to invent values for keys missing from the source.

## Setup

1. The LXC host keypair is already generated: `secrets/lxc-host-ed25519`
   (private) + `secrets/lxc-host-ed25519.pub` (public, committed). Upload the
   private key to the LXC during initial creation:

   ```sh
   # from the build machine, after the CT is created & reachable:
   scp secrets/lxc-host-ed25519 root@<ct-ip>:/etc/ssh/ssh_host_ed25519_key
   scp secrets/lxc-host-ed25519.pub root@<ct-ip>:/etc/ssh/ssh_host_ed25519_key.pub
   ssh root@<ct-ip> "chmod 0600 /etc/ssh/ssh_host_ed25519_key \
     && restorecon -v /etc/ssh/ssh_host_ed25519_key 2>/dev/null || true"
   ```

   agenix on the host uses this key by default (`/etc/ssh/ssh_host_ed25519_key`).

2. Encrypting a secret to both recipients (one-liner, age from `nix develop`):

   ```sh
   OP=$(awk '/^ssh-ed25519/{print $0}' secrets/operator-pubkey.pub)
   LX=$(awk '/^ssh-ed25519/{print $0}' secrets/lxc-host-ed25519.pub)
   printf '%s' 'the-value' | age -e -r "$OP" -r "$LX" -o secrets/<NAME>.age
   ```

3. Commit the `.age` files. Deploy; agenix decrypts them at activation into
   `/run/agenix/<NAME>` and the hermes-secrets activator assembles the
   per-consumer env files.

## Alias invariants (must stay equal)

| Keys | Constraint |
| ---- | ---------- |
| `API_SERVER_KEY` == `HERMES_API_TOKEN` | the nicegui kanban plugin authenticates to the gateway with the listener key |
| `HINDSIGHT_API_TENANT_API_KEY` == `HINDSIGHT_API_KEY` == `HINDSIGHT_API_TOKEN` == `HINDSIGHT_CP_DATAPLANE_API_KEY` | same tenant token across data-plane clients, hermes memory provider, opencode plugin, and CP dataplane proxy |
| `HINDSIGHT_API_LLM_API_KEY` == `LLM_API_KEY` | one vLLM key for the gateway and Hindsight's extraction/retrieval calls |
| `OPENCODE_API_KEY` == `LLM_API_KEY` | opencode children use the same model key |

`hermes-secrets doctor` checks these.

## Per-key rotation guides

### Discords tokens
Edit `DISCORD_BOT_TOKEN`, `DISCORD_ALLOWED_USERS`, `DISCORD_HOME_CHANNEL`.
Mint a new token in the Discord Developer Portal, replace `DISCORD_BOT_TOKEN`
with `hermes-secrets edit DISCORD_BOT_TOKEN`.

### Home Assistant token
Replace `HASS_TOKEN`. Mint a new token at Home Assistant → Profile →
Security → Long-lived access tokens.

### API_SERVER_KEY
Replace `API_SERVER_KEY` (≥ 16 chars) AND `HERMES_API_TOKEN` with the same
value (they must match — see aliases above).

### Dashboard password
Replace `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH` and optionally
`HERMES_DASHBOARD_BASIC_AUTH_SECRET`. Generate a fresh hash (the scrypt
format is documented in the old config.age contents: `scrypt$…`). If you also
rotate the password the kanban plugin uses, update `HERMES_KANBAN_PASSWORD`
to match.

### Hindsight control-plane login
Rotate `HINDSIGHT_CP_ACCESS_KEY`.

### XAEL_AUTH_TOKEN
Replace `XAEL_AUTH_TOKEN` (≥ 16 random characters).