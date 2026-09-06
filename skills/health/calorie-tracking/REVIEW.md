# Calorie Tracking — Port Review Notes

Ported from hermes@hermesagent.lan (2026-08-15) into the hermes-deploy flake
as part of the old-box migration. These notes capture issues found while
reviewing the original implementation, and what the port did about each.

## What was ported (running behavior, not aspirational copies)

The old box kept **two divergent copies** of the fitness scripts:

| Script | `~/.hermes/scripts/` (RUNNING — cron uses this) | `~/workspace/foodlog/` ("source of truth" per skill) |
|---|---|---|
| fitness_planner.py | 402 lines, v1 archivist | 812 lines, **v2 experiment** (routine-prescribing) |
| fitness_snapshot.py | 492 lines (current) | 495 lines (cosmetic `mode_txt` diffs only) |

The skill claimed the foodlog/ copy is canonical ("keep copies in sync"), but
it had drifted badly. The foodlog/ `fitness_planner.py` is a **rolled-back v2**
that prescribes routines, rotates "standard vs gentle sessions", and writes
`fitness_targets.json` — all of which directly contradict the current passive
"pure archivist" directive (Aug 2026). The foodlog/ `fitness_routine.json`
(the v2 config the skill says was deleted) also still exists there.

**Port decision:** the RUNNING copies (scripts/) were ported. The foodlog/
v2 copies were left on the old box — they should be deleted there. See
Residual risks below.

## Findings

1. **Source-of-truth drift (HIGH)** — see table above. Two copies, divergent
   semantics, and the skill's pointer to the "source of truth" pointed at the
   wrong (rolled-back) one. The port collapses this: canonical scripts now
   live in one place (this flake, `scripts/`), runtime copies are installed by
   `install.sh`.

2. **fitness_targets.json is stale and contradicts the skill (HIGH)** — the
   skill says the planner NEVER writes targets, that HA
   (`input_number.target_*`) is the source of truth, and that this file's bulk
   2700/145 values caused wrong suggestions after the user moved to
   maintenance. But the file on the old box says `"updated_by": "planner"`
   (written by the v2 experiment or the coach, not the running planner), and
   `fitness_snapshot.py` reads it as "configured targets" — so every weekly
   snapshot emitted stale bulk targets, conflicting with the skill's own
   directive. **Port decision:** the file is archived to `data/` but NOT
   installed on the new box; the snapshot now derives targets from data
   (weight/duration + TDEE) exactly as the skill intends. The weekly cron
   prompt was also rewritten to read/write targets in HA, not this file.

3. **Weekly cron prompt disagreed with the skill (MEDIUM)** — the old prompt
   told the reviewer to write targets to `fitness_targets.json` and (in an
   older skill snapshot) to act as a coach recommending training. The current
   skill is a passive data reviewer whose targets live in HA. The ported
   prompt follows the current skill (passive, HA targets, no training
   content).

4. **reset-food-today.sh ignored HASS_URL (MEDIUM)** — it read only
   `HASS_TOKEN` and hardcoded `BASE="http://hearth.lan"`. That happens to be
   correct (the env's `HASS_URL` is a Cloudflare-fronted public URL that
   rejects script API calls — error 1010/404), but it was implicit. Port adds
   a `FOOD_RESET_BASE` override and a `DRY_RUN=1` guard. Also: the auth header
   was verified to use `Bearer $TOKEN` (not a hardcoded secret — earlier
   masked-output red herring; see the skill pitfall note).

5. **food_log.py duplicates its push logic (MEDIUM, known wart)** — `cmd_add`
   inlines the today+lifetime push + logged-flag block instead of calling
   `push_totals()`; any future push change must be made in both places (the
   skill documents this pitfall; the code confirms it at `cmd_add` vs
   `push_totals`). Recommend a single `push_totals` call in `cmd_add` as
   future refactor. Kept as-is for faithful behavior.

6. **Non-atomic log rewrites (LOW)** — `edit`/`undo` rewrite the whole
   `food-log.jsonl` in place; a crash mid-write can truncate the log.
   Recommend write-to-tmp + `os.replace()`. Acceptable for single-user use.

