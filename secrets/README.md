# Secrets

Secrets are encrypted with [agenix](https://github.com/ryantm/agenix) and
committed to the repo as `.age` files. They are decrypted on the target host
at activation time into `/run/agenix/...` — never into `/nix/store` (which is
world-readable) and never into this repo in plaintext.

## Unified `config.age`

The bulk of secrets are merged into a single `secrets/config.age` file. This
contains all LLM/API keys, dashboard authentication, API server keys, and other
shared secrets — decrypted to `/run/agenix/config`. A single `agenix -e
secrets/config.age` edits everything at once. To rotate a specific key, edit its
line in the decrypted file and re-encrypt.

**Contents of `config.age` (decrypted `/run/agenix/config`):**

```
LLM_API_KEY=<key for the preferred model endpoint (vLLM, opencode-go)>
HINDSIGHT_API_LLM_API_KEY=<key for the preferred model endpoint (opencode-go)>
HINDSIGHT_API_TENANT_API_KEY=<shared key clients send as `Authorization: Bearer`>
HINDSIGHT_CP_ACCESS_KEY=<optional; control-plane login, only if the dashboard is enabled>
HINDSIGHT_CP_DATAPLANE_API_KEY=<same value as HINDSIGHT_API_TENANT_API_KEY>
HINDSIGHT_API_KEY=<same value as HINDSIGHT_API_TENANT_API_KEY, sent by hermes-agent memory provider>
OPENCODE_API_KEY=<opencode-go API key>
TAVILY_API_KEY=<tavily API key>
OPENROUTER_API_KEY=<openrouter API key>
AGENTMAIL_API_KEY=<agentmail API key>
TAILSCALE_CLIENT_ID=<tailscale OAuth client id>
TAILSCALE_CLIENT_SECRET=<tailscale OAuth client secret>
OPENCODE_GO_API_KEY=<opencode-go API key>
DISCORD_BOT_TOKEN=<discord bot token>
DISCORD_ALLOWED_USERS=<comma-separated discord user ids>
DISCORD_HOME_CHANNEL=<discord channel id>
HASS_URL=https://hearth.0u0.ca/
HASS_TOKEN=<Home Assistant long-lived access token>
API_SERVER_KEY=<key for the multiplexed api_server>
HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin
HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=scrypt$16384$8$1$<salt_b64>$<dk_b64>
HERMES_DASHBOARD_BASIC_AUTH_SECRET=<32+ random bytes, base64>
XAEL_AUTH_TOKEN=<random token, min 16 chars>
HERMES_KANBAN_USERNAME=admin
HERMES_KANBAN_PASSWORD=<plaintext password for dashboard login>
HERMES_API_TOKEN=<bearer token for nicegui → gateway api_server calls>
```

`HERMES_API_TOKEN` must equal `API_SERVER_KEY` (it is the same key, stored
under two names for different consumers). The nicegui kanban plugin uses
`HERMES_KANBAN_USERNAME/KANBAN_PASSWORD` (plaintext) to cookie-login into the
dashboard; the dashboard itself uses the scrypt hash from
`HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH`. Keep the two in sync.

To edit `config.age`:

```sh
nix develop
agenix -e secrets/config.age        # edit all merged secrets
agenix --rekey -e secrets/config.age # re-encrypt with updated recipients
```

To rotate a specific key, edit its line in the decrypted editor, verify the
format matches the comments below, then save & exit agenix.

## Standalone secrets (operator-provisioned)

These secrets are NOT merged into `config.age` because they are minted
externally (by operators, not derived from the config merge):

| File                     | Decrypts to             | Used by                             |
| ------------------------ | ----------------------- | ----------------------------------- |
| `tailscale-auth.age`     | `/run/agenix/tailscale-auth` | `services.tailscale.authKeyFile` |
| `cloudflare-tunnel.age`  | `/run/agenix/cloudflare-tunnel` | Cloudflare Tunnel connector token |
| `xaelwiki-ssh.age`       | `/run/agenix/xaelwiki-ssh` | SSH deploy key for the notes vault |

## Recipients

Every `.age` file is encrypted to **two** recipients:

| Recipient | Key file | Why |
| --------- | -------- | --- |
| Operator   | `secrets/operator-pubkey.pub` | lets you `agenix -e` / decrypt on the build machine |
| The LXC    | `secrets/lxc-host-ed25519.pub` | decrypts at activation on the host |

The operator pubkey is pulled from `root@hermes.lan` at first deploy:

```sh
ssh root@hermes.lan "grep ssh-ed25519 /root/.ssh/authorized_keys" \
  > secrets/operator-pubkey.pub
```

`secrets/lxc-host-ed25519` (private, gitignored) is a pre-generated SSH host
keypair for the LXC. **It must be uploaded to the LXC as its SSH host key
during initial creation** (see below) — otherwise agenix can't decrypt.

> **Important:** recipients are the **raw SSH public keys** (`ssh-ed25519 AAAA…`),
> *not* the `age1…` strings from `ssh-to-age`. With age ≥1.3 an `ssh-to-age`
> (X25519) recipient cannot be decrypted using an SSH private-key identity,
> which is exactly what agenix uses on the host. This was verified empirically;
> encrypt with `age -e -r "$(cat key.pub)"` or `age -R key.pub`.

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

2. Editing secrets (dev shell):

   ```sh
   nix develop
   agenix -e secrets/config.age           # edit unified config (all secrets)
   agenix -e secrets/tailscale-auth.age   # edit tailscale auth key (standalone)
   agenix -e secrets/cloudflare-tunnel.age # edit CF tunnel token (standalone)
   agenix -e secrets/xaelwiki-ssh.age     # edit xaelwiki SSH key (standalone)
   ```

   agenix decrypts with your local identity and re-encrypts to the recipients
   in `secrets/secrets.nix`. If you don't use a `secrets.nix`, re-encrypt
   manually with `age -e -r "$(cat ~/.ssh/id_ed25519.pub)" -r "$(cat secrets/lxc-host-ed25519.pub)"`.

3. Commit the `.age` files. Deploy; the LXC decrypts them on activation.

## Rotating / rekeying

```sh
nix develop
agenix --rekey -e secrets/config.age
```

## Per-key rotation guides

### Discords tokens
Edit `DISCORD_BOT_TOKEN`, `DISCORD_ALLOWED_USERS`, `DISCORD_HOME_CHANNEL` in
`config.age`. Mint a new token in the Discord Developer Portal, replace the
value.

### Home Assistant token
Edit `HASS_TOKEN` in `config.age`. Mint a new token at Home Assistant →
Profile → Security → Long-lived access tokens.

### API_SERVER_KEY
Edit `API_SERVER_KEY` in `config.age`. Must be ≥ 16 characters. Also update the
`HERMES_API_TOKEN` line to the same value (they must match).

### Dashboard password
Edit `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH` and optionally the
`HERMES_DASHBOARD_BASIC_AUTH_SECRET`. Generate a fresh hash (the scrypt
format is documented in the file itself).

### XAEL_AUTH_TOKEN
Edit `XAEL_AUTH_TOKEN` in `config.age`. Must be ≥ 16 random characters.
