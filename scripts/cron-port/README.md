# Porting the old crontab manager from hermesagent.lan

This directory holds the migration of the legacy cron system that ran on
**hermesagent.lan** (the previous Hermes box, 192.168.0.201) into this
deployment. The port was tracked as kanban task `t_58515b75` (2026-08-15).

## What the old "crontab manager" was

The old crontab manager was **not** a bespoke cron daemon — it was the cron
subsystem of Hermes Agent itself, an older snapshot of the same code that runs
here today:

| | Old box (hermesagent.lan) | This box |
|---|---|---|
| Package | hermes-agent **0.20.0** (git checkout `~/.hermes/hermes-agent`, tag `v2026.8.3-1455-gfa83af3f9`) | hermes-agent **0.20.1** (Nix store) |
| Cron store | `~/.hermes/cron/jobs.json` (10 jobs, `{"jobs": [...]}`) | `~/.hermes/cron/jobs.json` (same schema) |
| Scheduler | gateway ticker + `hermes_cli/cron.py` CLI | gateway ticker + `hermes_cli/cron.py` CLI (**byte-identical** to the old file) |
| Usage audit | `~/.hermes/cron/usage_audit.jsonl` | same path, written by `cron/scheduler.py::_usage_audit_path` |
| Support scripts | `~/.hermes/scripts/*` (fitness, food, sailboat, cron-usage-summary) | `~/.hermes/scripts/*` |
| System crontab | one `@reboot` tailscaled entry only | nix flake update entry |

So the "port" is a **field-level migration of state and dependencies**, not a
code rewrite: `tools/cronjob_tools.py` (1619 lines old vs 1687 lines current)
differed by only ~80 lines, and `hermes_cli/cron.py` is byte-identical. The
old manager's feature set (list / add / remove / pause / resume / run / edit /
status / tick, cron + interval + once schedules, script-only `no_agent` jobs,
per-job skills, delivery targets, repeat counts) is fully present in the
current codebase — the current `cronjob` tool and `hermes cron` CLI are its
direct successors.

## Feature inventory of the old manager (10 jobs)

| Old id | Name | Schedule | Kind | Deps on old box |
|---|---|---|---|---|
| `ee2bbc93c7b2` | arr-queue-nightly-check | `0 2 * * *` | agent | arr-stack skill + `arr` CLI + creds |
| `beca0bd83532` | Tunarr Monthly Library Sync | `0 4 1 * *` | agent | tunarr CLI + arr-stack |
| `8e4e00013457` | Monthly Show Triage | `0 2 25 * *` | agent | arr-stack skill + creds |
| `38475d6b630e` | cronjob-self-review | `0 6 * * 1` | agent | `cron-usage-summary.py` script |
| `9e8fe0d6a12e` | Note organization | `0 4 * * *` | agent | xaelwiki MCP (`mcp__xael__get_prompt`) |
| `94165a4948c7` | Sailboat Scan Nightly | `0 2 * * *` | script-only | `~/workspace/sailboatpurchase/` service |
| `1f4925e6fbe1` | Food Log Reset | `5 0 * * *` | script-only | `reset-food-today.sh` + HA |
| `4bb816aecdfe` | Fitness Assistant Weekly | `0 20 * * 0` | agent | calorie-tracking skill + `fitness_snapshot.py` |
| `8270b3c221e7` | Tailscale token rotation reminder | once 2026-11-05 | agent | — (host-specific nag) |
| `95de575a47b9` | Fitness Planner Daily | `30 20 * * *` | script-only | `fitness_planner.py` + HA |

**Storage format:** one JSON file (`jobs.json`) with a `jobs` array; each job
carries `id`, `name`, `prompt`, `skills`, `schedule` (`{kind: cron|interval|
once, ...}`), `repeat`, `enabled`, `deliver`, `origin`, runtime fields
(`next_run_at`, `last_run_at`, `last_status`, `fire_claim`). The import tool
preserves the definition fields and resets the runtime fields (see below).

## What was migrated and how (migration map)