7. **Inconsistent corrupt-line handling (LOW)** — `food_log.py` exits fatally
   on a corrupt JSONL line; `fitness_snapshot.py`/`fitness_planner.py` skip
   them. One corrupt line bricks the CLI but not the readers. Recommend
   tolerance in `food_log.py`.

8. **Absolute /home/hermes paths everywhere (LOW)** — all scripts hardcoded
   the old box's home; all supported env overrides except the reset script's
   BASE. Port uses `$HOME`/`~`-derived defaults (`/var/lib/hermes` on this
   box) + keeps the env overrides.

9. **weekly_fitness_collect.py is legacy (LOW)** — not referenced by any cron
   job; uses old `withings_*` entity names (current: `pixel_8_*`). Ported for
   completeness; candidate for deletion on the old box.

10. **No automated tests (LOW)** — only `_test_planner_v2.py`, which tests the
    rolled-back v2. Port adds `scripts/smoke_test.py` (read-only + DRY_RUN
    checks, temp dirs, never touches HA or the real log).

11. **Hardcoded stale fallbacks in snapshot (LOW)** — `FALLBACK_TARGET_KCAL =
    2700`, `PROTEIN_PER_KG = 1.8`, `BULK_SURPLUS = 300` mirror the stale bulk
    config; used only when no body data exists. Left as-is (behavioral
    parity); the weekly reviewer should not quote fallback-derived numbers as
    targets.

12. **Mid-day manual reset zeroed the day's dashboard bars (MEDIUM, post-port
    incident; chart fix corrected again 2026-08-15)** — during port
    verification (2026-08-15 02:08 UTC = 23:08 local user time), the worker
    ran `reset-food-today.sh` by hand because the log had no new-day entries;
    the box runs UTC so this fired ~1h before the user's local midnight. The
    `*_today` helpers were zeroed and the flag turned off, writing a trailing
    `0.0` into Aug 14's recorder history. The dashboard "Calories & macros"
    chart reads those sensors with `group_by.func: last`, so Aug 14's bars
    rendered as 0 (user: "we wiped yesterday's macros"). No data was lost —
    JSONL + `*_consumed` sensors were intact. Two wrong fixes followed:
    (a) `func: max` on `*_today` was deployed then reverted on the belief that
    "the day bucket opens with the previous day's carryover" — that belief was
    based on UTC-bucket reasoning (box TZ) and on a phantom "00:00 rollover
    snapshot" that turned out to be a REST initial-state artifact; with the
    user's UTC-3 browser buckets, `func: max` on `*_today` is correct.
    (b) `func: diff` on `sensor.food_*_consumed` (commit cc429a6, gen 33) —
    WORSE: `*_consumed` only records on state change (meals), so a 1d bucket's
    first point is the first MEAL, and diff loses the first meal of every day
    (Aug 14 rendered 7866−6824 = 1042 instead of 1702; "counts seem lower than
    they were"). **Final fix (gen 39, commit ff15415): `*_today` gauges +
    `func: max` + per-series `offset: "+3h"` + ceiling-guard transform** —
    intake accumulates within a day, so max == day total (Aug 12=1970,
    Aug 13=1721, Aug 14=1702 — exact JSONL matches on all 4 macros), immune to
    trailing reset-0s, and the transform nulls the reset 0s so unlogged days
    render as missing rather than a 0 bar. The `offset: "+3h"` is the
    third-visit correction: gen 37's fix (max + plain `x > 0 ? x : null`,
    commit ddde25b) was verified only in UTC-3 buckets, but the operator
    reported the 5786 kcal birth-day transient (sensor born 2026-08-12
    00:59Z; seeding double-count 2893×2 at 01:44:52Z) on WEDNESDAY Aug 12 —
    which only happens with 00:00Z UTC buckets. apexcharts-card has NO
    timezone option; it buckets by the frontend locale's midnight
    (`locale.time_zone === "server"` → HA config TZ America/Halifax; else the
    browser's TZ, which for this operator resolves to UTC). The offset shifts
    each data point 3h earlier so UTC buckets align with the sensors'
    00:05-local reset rhythm; verified by exact pipeline replication under
    BOTH UTC and UTC-3 viewers: Aug 12=1970, Aug 13=1721, Aug 14=1702,
    Aug 15=no bar. Under UTC buckets without the offset, max gives Aug
    12=5786, Aug 13=1970 (carryover), Aug 14=1597 — the carryover objection
    that sank attempt 1 was real, but only in UTC buckets; the offset resolves
    it. The ceiling guards (`calories < 3000`, `protein/fat < 200`, `carbs <
    400`) null the doubled birth-day transients (5786/218/668/242) so the
    sensor's birth day (Aug 11 bucket) renders seeding values
    (~2893/109/339/121) instead of absurd doubles; no real logged value comes
    near the ceilings. Lessons: never run the reset manually against a live
    day; the scheduled cron fires at 00:05 user-local (03:05 UTC), which is
    correct. And for apexcharts 1d buckets, the alignment is the FRONTEND
    LOCALE's midnight, not the box's UTC and not an assumption — verify with
    the operator's report of which calendar day a known transient lands on.

13. **Cron expr was 3h early until 2026-08-15, then re-pinned for the TZ flip (MEDIUM, port bug — t_eea4b2bc; resolved 2026-08-16)**:
    the ported job record kept the old box's `5 0 * * *`, which on the old
    box (TZ America/Halifax) meant 00:05 local — but this box ran UTC, so it
    fired at 00:05 UTC = 21:05 user-local, i.e. 3h BEFORE the user's
    midnight, the exact class of pre-midnight reset that caused incident 12.
    It had never fired yet (created 01:59 UTC, first run would have been
    2026-08-16T00:05 UTC). First corrected to `5 3 * * *` (03:05 UTC = 00:05
    user-local during ADT; note DST: Halifax is UTC−3 in summer, UTC−4 in
    winter, so the expr drifts 1h early after the fall DST switch). **FINAL
    (2026-08-16): the box system TZ was flipped to America/Halifax to match
    HA, and the reset cron re-pinned to `5 0 * * *` = 00:05 LOCAL —
    DST-correct year-round, the October-transition caveat is resolved and no
    expr change will be needed.** Food-log day bucketing (food_log.py keys
    off the system clock) is local automatically once the box TZ is the
    user's TZ; two entries were migrated 2026-08-16 (ts 00:55:54Z banana and
    01:41:41Z cheese: day 2026-08-16 → 2026-08-15). Also cleared the stale
    `prompt` field on the job record (no_agent jobs ignore it, but it made
    the dashboard cron UI show the reset as a prompt-driven job).

