# Nightly screening runbook (yachtwatch cron, sailboatlistings.com)

End-to-end procedure for the nightly sailboat screening job. Verified 2026-08-09.
Current architecture: script-driven pipeline — cheap Stage-1 screen (`screen_boats.py`)
+ per-survivor evaluation via the scoped gateway api_server (`boats` profile, `/p/boats/`)
— NOT parent-agent + delegate_task, NOT `hermes -z`.

## 0. Scope
Full-time NS liveaboard + remote software work, managed winter slip, secondary bluewater.
Soft budget ~$150k CAD (≈ $110k USD — sailboatlistings prices are USD), may stretch for an
especially good deal; hard ceiling $200k USD. 33-47ft realistic (30ft is minimum gate).
POST FLOOR: weighted score must be >= 6.5/10 — below that the boat is NOT posted, enforced
twice (agent prompt asks for NOT_SUITABLE; the script re-parses `**Weighted score:` from the
report and stays silent below the floor). Two-stage cost-gated screening per SKILL.md.

## 1. Files / entrypoints
- `~/workspace/sailboatpurchase/nightly-sailboat-scan.sh` — the driver script (bash).
- `~/.hermes/scripts/nightly-sailboat-scan.sh` — thin wrapper the no_agent cron invokes
  (Hermes cron `script=` must resolve inside `~/.hermes/scripts/`; symlinks rejected, use a
  wrapper that `exec`s the real path).
- Destination: discord FORUM channel 1536043929445859478 (one thread per boat; pass as
  `$1` to the script to override). Old flat channel 1535981274865467503 retired 2026-08-09.
- `~/workspace/sailboatpurchase/daily_new.py` — index scrape + seen.json diff. `--mark <ids>` marks
  seen; plain run prints JSON `{run_date,total_on_page,new_count,new:[...]}`. `--days N`
  = lookback window: pages through the index (newest-first, each card's `Added DD-MMM-YYYY`)
  until listings fall outside the window; returns only listings within it. Omit = front
  page only (the nightly default).
- `~/workspace/sailboatpurchase/nightly_report.py` — fetches each detail page, writes
  `today_new.json` (compact per-boat payload: id,title,url,price,location,raw_fields,
  spec_text, description[:1200], images[]). Accepts `--days N` passed through to the detector.
- `~/workspace/sailboatpurchase/forum_post.sh` — posting primitive. Reads report from stdin
  (text + `MEDIA:/abs/path.jpg` lines), creates ONE NAMED thread in the forum channel
  (correct name + report + listing link + all image attachments as the initial message in
  a single multipart REST call to `POST /channels/{forum_id}/threads`). Usage:
  `forum_post.sh <forum_channel_id> <thread_name> [<listing_url>] < report.txt`.
  The URL is appended as a `🔗 Listing:` line at the end of the message. Prints the
  thread id on success. Requires `DISCORD_BOT_TOKEN` in `~/.hermes/.env`.
- `~/workspace/sailboatpurchase/seen.json` — state. Seed all current-page IDs once; future runs
  only see IDs above that.

## 2. Driver script logic (the part that matters)
+ `MAX_BOATS` (default **10**): cap on per-boat gateway API evaluations per night.
    Each survivor is POSTed to `http://127.0.0.1:8642/p/boats/v1/chat/completions` —
    the scoped `boats` profile (own config + env at `~/.hermes/profiles/boats/`, lean
    toolset file/skills/terminal/vision, opencode-go model, vision via openrouter
    gpt-4o) — key `API_SERVER_KEY` in `~/.hermes/.env`, model `hermes-agent`,
    `max_tokens: 2000`, curl timeout 900s). The agent loop runs inside the gateway
    process — no process boot, no RAM spike. Never use `hermes -z` without a very
    good reason (a fresh
    ~1.1GB process per boat; 53 parallel spawns OOM-killed the gateway on 2026-08-09 —
    28 empty out files, nothing posted/marked). Failed evals (curl error, non-2xx,
    empty reply) are logged loudly and NOT marked seen — they retry tomorrow and the
    cron agent files a fixups ticket.
+ Floor plans: the per-boat prompt REQUIRES the evaluator to include a floor plan /
    interior layout image in the `IMAGES:` output whenever the listing has one (buyer
    requirement — prioritized over a generic exterior shot, still within the 2-4
    image cap).
