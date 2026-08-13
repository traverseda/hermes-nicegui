# Secrets

Secrets are encrypted with [agenix](https://github.com/ryantm/agenix) and
committed to the repo as `.age` files. They are decrypted on the target host
at activation time into `/run/agenix/...` — never into `/nix/store` (which is
world-readable) and never into this repo in plaintext.

## Secrets in this repo

| File                     | Decrypts to           | Used by                             |
| ------------------------ | --------------------- | ----------------------------------- |
| `hermes-env.age`         | `/run/agenix/hermes-env` | Hermes `.env` (API keys, tokens)  |
| `tailscale-auth.age`     | `/run/agenix/tailscale-auth` | `services.tailscale.authKeyFile` |
| `hindsight-env.age`      | `/run/agenix/hindsight-env` | Hindsight container env file   |

`hermes-env` is a plain `KEY=value` file, e.g.:

```
OPENROUTER_API_KEY=sk-or-...
ANTHROPIC_API_KEY=sk-ant-...
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

> **Note:** `secrets/hindsight-env.age` currently contains a placeholder
> encrypted to your *local* key so the flake builds. Before deploying to the
> LXC, re-encrypt it to the LXC's age pubkey (see Setup, step 1) or activation
> will fail on the target.

## Setup

1. Generate an age key for the **target host** (the LXC). agenix uses the
   host's SSH ed25519 key by default:

   ```sh
   nix-shell -p ssh-to-age --run 'ssh-to-age < /etc/ssh/ssh_host_ed25519_key.pub'
   ```

2. Create/update `secrets/secrets.nix` mapping identities to recipients.
   Start with:

   ```nix
   {
     # Replace with the pubkey(s) you want to decrypt.
     #   local host:  nix-shell -p ssh-to-age --run 'ssh-to-age < ~/.ssh/id_ed25519.pub'
     #   lxc host:    as above, from the LXC
     identityPaths = [ "/etc/ssh/ssh_host_ed25519_key" ];
     recipients = [ <your-age-pubkey> ];
     secrets = {
       "hermes-env" = { };
       "tailscale-auth" = { };
       "hindsight-env" = { };
     };
   }
   ```

3. Generate the `.age` files from the dev shell:

   ```sh
   nix develop
   agenix -e secrets/hermes-env.age        # edit hermes-env
   agenix -e secrets/tailscale-auth.age    # edit tailscale auth key
   agenix -e secrets/hindsight-env.age     # edit hindsight keys
   ```

4. Commit the `.age` files. Deploy; the LXC decrypts them on activation.

## Rotating / rekeying

```sh
nix develop
agenix --rekey -e secrets/hermes-env.age
```
