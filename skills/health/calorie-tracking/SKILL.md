---
name: calorie-tracking
description: "Use when logging food/macros into HA or running the fitness planner (todo.fitness sync, workout log)."
version: 1.2.2
author: hermes
license: MIT
metadata:
  hermes:
    tags: [health, fitness, home-assistant, food-log]
---

# Calorie Tracking

Log the user's food intake into HA with repeat-meal memory. Meals tend to repeat — always check the food library first.
Use whenever the user reports what they ate, asks to log a meal, wants
calorie/macro totals for the day, or references the food log/dashboard.
Not for weight or body metrics — those live in HA directly.

## Deployment (this box — ported Aug 2026)

- HOME is `/var/lib/hermes` on this box; all `~/` paths below resolve there.
- Canonical sources live in the deploy flake at
  `skills/health/calorie-tracking/` (SKILL.md, scripts/, data/, REVIEW.md).
  Edit there, commit, deploy; then re-sync runtime copies with:
  `bash skills/health/calorie-tracking/install.sh` (installs to
  `~/.hermes/scripts/`, `~/workspace/foodlog/`, `~/workspace/notes-and-research/fitness/`).
- Runtime cron scripts: `~/.hermes/scripts/{fitness_snapshot.py,fitness_planner.py,fitness_targets.py,weekly_fitness_collect.py,reset-food-today.sh}`.
- Tool + library: `~/workspace/foodlog/{food_log.py, foods.json}`.
- Logs: `~/workspace/notes-and-research/fitness/{food-log.jsonl, workouts.jsonl}`.
- Dependencies: python3 (added to the gateway PATH via the hermes-skills module
  `extraPackages`) + curl (already present). All scripts are stdlib-only.
- HA is reached at `http://hearth.lan` (LAN). The public URL in
  `HASS_URL` (`https://hearth.0u0.ca/`) is Cloudflare-fronted and rejects
  script API calls — never use it in scripts. `HASS_URL`/`HASS_TOKEN` are in
  `~/.hermes/.env`.
- `fitness_targets.json` is deliberately NOT installed on this box (stale —
  see REVIEW.md). Targets come from HA or are derived from data.

## Tooling

- Script: `~/workspace/foodlog/food_log.py` (stdlib Python, no deps)
- Log file (source of truth): `~/workspace/notes-and-research/fitness/food-log.jsonl` (append-only JSONL)
- Food memory library: `~/workspace/foodlog/foods.json` (name → macros, auto-upserted with `--remember`)
- HA display: 8 `input_number` helpers + 8 template sensors (see Entities below)
- `input_boolean.food_logged_today` — the "missing" signal: ON = ≥1 entry logged today; OFF = no record. Scripts set it; the 4 `*_today` sensors + `calorie_balance` render `unknown` (—) while OFF, so unlogged days show as missing/gaps instead of a misleading 0.
- Midnight reset cron: `Food Log Reset` (script-only, silent) — zeroes the 4 `*_today` helpers AND turns the logged-today flag off. Cron expr `5 0 * * *` = 00:05 LOCAL (box TZ America/Halifax since 2026-08-16, matching HA; phase-in note: `5 3 * * *` was used while the box still ran UTC — see REVIEW.md item 13).
- Fitness Daily Planner cron: `Fitness Daily Planner` (daily 20:00, job `c596695bcb85`) — a goal-directed workout planner. Prompt starts with: "**Goal: Based on what the user has done and how it went, set a direction for next time.**" Uses `fitness_daily_check.py` as gatekeeper (script field) — runs before the agent, exits early when nothing to do (empty stdout suppresses agent, saves tokens). When triggered, outputs context: user profile, workout gap, session feedback (RPE + notes), current todo items, workout history. Agent sets a GOAL/DIRECTION on todo.fitness (not prescriptive exercise prescriptions) and writes it via HA REST API. Delivers to origin (surfaces to user, not just local file). Replaced `Fitness Assistant Weekly` (removed 2026-08-18) and absorbed the planning role from `Fitness Planner Daily` (which still runs at 20:30 as a pure archivist sync).