| Old job | New id | Status here | Notes |
|---|---|---|---|
| Food Log Reset | `85b37324ee31` | **active** | already migrated by the calorie-tracking port (2026-08-15, earlier ticket); same schedule |
| Fitness Planner Daily | `f4678c9ee1ed` | **active** | ditto |
| Fitness Assistant Weekly | `84870b9fc7d4` | **active** | ditto |
| cronjob-self-review | `38475d6b630e` | **active** | imported; `cron-usage-summary.py` ported to `~/.hermes/scripts/` |
| Note organization | `9e8fe0d6a12e` | **active** | imported; prompt rewritten `mcp__xael__get_prompt` → `mcp__xaelwiki__get_prompt` (this box's MCP server name); the `organize` prompt exists here |
| arr-queue-nightly-check | `ee2bbc93c7b2` | **paused** | needs arr-stack skill + `arr` CLI + credentials, not provisioned here |
| Tunarr Monthly Library Sync | `beca0bd83532` | **paused** | needs tunarr CLI + credentials |
| Monthly Show Triage | `8e4e00013457` | **paused** | needs arr-stack skill + credentials |
| Sailboat Scan Nightly | `94165a4948c7` | **paused** | the `sailboatpurchase` workspace/venv/gateway-API service is a separate legacy service (out of scope); only the cron wrapper exists |
| Tailscale token rotation reminder | — | **not ported** | host-specific one-shot nag about a `TAILSCALE_API_TOKEN` that does not exist here (this box uses OAuth client credentials, `TAILSCALE_CLIENT_ID/SECRET`, which auto-rotate) |

All imports landed with `deliver` remapped `discord:*` → `local` (this box has
a Discord platform but the old chat ids are stale), `origin` dropped, and
runtime fields reset (`next_run_at` recomputed by the scheduler's next tick).

### Secrets

The old prompts and skills embedded live credentials (Sonarr/Radarr/Prowlarr
API keys, Jellyfin tokens, an LDAP basic-auth password). The port **redacts**
them — see `redact-legacy-secrets.py`. Nothing in this repo (manifest, ported
skills) contains a live credential; the ported `arr-stack` skill documents the
intended runtime convention: keys live in `~/.hermes/.env` (`ARR_*` /
`JELLYFIN_*`) and `~/.hermes/internal-creds.json`.

## Files

- `import-old-cron.py` — imports a legacy `jobs.json` into the local store.
  Definition fields preserved; runtime fields reset; deliver targets remapped
  (`--deliver-map`, default `discord:*=local`); idempotent (`--replace` to
  overwrite); atomic write under the scheduler's `.jobs.lock`; disabled
  (paused) source records keep their pause markers.
- `legacy-jobs-import.json` — the migration manifest: the 6 ported jobs,
  redacted + adapted, as imported on 2026-08-15.
- `redact-legacy-secrets.py` — scrubs known credentials + generic credential
  patterns from a directory tree, then fails if anything is left.
- `cron-usage-summary.py` — ported token-usage summary (reads
  `~/.hermes/cron/usage_audit.jsonl`), installed to `~/.hermes/scripts/`.
- `xaelwiki-prompts/` — the xaelwiki prompt files (`capture`, `organize`,
  `outline`) fetched from the old box's vault; installed at the vault root
  (`/var/lib/xaelwiki/prompts/`) so the ported Note organization job's
  `mcp__xaelwiki__get_prompt` dependency resolves.
- `test_import_old_cron.py`, `test_redact_legacy_secrets.py` — stdlib
  unittest suites; run with `python3 <file> -v` (no network needed).

## Usage

```bash
# Re-run the migration (idempotent; backs up the store first):
python3 import-old-cron.py --source legacy-jobs-import.json --dry-run
python3 import-old-cron.py --source legacy-jobs-import.json

# Import from a fresh pull of the old store (with secret redaction):
python3 redact-legacy-secrets.py /path/to/old-cron-dir /tmp/redacted
python3 import-old-cron.py --source /tmp/redacted/jobs.json

# Inspect the result:
hermes cron list --all        # or the cronjob(action='list') tool
```

### Resuming the paused jobs (arr / tunarr / sailboat)

1. **arr jobs**: provision the credentials — add `ARR_SONARR_API_KEY`,
   `ARR_RADARR_API_KEY`, `ARR_PROWLARR_API_KEY`, `JELLYFIN_*` to
   `~/.hermes/.env` (and basic-auth creds to `~/.hermes/internal-creds.json`
   if the nginx proxy still requires them), install the `arr` CLI
   (`~/workspace/arr-stack`), then:
   `hermes cron resume ee2bbc93c7b2` (and `8e4e00013457`).
2. **tunarr job**: install the tunarr CLI at `~/.hermes/scripts/tunarr`
   (see `skills/media/tunarr/SKILL.md`), provision creds, then
   `hermes cron resume beca0bd83532`.
3. **sailboat job**: port the `sailboatpurchase` workspace (data source +
   venv + gateway-API integration) — a separate service port, then
   `hermes cron resume 94165a4948c7`.

The ported `arr-stack` / `tunarr` / `sonarr-add-show` skills live in
`skills/media/` (redacted) and are picked up by `hermes-skills.nix`
`external_dirs` on the next deploy.

## Verification

- `python3 test_import_old_cron.py` (23 tests) — import semantics, pause
  preservation, manifest round-trip (no secrets, correct enabled/paused split).
- `python3 test_redact_legacy_secrets.py` (10 tests) — redaction, JSON
  parseability (no trailing note in `.json`), verification of unprocessed
  suffixes (`.sh` etc.), placeholder skip.
- Post-import: `cronjob(action='list')` shows 10 jobs (4 pre-existing + 6
  ported); the 2 active ported jobs get `next_run_at` on the scheduler's next
  tick; the 4 paused jobs stay `state=paused`.
