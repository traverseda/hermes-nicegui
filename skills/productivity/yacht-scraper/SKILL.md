---
name: yacht-scraper
description: "Use when scraping sailboat listings from YachtWorld or sailboatlistings.com. Two sources: sailboatlistings.com (always works) and YachtWorld (Cloudflare-blocked, needs --from-html)."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [scraping, yachts, sailboats, listings, yachtworld]
    related_skills: [liveaboard-sailboat-rubric, send-message]
---

# Yacht Scraper

Simple CLI tool for scraping sailboat listings. Two backends:

- **sailboatlistings.com** — always accessible, used by default
- **YachtWorld** — behind Cloudflare, needs saved HTML

## Commands

```
yacht-scrape browse --limit N          # newest listings (sailboatlistings.com)
yacht-scrape detail <url> [--out dir]  # listing detail + images
yacht-scrape search <url>              # YachtWorld via Playwright (likely blocked)
yacht-scrape search --from-html file   # YachtWorld from saved page
yacht-scrape listing <url> [--headed]  # YachtWorld listing detail
```

## Quick start

```bash
# Browse latest sailboats
~/workspace/sailboatpurchase/yacht-scrape browse --limit 20

# View details and images
~/workspace/sailboatpurchase/yacht-scrape detail https://www.sailboatlistings.com/view/113200

# YachtWorld (if you save the page HTML from your browser)
~/workspace/sailboatpurchase/yacht-scrape search --from-html ~/Downloads/yachtworld.html
```

## Files

- `~/workspace/sailboatpurchase/yacht_scraper.py` — main tool
- `~/workspace/sailboatpurchase/yacht-scrape` — shell wrapper (uses Hermes venv)
- `~/scripts/hermes-browser` — headed browser box (Xvfb + x11vnc + noVNC)

## Dependencies

```bash
uv pip install selectolax playwright  # in Hermes venv
uv run python -m playwright install chromium
```

## Common Pitfalls

1. YachtWorld uses Cloudflare — headless Chromium gets blocked even with stealth patches. Headed mode (--headed) opens the browser on Xvfb :99 but the VNC port may be firewalled.
2. The browser box runs via Xvfb at ~/scripts/hermes-browser. It needs VNC access on port 6080.
3. sailboatlistings.com is simple HTML — no JS, no Cloudflare, always works.
4. `yacht-scrape detail` image downloads for sailboatlistings are frequently CORRUPT (files start with bytes `ef bf bd` (U+FFFD), `file` reports "data", vision_analyze rejects them as "not real image files"). Don't debug the scraper — just re-download full-size images directly with curl:
   `curl -sL -A "Mozilla/5.0" -o out.jpg "https://www.sailboatlistings.com/sailimg/m/<id>/<filename>.jpg"`
   The `m/` (medium/full-size) path serves valid 800x600 JPEGs; the nightly_report JSON `images[]` array already contains these exact URLs. Verify with `file out.jpg` → must say "JPEG image data".
5. `yacht-scrape detail` often prints "Price: N/A" and writes sbl_listing.json to the CWD (overwrites per run — NOT the --out dir). When price comes back empty in the payload or detail, run web_extract on https://www.sailboatlistings.com/view/<id> — the rendered page shows the parsed price under Quick Specs (plus the full equipment list the payload truncates).
6. sailboatlistings.com index card parsing — `td.next` is a TRAP. Label cells (Asking/Length/Year/Location) contain text + spans, so `td.next` returns an empty text node, never the value cell. Regex the card block instead: `re.search(rf"{label}:\s*</span>\s*</TD>\s*<TD[^>]*>\s*(?:<span[^>]*>\s*)?([^<]+)", card_html, re.I|re.S)`. Without this, `price` silently comes back empty.
7. The index page contains ~380 `/view/` links (pagination + related listings) but only ~25-68 real cards. Filter by the same card selector as the detail parser: `table[width='728']` + `a.sailheader`. A naive regex over all `/view/` links over-reports and breaks the seen.json diff.
8. Spec grids on detail pages are TWO ROWS: row 0 = labels (Year|Length|Beam|Draft|Location|Price), row 1 = values. Zip `rows[0].css("td")` against `rows[1].css("td")`; don't walk siblings. Only treat a table as the spec grid if a label in the row matches year/length/beam/draft/location/price/asking.
9. When a bash `while read` loop consumes a JSON payload, write NEWLINE-DELIMITED JSON (one object per line). A single-line `json.dump` of an array makes the loop read the whole array as one "boat" and silently spawns 1 agent instead of N.
10. Role of the tools: `yacht_scraper.py browse` (newest N) and the yachtwatch pipeline (nightly dedupe via seen.json, cheap `screen_boats.py` Stage-1 screen, per-boat gateway api_server eval calls on the scoped `boats` profile `/p/boats/` ONLY for survivors — never `hermes -z`, `forum_post.sh` named threads in #boats). See liveaboard-sailboat-rubric skill's references/nightly-pipeline.md for the full driver. Pipeline issues go to the fixups MCP (`file_ticket` / `request_fixup`) — never silently patched around.

## Nightly pipeline (yachtwatch)
`~/workspace/sailboatpurchase/` + `~/.hermes/scripts/nightly-sailboat-scan.sh` (cron no_agent) run the
screening automatically. Full procedure lives in the liveaboard-sailboat-rubric skill.