## Targets — HA is the source of truth

Daily targets live in HA, NOT on disk (user directive, Aug 2026):
- `input_number.target_calories` (kcal) — e.g. 1600
- `input_number.target_protein` (g) — e.g. 86

Read them with `ha_get_state` before quoting any target; set them with `input_number.set_value` when the user changes mode. `fitness_targets.json` on disk is NOT authoritative — it was left at bulk 2700 kcal / 145 g after the user moved to maintenance (Aug 2026) and suggestions built on it were wrong. When recommending meals: remaining = target − `input_number.food_*_today`, then pick from the food library.

### Auto-adjusting targets (fitness_targets.py)

`fitness_targets.py` reads body composition from HA (Withings sensors) and pushes updated calorie/protein targets using Katch-McArdle TDEE. Run it daily or whenever body comp changes.

```bash
python3 ~/.hermes/scripts/fitness_targets.py              # push targets to HA
python3 ~/.hermes/scripts/fitness_targets.py --dry-run     # preview without pushing
python3 ~/.hermes/scripts/fitness_targets.py --mode cut    # override mode
```

Mode is stored in `~/.hermes/scripts/fitness_mode.json`: `{"mode": "bulk"}` or `{"mode": "cut"}`. Default is bulk. Edit the file to switch modes, or pass `--mode` on the CLI.

**Katch-McArdle formula**: BMR = 370 + (21.6 × lean_mass_kg). TDEE = BMR × 1.55 (moderate activity, 2x/week gym). Bulk adds 400 kcal surplus; cut subtracts 400 kcal deficit. Protein: 2.0g/kg lean mass (bulk), 2.2g/kg (cut). Sanity clamps: calories 1500–4000, protein 100–300g.

## Core workflow (every meal log)

1. **Check the food library first** (meals repeat!):
   ```bash
   python3 ~/workspace/foodlog/food_log.py foods --search "butter chicken"
   ```
2. **If found** — re-log with one command (no macro lookup needed):
   ```bash
   python3 ~/workspace/foodlog/food_log.py add --food "butter chicken"
   ```
   Case-insensitive substring match; multiple hits print candidates and exit 2.
3. **If NOT found** — look up nutrition (label/package/web; ask the user for portion if unclear), log with explicit macros + `--remember` so it's one command next time:
   ```bash
   python3 ~/workspace/foodlog/food_log.py add \
     --item "chicken breast 200g" --kcal 330 --protein 62 --carbs 0 --fat 7 \
     --confidence weighed --remember
   ```
   `--confidence` ∈ `weighed|estimated|guessed`. `weighed` = label/scale-known, `estimated` = typical portion, `guessed` = unsure portion.

## Commands

| Command | Purpose |
|---|---|
| `add --item NAME --kcal N --protein N --carbs N --fat N [--confidence C] [--remember]` | Log a meal (repeatable `--item` flags for multi-item meals) |
| `add --food NAME [--remember]` | Re-log from library |
| `today` | Print today's totals |
| `status` | Show log path + entry count |
| `edit --item N [--kcal N ...]` / `edit --item N --remove` | Fix/delete an item, re-pushes totals |
| `undo` | Remove last entry, re-pushes |
| `foods [--search TERM]` | List/search the library |
| `DRY_RUN=1 <cmd>` | Preview without writing anything |

Every `add`/`edit`/`undo` recomputes today + lifetime totals, pushes them to the HA helpers, and syncs `input_boolean.food_logged_today` (on if any entry exists for today). After logging, confirm the new total to the user (e.g. "2,893 kcal · 109 g protein").

**Missing vs 0 semantics**: 0 kcal is meaningful only if the user actually ate nothing; unrecorded days must read as missing. The flag → `unknown` chain handles this. The `*_consumed` sensors (lifetime, `total_increasing`) feed permanent statistics and are NEVER reset — they're the long-term record; `*_today` sensors are live gauges that go unknown when unlogged.

## Nutrition estimation defaults (when user gives no numbers)

