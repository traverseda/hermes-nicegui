# LLM Config Refactor — Single Source of Truth

## Current State

### Config (Nix) — non-secret, visible, versioned:
- `hermesDeploy.llm` (llm.nix) with `provider`, `baseUrl`, `model`, `apiKeyEnvVar`
- Consumed by: hermes-service.nix (40-42), hindsight.nix (6-9), opencode.nix (13)
- Also consumed by: hermes-nicegui.nix (268-270), vm-configuration.nix (23-27)

### Secrets (12 agenix files — all encrypted):
- hermes-env.age → hermes-agent, dashboard, hass, nicegui env
- hindsight-env.age → hindsight container, opencode-go LLM key
- hass-env.age, api-server-env.age, dashboard-env.age, xaelwiki-env.age, xaelwiki-ssh.age, hermes-nicegui-env.age, hermes-nicegui-gateway-token.age
- cloudflare-tunnel.age, tailscale-auth.age (standalone)

### The bug:
- LLM key in hindsight-env as `HINDSIGHT_API_LLM_API_KEY`
- hermes-agent needs API key for custom endpoint — docs say use `OPENAI_API_KEY` from `.env`
- Opencode CLI has `"apiKey": ""` in `providers.vllm` config
- Result: 401 on vLLM (203s timeout)

## Design (confirmed by user)

### Providers (Nix)
```nix
hermesDeploy.providers = {
  local = {
    name = "local"; provider = "vllm"; model = "quanttrio/Qwen3.6-35b-a3b-awq";
    baseUrl = "http://192.168.193.96:8000/v1";
  };
  openrouter = {
    name = "openrouter"; provider = "openrouter"; model = "google/gemini-2.5-flash";
  };
};
hermesDeploy.defaultProvider = "local";
hermesDeploy.fallbackProviders = [ "openrouter" ];
```

### Secret: one `secrets/config.age` — ALL secrets unified
- Key name: `LLM_API_KEY` (not `VLLM_API_KEY`)
- Includes ALL 12 existing secrets merged into one file
- Decrypted to `/run/agenix/config`

### OpenRouter fallback
- Real fallback in Nix, not runtime-only
- Default model: `google/gemini-2.5-flash` (cheap, fast)
- Configured as `hermesDeploy.fallbackProviders`
- Each fallback entry has its own model and provider

## Implementation Steps

1. Create `modules/config.nix` — replace `llm.nix`
   - Options: `providers` (attrs-of-submodule), `defaultProvider`, `fallbackProviders`, `hindsight`
   - Submodule: `name`, `provider`, `model`, `baseUrl`, `models`

2. Create `secrets/config.age` — merge all 12 secrets
   - Extract all env vars from decrypted secrets on hermes.lan
   - Merge with consistent naming
   - Encrypt with same age recipients

3. Rewrite `hermes-service.nix` (225 lines → ~180 lines)
   - Remove `llm = config.hermesDeploy.llm`
   - Use `hermesDeploy.providers.${cfg.defaultProvider}` for model/provider/baseUrl
   - Generate `model.api_key: "\${env:LLM_API_KEY}"` in config.yaml
   - Pass `LLM_API_KEY` from `/run/agenix/config` via `services.hermes-agent.environment`

4. Rewrite `hindsight.nix` (444 lines → ~400 lines)
   - Remove `llm = config.hermesDeploy.llm`
   - Use `hermesDeploy.providers` for LLM env vars
   - Generate `HINDSIGHT_API_LLM_*` from provider config
   - Read `LLM_API_KEY` from `/run/agenix/config`

5. Rewrite `opencode.nix` (90 lines → ~100 lines)
   - Remove `llm = config.hermesDeploy.llm`
   - Use `hermesDeploy.providers` for model/provider
   - Read `LLM_API_KEY` from `/run/agenix/config` for `apiKey` in providers JSON

6. Rewrite `configuration.nix` (~497 lines → ~440 lines)
   - Replace all `age.secrets.*` with single `age.secrets.config`
   - Define `hermesDeploy.providers` + `hermesDeploy.defaultProvider`
   - Remove `hermesDeploy.llm` block (lines 330-334)
   - Update restartTriggers list

7. Rewrite `secrets/README.md`
   - New `config.age` section: single-file pattern
   - `agenix -e secrets/config.age` editing workflow
   - Remove old per-secret sections or update them

8. Update test files
   - `tests/vm-configuration.nix` → use new providers format
   - `tests/hindsight-config-check.nix` → new module import path

9. Update `flake.nix` (217 lines → 215 lines)
   - Replace `./modules/llm.nix` with `./modules/config.nix` in commonModules

10. Remove `llm.nix`

11. `nix flake check` + deploy
    - Fix any assertion errors
    - Deploy with `scripts/deploy.sh`

## Files Changed Summary

| File | Action | Lines |
|---|---|---|
| `modules/config.nix` | CREATE | ~60 |
| `modules/llm.nix` | DELETE | -41 |
| `secrets/config.age` | CREATE | - |
| `secrets/hermes-env.age` | DELETE | -1 |
| `secrets/hindsight-env.age` | DELETE | -1 |
| `secrets/hermes-service.nix` | EDIT | ~180 |
| `modules/hindsight.nix` | EDIT | ~400 |
| `modules/opencode.nix` | EDIT | ~100 |
| `hosts/hermes/configuration.nix` | EDIT | ~440 |
| `secrets/README.md` | EDIT | restructure |
| `tests/vm-configuration.nix` | EDIT | ~70 |
| `tests/hindsight-config-check.nix` | EDIT | ~70 |
| `flake.nix` | EDIT | ~215 |

## Risks
- `config.age` merging means one edit updates ALL secrets — operator must review carefully
- `config.age` is now a single point of failure — if lost, nothing can be decrypted without backups
- hermes-agent reads `LLM_API_KEY` from `.env` — ensure the env var is actually wired to the process