+ Deterministic dimension sanity (added 2026-08-11, bug 113228): `nightly_report.py`
    flags physically impossible beam/draft vs length (sellers enter inches with a
    foot mark, e.g. beam 108' on a 35' boat) as a `data_issue` field on the payload.
    The driver skips eval for these with a deterministic pre-check (log line
    "DATA_ISSUE (deterministic pre-check)") — never posted, still marked seen, same
    semantics as the LLM DATA_ISSUE path. Covers the case where the LLM misses the
    contradiction.
+ Deterministic missing-specs check (added 2026-08-14, bug 113248): `nightly_report.py`
    also flags listings whose price/beam/draft are absent, empty, or junk (lone
    apostrophe `'` or bare `$` cells — sellers leaving fields blank) as `data_issue`
    ("price, draft missing from listing"). Junk cells are dropped from `raw_fields`/
    `spec_text` entirely (`_is_junk`). Same driver pre-check semantics as dimension
    sanity: no eval, no post, marked seen, fixups ticket filed. Fixes LLM variance —
    previously only a DATA_ISSUE'd boat got a ticket while another listing with the
    same gap could sail through evaluation.
+ Stage-1 gate on tickets (added 2026-08-14, ticket 113246): `nightly_report.py`
    sets `stage1_reject` on the payload when a data-issue boat deterministically
    fails a Stage-1 dealbreaker — length < 30ft or price > $200k, when either is
    determinable (`stage1_reject()` strips non-[0-9.] from price: '$435,000' →
    435000 — never take the first regex number). The driver then logs, marks seen,
    and still reports the boat in cron stdout, but files NO fixup ticket: the data
    gap cannot change the outcome. Missing/unparseable length or price → no gate
    (conservative, ticket kept). Genuine candidates with missing specs (e.g. a 34'
    Gemini cat with draft missing) still ticket.
2. `daily_new.py` → list of new boats. Zero → exit silently (no channel post).
   Lookback mode: a `.scan-env` file in the sailboatpurchase dir can set `LOOKBACK_DAYS=10`
   (+ `MAX_BOATS=60`, optional `DRY_RUN=1`) for a one-off catch-up run — create it, fire
   the cron job, then DELETE the file so nightly reverts to front-page-only. The script
   sources it before the config block.
2. Build per-boat payloads (NDJSON: ONE boat per line — a `json.dump` of the array on one
   line breaks `while read`; use `f.write(json.dumps(b)+"\n")` per boat).
3. **Stage-1 prefilter (added 2026-08-11):** `screen_boats.py --in BOATS_TMP --out
   survivors.ndjson --rejected rejected.ids` — one cheap parallel OpenRouter text call
   per boat (~10 concurrent, seconds, pennies) enforcing the dealbreaker list. Only
   SURVIVORS spawn the heavy agent; rejects are marked seen without booting anything.
   Fail-open: per-boat API errors stay in the survivor set (screen_boats.py `errors`
   list), and a total screen failure (`|| SCREEN_FAILED=1`) falls back to treating all
   of BOATS_TMP as survivors with a stderr warning. Dry-run still runs the screen and
   logs `screened N → M survivors, K rejected`.
4. For each survivor, spawn in parallel: `hermes --skills liveaboard-sailboat-rubric,yacht-scraper
   -z "<self-contained prompt>"` under the clean env (`env -i HOME=... PATH=... HERMES_HOME=...`)
   — a process inheriting HERMES_CRON_SESSION / HERMES_SESSION_* self-skips or misbehaves.
   Prompt embeds the boat JSON inline; agent must NOT re-fetch the listing. Cap
   concurrency/limit (MAX_BOATS=10).