- Restaurant/fast food: look up the item (Costco hot dog = 570/24/46/33)
- Typical portion, no label: estimate from comparable serving
- 200g bag of Goldfish ≈ 933 kcal; 175g cooked rice ≈ 230 kcal; garlic naan ≈ 260 kcal
- If genuinely unknown, ask the user for portion/weight rather than inventing

## HA entities

Helpers (storage, Hermes writes): `input_number.food_{calories,protein,carbs,fat}_{today,lifetime}`
Template sensors (display + permanent stats):
- `sensor.food_*_consumed` — `state_class: total_increasing`, feeds HA long-term statistics (kept forever)
- `sensor.food_*_today` — `state_class: measurement`, live gauges
- `sensor.calorie_balance` — consumed − burned (live template)

Body composition (Withings scale, `state_class: measurement`):
- `sensor.withings_fat_ratio` — body fat % (primary source)
- `sensor.withings_fat_mass` — fat mass in kg
- `sensor.withings_fat_free_mass` — lean / fat-free mass in kg
- `sensor.withings_weight` — body weight in kg

Alternative body fat source (synced from Withings via Pixel 8 Health Connect):
- `sensor.pixel_8_body_fat` — body fat % (same underlying measurement, via Pixel 8 integration)

Use `sensor.withings_fat_ratio` as the primary body fat entity. `sensor.pixel_8_body_fat` is an alternative if the Withings integration is down; both read ~16.6%. Lean mass = `sensor.withings_fat_free_mass` (preferred) or weight × (1 − fat_ratio/100).

Fitness dashboard (`dashboard-fitness`, storage mode): Today view has gauges + burned/consumed chart; Trends view has grouped macro columns + total kcal line; the **Gym session** tab (path `feedback`, icon `mdi:dumbbell`) holds the "Next workout" todo list AND session feedback together (see the effort-rating bullet). A JSON backup of the dashboard config is in the flake at `skills/health/calorie-tracking/data/dashboard-fitness.json`.

## Fitness Planner (Archivist)

The `fitness_planner.py` script is the REACTIVE archivist. It runs daily 20:30 as a silent no-agent cron (`Fitness Planner Daily`, delivers to Discord fitness channel only when something changed). It records what the user did and NEVER prescribes — no sessions, exercises, weights, due dates, routines, or targets. The todo.fitness list is managed by the Fitness Daily Planner (above); the archivist only syncs completed items from todo to workouts.jsonl.

- Script: `~/.hermes/scripts/fitness_planner.py` (canonical copy in the flake at `skills/health/calorie-tracking/scripts/`). Config (optional): `~/.hermes/scripts/fitness_config.json` → `{"todo_entity": "todo.fitness"}` (env `FITNESS_TODO_ENTITY` wins). No routine config exists — `fitness_routine.json` was deleted (v2 leftover).
- Commands: `sync` (default; cron entrypoint — archives completed items to workouts.jsonl then removes them; silent when nothing changed), `log --session NAME [--date YYYY-MM-DD]` (chat path — records the workout, removes any open item matching the name), `next` (factual: last session + median gap, NO recommendation), `status`. Always honor `DRY_RUN=1`; env overrides `WORKOUTS_LOG_PATH`/`WORKOUTS_PATH`, `FITNESS_CONFIG_PATH`, `FITNESS_TODO_ENTITY`.
- Log: `~/workspace/notes-and-research/fitness/workouts.jsonl` (append-only JSONL: ts/date/session/source=todo|chat).
- Agent behavior: user reports a workout → ALWAYS ask for weights and reps per exercise before logging. Format: `fitness_planner.py log --session "Exercise: weight x reps x sets, Exercise 2: ..."`. Example: `fitness_planner.py log --session "Bench press: 60kg x 8 x 3, Squat: 50kg x 10 x 3"`. If the user gives exercise names only (e.g. "bench, squats"), ASK what weight/reps they used before logging — do NOT log bare exercise names without load data. User checks off an item in HA → next sync logs it (source=todo, date=item due) and removes it. Empty stdout = silent cron. Do NOT invent sessions, exercises, sets, or weights for the user — ever.

