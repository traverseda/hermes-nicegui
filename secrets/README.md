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