5. Agent does Stage 2 (it already passed Stage 1); if it self-rejects replies exactly
   `NOT_SUITABLE` (script stays silent). Otherwise downloads 3-5 images
   (`curl -A "Mozilla/5.0"` into /tmp/yachtwatch/<id>/,
   verify `file` says JPEG), vision-analyzes, outputs the STRICT TEMPLATE
   report (SKILL.md Output format: `**Weighted score: X.X/10` FIRST line →
   data line → URL → fixed-label bullets → `IMAGES:` absolute paths).
   Thread title = `boat title · X.X/10` (score parsed from the report;
   title truncated to Discord's 100-char cap).
6. Script `wait`s all PIDs (do NOT poll for file non-emptiness — the obsoleted
   `[ -f ] || continue` polling raced the backgrounded spawn and skipped reports).
7. Convert `IMAGES:` block → `MEDIA:` lines, pipe to `forum_post.sh` with the boat
   title and listing URL (both saved to `/tmp/yachtwatch-title-$i.txt` and
   `/tmp/yachtwatch-url-$i.txt` at spawn time) → creates one NAMED thread per boat in
   the forum, with the listing link appended. Example (inside script):
   `echo "$REPORT_CT" | bash "$ROOT/forum_post.sh" "$CHANNEL" "$THREAD_NAME" "$BOAT_URL"`.
   NOTE: `hermes send --to <forum_id>` creates auto-named "New Post" threads — useless
   in a forum list. Named threads require the REST `POST /channels/{forum}/threads`
   call, which is exactly what forum_post.sh wraps.
8. Mark processed IDs seen (`daily_new.py --mark <ids>`) — heavy-agent boats (passed AND
   rejected) plus screen-rejected boats, or they re-report tomorrow. Only boats that
   actually got evaluated are marked — overflow boats stay unmarked so a future run
   still evaluates them.

## 3. forum_post.sh gotchas (learned the hard way, 2026-08-09)
- `curl -F "payload_json=@file"` uploads the JSON as a FILE attachment → Discord rejects
  with "name and message are required". Must use `-F "payload_json=<file"` (`<` = read
  file content as a plain form field; `@` = file upload).
- The `while IFS= read -r f; do ... done < files.lst` loop silently SKIPS the last line
  when files.lst has no trailing newline (read returns EOF-status on the final line → body
  never runs). Write the list with a trailing `\n` (or use `mapfile -t`).
- Discord caps thread names at 100 chars — truncate before sending.
- A `printf '...' | bash script.sh` pipeline trips the Hermes security scanner (pipe to
  interpreter = HIGH). From scripts/tests use stdin redirect instead:
  `bash script.sh args < body.txt`.
- Forum messages are NOT visible via `GET /channels/{forum}/messages` (empty array) —
  they live inside threads. Verify via `GET /channels/{thread_id}/messages`.
- Threads created by the bot can be deleted by the bot (`DELETE /channels/{thread_id}`),
  but threads created via `hermes send` are owned by the gateway token → 403 for the bot
  token. Clean up test threads with the bot token to avoid orphans.

## 4. Price recovery (FIXED at the index level — no web_extract needed anymore)
`daily_new.py` now extracts Asking/Length/Year/Location by regexing each card's HTML
(`table.html`) for `<label>:</span></TD><TD...>(?:<span...>)?VALUE`. The naive
`td.next` approach does NOT work on this site: label cells contain text nodes + spans, so
`td.next` returns an empty text node, not the value cell. Always regex the card block.
Empty price in a payload means the value cell format differed — flag it, don't hand-roll.

## 5. Image download (curl, NOT yacht-scrape detail)
`yacht-scrape detail` image downloads are frequently corrupt (files start `ef bf bd` U+FFFD;
`file` says "data"; vision refuses). Direct curl from the images[] URLs (full-size `m/` path)
works:
```bash
mkdir -p /tmp/yachtwatch/<id>/full
curl -sL -A "Mozilla/5.0" -o /tmp/yachtwatch/<id>/full/<name>.jpg "https://www.sailboatlistings.com/sailimg/m/<id>/<photo>.jpg"
```
Verify `file` says "JPEG image data". Listing may have ONE photo (e.g. Island Packet 45) —
state the limitation instead of fabricating interior descriptions.

## 6. What was REMOVED (do not resurrect)
- Parent-agent cron job that `delegate_task`s per boat: the cron session posts its final
  response ("awaiting sub-agent ...") before async sub-agent results re-enter, so the channel
  gets a stub and the real eval arrives minutes later — or never.
- `web_extract` for every listing's price: slow and unnecessary since the index regex fix.

## 7. Known script bugs FIXED 2026-08-09 (do not reintroduce)
- **seen-marking on NDJSON**: step 5 previously did `json.load(open(BOATS_TMP))` on a
  NEWLINE-DELIMITED file → `JSONDecodeError: Extra data` → swallowed by `except: pass` →
  IDS empty → `--mark` never ran → boats re-reported next night (duplicate posts). Must read
  line-by-line: `for line in f: json.loads(line)`.
- **MEDIA: conversion**: the convert snippet tested `not ln.startswith(" ")` to end the
  IMAGES block, but image paths start with `/` (not a space), so the FIRST path line flipped
  `in_imgs=False` and ALL paths fell through as plain text (zero attachments, dead local
  paths in the Discord post). Block-end test must be "line that is NOT a path"
  (`s.startswith("/")`), not "not indented".
- **Truncated agent output**: an agent's stdout can be cut mid-sentence (no score line, no
  IMAGES block). Check `wc -c` on the out file; if it lacks `**Weighted score:` or ends
  mid-word, re-run that boat's agent before posting — a truncated report posts as-is and
  lands in the channel broken.