## Fitness Daily Planner (Goal-Directed)

The `Fitness Daily Planner` cron (daily 20:00) is the GOAL-DIRECTED system. Its goal: **Based on what the user has done and how it went, set a direction for next time.** This replaced the passive `Fitness Assistant Weekly` (removed 2026-08-18) and was updated to be goal-directed (not prescriptive) on 2026-08-20.

Architecture:
- **Gatekeeper** (`~/.hermes/scripts/fitness_daily_check.py`): cheap, no-LLM Python script that runs BEFORE the agent. Reads current todo.fitness, compares to a stored snapshot (`~/.hermes/scripts/fitness_daily_snapshot.json`), and decides whether the agent should run. Triggers on: todo changed, new feedback/RPE, todo stale ≥7 days, or empty todo. Outputs context block with user profile, workout gap, feedback, history, and decision guidance. The snapshot stores `feedback` and `rpe` fields to detect new feedback between runs.
- **Agent prompt**: starts with the goal statement. Receives gatekeeper output as context. Reads workout history (workouts.jsonl), feedback (RPE + notes from HA), body metrics (weight from `sensor.withings_weight`, body fat from `sensor.withings_fat_ratio`, lean mass from `sensor.withings_fat_free_mass`; see HA entities above). Sets a GOAL/DIRECTION on todo.fitness (not prescriptive exercise prescriptions). Writes via HA REST API. Clears feedback fields after processing (RPE → 5, notes → empty). Delivers response to origin (surfaces to user).
- **Todo format**: Goals, not prescriptions. "Goal: Progress squat from 40 lbs — try 45 lbs next session" NOT "Session B - Goblet squat: 14 kg dumbbell, 3 sets x 8-12". The user decides exercises, sets, and reps at the gym.
- **Feedback loop**: RPE 1-3 → push harder next time; RPE 7-8 → solid, keep progressing; RPE 9-10 → back off, rebuild gradually. Notes → inform what to focus on. Feedback is one-shot.
- HA todo API mechanics (verified on this instance, do not "fix"): response services (`get_items`) require `?return_response=true` as a QUERY PARAMETER on the REST URL — putting it in the JSON body returns 400. Fire-and-forget writes (`add_item`, `update_item`, `remove_item`) REJECT `return_response` (400). The field for identifying items is `item` (not `uid` or `summary`): `add_item` takes `{"entity_id": "...", "item": "summary text"}`, `remove_item` takes `{"entity_id": "...", "item": "<uid>"}`. Base URL `http://hearth.lan`, never https (Cloudflare).
- **Effort rating widget (added 2026-08-15; merged with the todo list 2026-08-16)**: `input_number.workout_effort_rpe` (RPE 1–10, step 0.5) plus `input_text.workout_feedback` (255-char freeform notes). Both live on the **"Gym session" tab** (path `feedback`, icon `mdi:dumbbell`) in dashboard-fitness, in the "Session feedback" section — the same tab's FIRST section is the "Next workout" heading + `todo-list` (`todo.fitness`). The user's "next workout to do" list and the post-session feedback share one tab (user directive 2026-08-16: "This is going to be the gym session tab"). The Today view does NOT contain the workout section. RPE is a tile card with feature `{"type": "numeric-input", "style": "slider"}` — do NOT use `{"type": "slider"}` or `direction`, those are invalid feature configs and break the render (config error, verified 2026-08-15). Notes are an entities card. The user rates each session there after checking off todo items and leaves freeform notes (e.g. "goblet squat felt easy, push-ups hard"); read both with `ha_get_state`/`ha_call_service` and treat them as THEIR signal — never infer effort from HR (HR during lifting tracks conditioning/metabolic stress, not mechanical load; user explicitly keeps Withings weight goal as non-authoritative). **Wipe after processing**: whenever the bot reads the feedback to build the next session, it MUST clear `input_text.workout_feedback` (set_value "") and reset `input_number.workout_effort_rpe` to 5 — feedback is one-shot, never carry it into the next cycle.
- **Gatekeeper (`fitness_daily_check.py`) must detect feedback changes** (incident 2026-08-20): the gatekeeper originally only triggered on todo list changes and staleness (7+ days). User left workout feedback after the 20:00 cron ran; next day's gatekeeper saw no todo changes, suppressed the agent, and the feedback sat unprocessed. Fixed by adding `feedback` and `rpe` to the snapshot comparison — the gatekeeper now triggers when feedback text changes or RPE moves off default (5.0). The snapshot format now includes `feedback` and `rpe` fields. If you modify the gatekeeper, keep these trigger conditions intact.

