# Secrets

Secrets are encrypted with [agenix](https://github.com/ryantm/agenix) and
committed to the repo as `.age` files. They are decrypted on the target host
at activation time into `/run/agenix/...` — never into `/nix/store` (which is
world-readable) and never into this repo in plaintext.

## Recipients

Every `.age` file is encrypted to **two** recipients:

| Recipient | Key file | Why |
| --------- | -------- | --- |
| Operator   | `~/.ssh/id_ed25519.pub`       | lets you `agenix -e` / decrypt on the build machine |
| The LXC    | `secrets/lxc-host-ed25519.pub` | decrypts at activation on the host |

`secrets/lxc-host-ed25519` (private, gitignored) is a pre-generated SSH host
keypair for the LXC. **It must be uploaded to the LXC as its SSH host key
during initial creation** (see below) — otherwise agenix can't decrypt.

> **Important:** recipients are the **raw SSH public keys** (`ssh-ed25519 AAAA…`),
> *not* the `age1…` strings from `ssh-to-age`. With age ≥1.3 an `ssh-to-age`
> (X25519) recipient cannot be decrypted using an SSH private-key identity,
> which is exactly what agenix uses on the host. This was verified empirically;
> encrypt with `age -e -r "$(cat key.pub)"` or `age -R key.pub`.

## Secrets in this repo

| File                     | Decrypts to           | Used by                             |
| ------------------------ | --------------------- | ----------------------------------- |
| `hermes-env.age`         | `/run/agenix/hermes-env` | Hermes `.env` (API keys, tokens)  |
| `tailscale-auth.age`     | `/run/agenix/tailscale-auth` | `services.tailscale.authKeyFile` |
| `hindsight-env.age`      | `/run/agenix/hindsight-env` | Hindsight container env file   |
| `api-server-env.age`     | `/run/agenix/api-server-env` | default profile `.env` (API_SERVER_KEY for the multiplexed api_server) |
| `dashboard-env.age`      | `/run/agenix/dashboard-env` | default profile `.env` (dashboard basic-auth credentials) |
| `cloudflare-tunnel.age`  | `/run/agenix/cloudflare-tunnel` | Cloudflare Tunnel connector token (`services.cloudflare-tunnel`) |
| `xaelwiki-env.age`       | `/run/agenix/xaelwiki-env` | xaelwiki MCP bearer token (`XAEL_AUTH_TOKEN`) |
| `xaelwiki-ssh.age`       | `/run/agenix/xaelwiki-ssh` | SSH deploy key for the notes vault (`services.xaelwiki`) |
| `hermes-nicegui-env.age` | `/run/agenix/hermes-nicegui-env` | hermes-nicegui kanban dashboard login (`services.hermes-nicegui`) |

`hermes-env` is a plain `KEY=value` file:

```
HINDSIGHT_API_KEY=<same value as HINDSIGHT_API_TENANT_API_KEY below>
OPENCODE_API_KEY=<opencode-go API key>
TAVILY_API_KEY=<tavily API key>
OPENROUTER_API_KEY=<openrouter API key>
```

`HINDSIGHT_API_KEY` is what the Hermes memory provider sends to the **local**
Hindsight API as `Authorization: Bearer` — keep it identical to the
`HINDSIGHT_API_TENANT_API_KEY` in `hindsight-env` (single source of truth for
the token). The URL Hermes uses is declarative, in
`hosts/hermes/configuration.nix` (`HINDSIGHT_API_URL=http://127.0.0.1:8888`),
*not* the old remote `https://hindsight-api.0u0.ca`.

`api-server-env` is a plain `KEY=value` file:

```
API_SERVER_KEY=<key clients send as `Authorization: Bearer` to the API server (min 16 chars)>
```

It gates the main gateway's multiplexed api_server (single listener owned by
the default profile) and is appended to that profile's `.env` via
`services.hermes-ha.apiServerKeyFile`. The `ha` profile served under
`/p/ha/` shares the listener and inherits provider keys from `hermes-env`.

`dashboard-env` is a plain `KEY=value` file gating the web dashboard
(`hermesagent.lan:9119`, tailnet-only). The dashboard binds a non-loopback
host, which always engages its auth gate; we use the bundled `basic`
username/password provider, configured purely through env vars (the
preferred `password_hash` form — no plaintext at rest):

```
HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin
HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=scrypt$16384$8$1$<salt_b64>$<dk_b64>
HERMES_DASHBOARD_BASIC_AUTH_SECRET=<32+ random bytes, base64>
```

The file is appended to the default profile's `.env` via
`services.hermes-dashboard.environmentFile`. To rotate the password:

```sh
nix develop
# compute a fresh scrypt hash:
python3 -c "import base64,hashlib,secrets; p=b'NEW_PASSWORD'; s=secrets.token_bytes(16); d=hashlib.scrypt(p,salt=s,n=2**14,r=8,p=1,dklen=32,maxmem=0); print(f'scrypt\$16384\$8\$1\${base64.b64encode(s).decode()}\${base64.b64encode(d).decode()}')"
agenix -e secrets/dashboard-env.age   # replace the _PASSWORD_HASH line, keep the rest
agenix --rekey -e secrets/dashboard-env.age
```

`hindsight-env` is also a plain `KEY=value` file:

```
HINDSIGHT_API_LLM_API_KEY=<key for the preferred model endpoint (opencode-go)>
HINDSIGHT_API_TENANT_API_KEY=<shared key clients send as `Authorization: Bearer`>
HINDSIGHT_CP_ACCESS_KEY=<optional; control-plane login, only if the dashboard is enabled>
```

The LLM *endpoint* and *model* are not secrets — they live in Nix as
`hermesDeploy.llm` (default: opencode-go `https://opencode.ai/zen/go/v1` with
`deepseek-v4-flash`). Only the key lives here.

`tailscale-auth.age` is **not managed in Nix** — the pre-auth key comes from
the Tailscale admin console and is left to the operator to fill in by hand
before first boot (an empty file is committed so the flake builds).

`cloudflare-tunnel.age` holds the Cloudflare Tunnel **connector token** for the
remotely-managed tunnel (`services.cloudflare-tunnel`). The tunnel + public
hostnames are configured in the Cloudflare dashboard; this file is just the
`cfut_…` token the LXC uses to dial out (`cloudflared tunnel run --token`).

To (re)generate the token: Cloudflare Zero Trust → Networks → Tunnels → your
tunnel → configure, or `cloudflared tunnel token <name>` (needs a `cert.pem`
from `cloudflared tunnel login`). Then re-encrypt it into the secret:

```sh
nix develop
printf '%s\n' '<cfut_...token...>' | \
  age -e -r "$(cat ~/.ssh/id_ed25519.pub)" -r "$(cat secrets/lxc-host-ed25519.pub)" \
  -o secrets/cloudflare-tunnel.age
```

No `tunnelId` is needed — the tunnel's identity and public hostnames live in
the dashboard, not in Nix config.

`xaelwiki-env` is a plain `KEY=value` file gating the xaelwiki notes MCP server:

```
XAEL_AUTH_TOKEN=<random token, min 16 chars>
```

The token is used by the server (HTTP bearer auth) and by hermes-agent's MCP
client (interpolated from `.env` at runtime — never stored in config.yaml).

`xaelwiki-ssh` is the raw SSH private key (ed25519, no surrounding text) for
the notes vault deploy key (`xaelwiki@notes.lan`, write-enabled on
`codeberg.org/traverseda/notes`). It lets the xaelwiki service clone and push
the shared notes repo. It must be kept in sync with the Codeberg/Forgejo deploy
key registration. To inspect or rotate either secret:

```sh
nix develop
agenix -e secrets/xaelwiki-env.age     # edit the token
agenix -e secrets/xaelwiki-ssh.age     # view/replace the deploy key
agenix --rekey -e secrets/xaelwiki-ssh.age
```

`hermes-nicegui-env` is a plain `KEY=value` file gating the hermes-nicegui
kanban plugin's login to the Hermes dashboard web server (`services.hermes-nicegui`):

```
HERMES_KANBAN_USERNAME=admin
HERMES_KANBAN_PASSWORD=<plaintext password>
```

This is the *plaintext* username/password the browser logs into the dashboard
with (cookie session) — the `dashboard-env` secret only stores the scrypt
hash, which the dashboard's own auth gate uses, so it can't be reused here.
Keep the two in sync. To rotate:

```sh
nix develop
agenix -e secrets/hermes-nicegui-env.age
agenix --rekey -e secrets/hermes-nicegui-env.age
```

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

2. Editing/rotating a secret (dev shell):

   ```sh
   nix develop
   agenix -e secrets/hermes-env.age        # edit hermes-env
   agenix -e secrets/hindsight-env.age     # edit hindsight keys
   agenix -e secrets/tailscale-auth.age    # edit tailscale auth key
   ```

   agenix decrypts with your local identity and re-encrypts to the recipients
   in `secrets/secrets.nix`. If you don't use a `secrets.nix`, re-encrypt
   manually with `age -e -r "$(cat ~/.ssh/id_ed25519.pub)" -r "$(cat secrets/lxc-host-ed25519.pub)"`.

3. Commit the `.age` files. Deploy; the LXC decrypts them on activation.

## Rotating / rekeying

```sh
nix develop
agenix --rekey -e secrets/hermes-env.age
```