## Verification performed (on the new box)

- `smoke_test.py` — all checks pass (see scripts/smoke_test.py).
- `food_log.py foods/status/today` against the seeded data.
- `food_log.py add --food ... --remember` with `DRY_RUN=1` (no HA writes).
- `fitness_snapshot.py` against live HA (read-only) — snapshot builds,
  entities resolve.
- `fitness_planner.py status/next` read-only + `sync` with DRY_RUN.
- `reset-food-today.sh` with `DRY_RUN=1`.
- `nix flake check` / `hermes-deploy` deploy — see task metadata.

## Residual risks / follow-ups

- **Cron delivery targets**: this box has no Discord/Telegram channel wired
  yet (the old box delivered the weekly review + planner changes to a Discord
  channel). Jobs are created with local delivery; once a chat channel is
  connected, update `deliver` on `Fitness Assistant Weekly` /
  `Fitness Planner Daily` (one cronjob update each).
- **Old box cleanup**: delete the stale foodlog/ v2 copies
  (`fitness_planner.py`, `fitness_snapshot.py`, `fitness_routine.json`,
  `_test_planner_v2.py`) on hermes@hermesagent.lan once the migration is
  complete.
- **food_log.py refactor** (single push path) and **atomic log writes** are
  offered as future work; deliberately out of scope here.
- The seeded `food-log.jsonl` history (15 entries, Aug 11–14 2026) was copied
  for continuity of lifetime totals; the live log on this box will diverge
  from the old box's once logging resumes here.