## Pitfalls

- **Never execute `.sh` cron scripts by path (`./script.sh`) on this box**: `/bin/bash` and `/usr/bin/bash` are Debian-compat binaries with a NixOS stub loader — direct exec prints "Could not start dynamically linked executable" (exit 127). The cron scheduler runs `.sh` via `shutil.which("bash")` → `/run/current-system/sw/bin/bash` (the real one), so scheduled runs are unaffected; for manual runs use `bash script.sh` (DRY_RUN=1 for a preview). `reset-food-today.sh`'s `#!/bin/bash` shebang only breaks direct exec, which is why the reset is safe from accidental manual mid-day runs (incident 12 taught that lesson the hard way).
- **`cmd_add` has its own inline push block** (does NOT call `push_totals`) — any new push logic (e.g. the logged-today flag) must be added in BOTH places or the first meal of the day silently skips it. (Known wart — see REVIEW.md.)
- **Never copy masked secrets into patch strings**: tool output redacts `Authorization: Bearer *** → `***`; pasting that into a `patch` writes the literal `***` into the file. Verify with `grep -c 'Bearer \*\*\*'` after editing scripts. (Also: a *real* token masked to `***` in tool output is NOT literal asterisks in the file — `reset-food-today.sh` correctly uses `Bearer $TOKEN`; do not "fix" it.)
- **HA 2026 removed some websocket commands**: `config_entries/flow`, `config_entries/delete`, `template/list` are `unknown_command`. Config entries: use REST (`POST /api/config/config_entries/flow`, `DELETE /api/config/config_entries/entry/<id>`). Helper storage collections still work (`input_boolean/create` etc.).
- **Template-sensor helpers created via flow have empty `options`** — their template text can't be read back via `config_entries/get`; delete + recreate (same name → same entity_id) is the way to change them. Recorder keeps history under the entity_id, so charts survive the swap (brief `unavailable` blip during delete/recreate).
- **Statistics need day rollover**: `statistics: {type: change, period: day}` returns 0/empty for sensors created today. (Note: HA's daily statistics, once populated, ARE correct for the `*_consumed` sensors — server-side day buckets in HA's TZ — but they were polluted for Aug 11–12 by the sensor's birth-day transients, so the dashboard uses `*_today` + `func: max` instead. See the pitfall below.)
- **Dashboard daily macro charts: `*_today` gauges with `group_by.func: max` + per-series ceiling-guard transform** (Aug 2026 incident, corrected 2026-08-15 take 3; offset removed 2026-08-20): the "Calories & macros" chart originally read the 4 `sensor.food_*_today` gauges with `func: last`. A manual `reset-food-today.sh` run before local midnight (port worker, 23:08 local 2026-08-14; box ran UTC then, user is UTC-3) wrote a trailing 0 into Aug 14's history, so that day's bars rendered 0 — "wiped". An attempted `func: diff` on `sensor.food_*_consumed` was WORSE and was reverted: `*_consumed` records ONLY on state change (meals) — there is NO midnight snapshot in the recorder, so each 1d bucket's first point is the first MEAL, and diff = last−first loses the first meal of every day (Aug 14 rendered 7866−6824 = 1042 instead of 1702; every day looked low). Do NOT chase the phantom "00:00 rollover snapshot": a REST `history/period` query starting at midnight returns the last prior state stamped at the query start (initial-state artifact) — it is not a real recorder point (a wide fetch proves it). Correct config: `*_today` gauges + `func: max` (intake accumulates within a day, so max == day total; immune to trailing reset-0s) + transform that nulls non-positive values (so an unlogged day's reset-0 bucket renders as missing, not 0). CRITICAL bucket-alignment caveat (corrected 2026-08-15 3rd visit, re-evaluated 2026-08-20): apexcharts 1d buckets anchor to the VIEWER's midnight in the frontend locale — and the card has NO per-card timezone option. The frontend `locale.time_zone` governs: if it equals `"server"` the card buckets by HA's `config.time_zone` (America/Halifax = UTC-3); otherwise it buckets by the BROWSER's TZ. The operator's frontend resolves to UTC (browser-local), so day bars land in UTC buckets. When the box ran UTC, a `+3h` offset was needed to shift data into the correct UTC buckets. Since the box moved to UTC-3 (America/Halifax) on 2026-08-16, the offset is NO LONGER NEEDED for the Today chart and actually CAUSES a bug: it shifts the previous day's final data point (e.g. 22:33 UTC) into today's bucket (+3h = 01:33 next day), making yesterday's calories appear on today's bar. **Today "Calories & macros" chart: NO offset.** The Trends "Macronutrients" chart still uses `offset: "+3h"` because it reads from HA's daily statistics (server-side bucketed, different data shape). Transform ceiling guards (`calories < 3000`, `protein/fat < 200`, `carbs < 400`) null any birth-day transients; no real logged value is ever near the ceilings.
- **Lovelace saves need `url_path`**: `lovelace/config/save` without `url_path` silently writes to the DEFAULT dashboard — I clobbered Home once this way. Always pass `url_path="dashboard-fitness"`. Before editing, fetch config and check for `sections` (modern layout) vs `cards`.
- **DRY_RUN must guard ALL writes** (log file + foods.json + helpers). Verify with a hash before/after.
- **HA API via https is Cloudflare-blocked for scripts** (error 1010) — use `http://hearth.lan/` or a browser User-Agent header.
- **input_number helpers have no `state_class`** → never enter long-term statistics. Template sensors with `state_class` are the native way to get permanent records.
- **Helper creation is not a config flow** in current HA: use websocket `input_number/create` (storage collection), not `config_entries/flow`.
- **opencode sandbox auto-rejects any read of `~/.hermes/scripts/` or `~/.hermes/.env`** — the whole run aborts (killed 3 builds). Every spec must carry the SANDBOX RULE verbatim: never read/ls/cat any absolute path outside the build dir; build against the schemas in the spec; deployed copies get replaced at deploy time. Put spec files inside the workspace dir it will edit (sandbox also blocks `/tmp`).
- **opencode may test against real files despite instructions** — always verify with hashes + `food_log.py today` after delegating changes.
- Dashboard views in sections layout: `cards: 0` in the API probe does NOT mean empty — count `sections`.
- **HA todo API `add_item` field is `item`, not `summary`**: `add_item` takes `{"entity_id": "...", "item": "summary text"}`, `remove_item` takes `{"entity_id": "...", "item": "<uid>"}`. Using `summary` or `uid` as the field name returns 400. The `get_items` response uses `summary` and `uid` as field names in the returned items — this is the source of confusion. Verified 2026-08-20.
- **Manual workout processing when gatekeeper misses feedback**: if the user reports a workout and the gatekeeper didn't trigger (e.g. feedback left after cron ran), process manually: (1) `python3 ~/.hermes/scripts/fitness_planner.py log --session "<description>"` to log the workout, (2) clear feedback via `ha_call_service` (set `input_text.workout_feedback` to "" and `input_number.workout_effort_rpe` to 5), (3) update todo.fitness with next session items, (4) update `fitness_daily_snapshot.json` with current state.

## Environment

- HASS_URL/HASS_TOKEN live in `~/.hermes/.env` (HASS_ENV_PATH env override supported)
- Websocket helper scripts: `nix-shell -p python3Packages.websockets --run "python3 script.py"`
- REST calls need `-H "User-Agent: Mozilla/5.0"` for Cloudflare-fronted hosts
