---
name: liveaboard-sailboat-rubric
description: "Use when evaluating sailboats for full-time liveaboard in Nova Scotia (NS) with remote software work, managed winter slip, and secondary bluewater/Atlantic capability. Two-stage cost-gated screening."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [sailboat, liveaboard, rubric, nova-scotia, evaluation]
    related_skills: [yacht-scraper]
---

# Liveaboard Sailboat Rubric — NS

Goal: full-time liveaboard + remote software work, managed winter slip (shore power + de-icing), secondary bluewater/Atlantic capability.

## Process (2-stage, cost-gated)

### Stage 1 — screen from text/spec data only, NO images
Check dealbreakers + minimum thresholds:
- Insurability blockers (steel/aluminum corrosion, wood hull)
- Hull material/condition red flags in listing text
- Price/size out of range (soft budget ~$150k CAD ≈ $110k USD — listings are USD; hard
  ceiling $200k USD; realistically 33-47ft for full-time. Above soft budget is OK ONLY
  for an especially good deal — flag the stretch in Cost & ownership)
- No viable heating path (reverse cycle AC alone is weak below 40°F; diesel heater preferred). Heat pump/AC is a BONUS (Stage 2), never a Stage-1 requirement
- Location practicality (far-flung = heavy delivery cost, note but don't auto-reject)

Reject at Stage 1 if ANY dealbreaker hits. Do NOT fetch or reason over images at this stage.

### Stage 2 — full weighted scoring (only for boats passing Stage 1)

Categories & weights:
| Category | Weight |
|---|---|
| Winter livability | 15% |
| Power & solar | 15% |
| Hull/structure/rigging | 15% |
| Maintenance burden | 10% |
| Liveaboard comfort/layout | 10% |
| Office/workshop suitability | 10% |
| Offshore/bluewater capability | 10% |
| Cost & ownership | 15% |

Score each 0-10 with 1 line of rationale. Weighted total out of 10.

## Category guidance

**Winter livability (15%)** — Heating adequacy/upgrade path. Insulation, condensation control. Reverse cycle loses efficiency <40°F; note diesel-heater retrofit cost.

**Power & solar (15%)** — Battery bank vs liveaboard draw. Solar expansion space + wiring/controller headroom. Alternator capacity. Shore power vs system sizing.

**Hull/structure/rigging (15%)** — Material tradeoffs (fiberglass easiest; steel/aluminum corrosion/insurance cost; wood = pass unless project wanted). Survey flags: blisters, deck core, keel bolts. Rigging age vs 10-15yr interval (age alone NOT a dealbreaker on solid-value boats — it's replaceable, note budget). Keel type (full=forgiving, fin=performance).

**Maintenance burden (10%)** — Engine age/hours/access. Parts/mechanic availability (Maritimes). Known chronic issues for make/model.

**Liveaboard comfort/layout (10%)** — Headroom, head/galley separation, storage, tankage vs full-time use, natural light. Existing marine heat pump (reverse-cycle AC) = BONUS: summer cooling + shoulder-season heat; retrofit if absent ~$3-6k (needs seawater thru-hull + seacock, 120V/230V AC, ~24"x14"x13" locker under settee/berth, ~88 lb). An existing AC thru-hull makes a retrofit a cheap unit swap.

**Office/workshop suitability (10%)** — Usable desk space (nav station/settee) away from traffic. Power outlets at desk. 3D printer space + ventilation. Workbench potential for electronics/tool repair.

**Offshore/bluewater capability (10%)** — Displacement/hull form for open-ocean motion. Rudder/keel offshore reliability (skeg/full vs spade/fin). Cockpit size (small/protected=safer offshore, cuts against liveaboard comfort — flag this tension, don't average it out). Tankage for multi-week passage. Rig redundancy/reefing short-handed. Sea berths.

**Cost & ownership (15%)** — Price vs condition/refit cost. Insurability flags (material/age/liveaboard status) — potential independent dealbreaker. Marina cost + confirmed liveaboard/winter policy.

## Stage 2 images (promising boats only)
Pull 3-5 representative images, prioritizing interior: main salon/living area, the proposed office/workshop spot, galley, one exterior/deck shot for rig/cockpit context. Skip images for boats rejected at Stage 1. Use the vision tool on downloaded images.

## Output format (STRICT TEMPLATE — fixed field order; every passing post uses EXACTLY this shape)
Discord-friendly, ONE boat per message. Passers follow the template below verbatim — nothing extra, no free-form paragraphs.

1. **Score line (ALWAYS FIRST):** `**Weighted score: X.X/10**` — own line, exact format (the script backstop re-parses this string AND builds the thread title from it: `boat title · X.X/10`). Passing boats only — below 6.5/10 is a reject: reply NOT_SUITABLE, low-scoring boats are never posted.
2. **Data line:** `**$price** — **year Make Model** — **XX ft** — **NOTABLE** | location`
   - Order is fixed: price → year/Make/Model → length in ft → notable features → location.
   - NOTABLE = short comma-joined list of features that STAND OUT for this buyer: multihull (always listed), big work room/workshop, heat pump/AC, exceptional galley, centre cockpit, boom furling, freshwater boat, full refit, etc. Any feature that would make the boat notably better or weirder than the average listing. Not filler: only genuine standouts, 1-4 items.
   - Nothing notable → omit the slot entirely: `**$price** — **year Make Model** — **XX ft** | location`.
3. **URL on the next line** (bare link, no wrapping text).
4. **3-5 bullets, fixed labels, this order** (skip a label only when there is genuinely nothing to say; never reorder, never rename):
   - **Hull:** material + keel/rudder (e.g. solid fiberglass, full keel). Multihulls: beam + living-space note.
   - **Power:** solar W, battery bank, inverter — off-grid capability in one line.
   - **Heating/AC:** heat source (diesel heater/stove) + AC/heat pump present or retrofit path.
   - **Layout:** cabins/heads, desk/office spots, notable comfort wins or limits.
   - **Condition:** refit date, rigging/engine age, red flags or value notes.
   - **Cost:** only when exceptional (well under budget or clearly overpriced).
5. Rejected boats: ONE FLAT LINE each — `**year Make Model** — not suitable`. NO reasons, NO rejection essays, no images. Binary output only: template report for passers, flat line for rejects.
6. Append 2-4 images via MEDIA:/tmp/yachtwatch/<id>/<file>.jpg lines after the last bullet (interior-first: salon, galley, office/nav-station spot, one exterior).
7. No markdown tables (Discord doesn't render them). Empty day → reply "No new sailboat listings today." and stop.

Full nightly cron runbook (prices, image download, seen-marking): references/nightly-pipeline.md

## Execution architecture (IMPORTANT — verified 2026-08-09)

Do NOT run the nightly scan as one big parent agent, and do NOT dispatch `delegate_task`
sub-agents from a cron parent. The parent posts its final response before the async
sub-agent results land ("awaiting sub-agent" race).

The proven pattern: a plain bash script (`~/workspace/sailboatpurchase/nightly-sailboat-scan.sh`,
wrapped at `~/.hermes/scripts/nightly-sailboat-scan.sh` for the cron) does the fetching,
then runs `screen_boats.py` — a CHEAP parallel opencode-go text-only Stage-1 screen
(dealbreakers only, seconds, pennies) — and POSTs the per-boat prompt to the gateway
**api_server on the scoped `boats` profile** (`http://127.0.0.1:8642/p/boats/v1/chat/completions`,
`API_SERVER_KEY` from `~/.hermes/.env`, model `hermes-agent`) for each SURVIVOR (serial, capped by
MAX_BOATS). The agent loop runs INSIDE the already-running gateway process — no new
process per boat (`hermes -z` is forbidden without a very good reason: ~1.1GB RAM
each).
Screen rejects are marked seen without ever booting an agent. Fail-open both ways:
screen errors keep a boat in the survivor set; a screen-call failure falls back to
treating everything as survivors. Each per-boat agent loads THIS skill, screens/scorers
its single boat, and either replies exactly `NOT_SUITABLE` (script stays silent) or
outputs a compact paragraph + `IMAGES:` paths. The script converts `IMAGES:` → `MEDIA:`
and pipes to `forum_post.sh` (one named thread per boat — needs clean env, see
send-message skill).

Per-boat agent prompt must include the full boat JSON (id/title/url/price/location/
spec_text/description/images[]) inline — the agent must NOT re-fetch listings. Prompt it
to download images via `curl -A "Mozilla/5.0"` and verify with `file` (JPEG) before
vision. Keep the parent payload tiny: price at the index level, description truncated.

## Escalation (fixups, not improvisation)

When the pipeline misbehaves — scraper parsing regressions, Discord API failures,
image pipeline breakage, screen/agent errors, repeated zero-new-listings streaks —
DO NOT improvise a route around the bug and DO NOT change screening behaviour to
compensate. File a ticket with the fixups MCP: `mcp__fixups__file_ticket` (bug:
"this doesn't work as expected") or `mcp__fixups__request_fixup` (capability gap:
"no skill/tool exists for this workflow"). The cron job prompt enforces this; the
scripts and this skill are the source of truth, fixed by a human/agent, not
patched around at runtime.

## Common rule notes
- No forced minimum of boats per batch. Zero viable boats is a valid answer.
- Don't invent rules to fill a batch — evaluate what's actually there.
- Rigging age alone is NOT a dealbreaker when the boat has solid value (it's replaceable).
- Boat types: monohull vs catamaran (cat = more living space, less offshore seakeeping), fin/spade (performance, less forgiving) vs full/skeg (offshore reliability), B&R rig (coastal, no backstay).
- NS context: 8ft+ draft limits some harbours; steel suffers freeze/thaw hull issues in ice; Lunenburg etc. are boat-friendly.