---
name: web-scraping
description: "Build Python CLI scrapers for websites — selectolax for parsing, playwright for JS-heavy/Cloudflare sites, --from-html fallback for blocked sources."
version: 1.0.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [scraping, selectolax, playwright, python, web]
    related_skills: [dogfood]
---

# Web Scraping

Build CLI scrapers for target websites. This user's preferred toolchain: **selectolax** (fast CSS selector-based HTML parser) + **playwright** (headless Chromium for JS-heavy sites).

## Preferred toolchain

| Layer | Tool | Why |
|-------|------|-----|
| HTML parsing | **selectolax** | Fast, CSS-selector-based. No dependency on lxml. |
| Browser automation | **playwright** | Headless Chromium for sites that render JS. Install via `uv pip install playwright && uv run python -m playwright install chromium`. |

Avoid beautifulsoup unless selectolax genuinely can't handle the page structure.

## Standard CLI scraper structure

```
scripts/<name>_scraper.py    # executable Python file
```

Pattern: single-file Python script, argparse CLI, `from selectolax.parser import HTMLParser`.

### Import pattern

```python
try:
    from selectolax.parser import HTMLParser
    HAS_SEL = True
except ImportError:
    HAS_SEL = False
    eprint("pip install selectolax")

HAS_PW = False
try:
    from playwright.sync_api import sync_playwright
    HAS_PW = True
except ImportError:
    pass
```

### Helper functions

```python
# Readable element access
def text_or_empty(node): return node.text(strip=True) if node else ""
def attr_or_empty(node, attr): return node.attributes.get(attr, "") if node else ""
```

## Handling Cloudflare-protected sites

Many sites (YachtWorld, BoatTrader, etc.) use Cloudflare challenge pages that block headless browsers, even with stealth patches.

**Strategy stack** (try in order):

1. **Playwright headless** with stealth patches
   ```python
   page.add_init_script("""
       Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
       Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
   """)
   page.goto(url, wait_until="networkidle", timeout=60000)
   ```

2. **Headed browser via Xvfb + VNC** — for sites where you need to solve a captcha manually. Run a virtual display with x11vnc exposed through websockify + noVNC so the user can interact from their browser:
   ```bash
   # Start the browser box (Xvfb :99 + x11vnc :5900 + websockify :6080)
   ~/scripts/hermes-browser start
   
   # The user opens http://hearth.lan:6080/vnc.html in their browser
   # Now run the scraper with --headed:
   yacht-scrape search --headed <url>
   ```
   The headed browser opens on Xvfb :99. The user sees it via VNC, solves the captcha, Playwright captures the result and returns.

3. **`--from-html` fallback** — the most reliable method. Accept a saved HTML file:
   ```
   scraper search --from-html ~/Downloads/page.html
   ```
   User saves the page in their real browser (which passes Cloudflare), dumps the HTML, feeds it to the tool.

4. **Alternative source** — scrape a different site that has the same category of data but no bot protection (e.g. sailboatlistings.com instead of YachtWorld).

### Browser box setup (hermes-browser)

The headed browser box lives at `~/scripts/hermes-browser`. It starts three daemons:

| Service | Port | Purpose |
|---------|------|---------|
| Xvfb :99 | — | Virtual display (1920×1080) |
| x11vnc | 5900 (localhost) | VNC server capturing the display |
| websockify | 6080 (0.0.0.0) | WebSocket proxy + serves noVNC web client |

User accesses `http://<lan-ip>:6080/vnc.html` in their browser to see and interact with the headed Chromium.

**Components:**
- `xvfb` (X virtual framebuffer) — ships with most Linux distros
- `x11vnc` — VNC server for X displays; install via `nix-env -iA nixpkgs.x11vnc`
- `websockify` — WebSocket-to-TCP proxy; install via `pip install websockify`
- `noVNC` — browser-based VNC client; download from GitHub releases to `~/scripts/noVNC/`

## __NEXT_DATA__ parsing

React/Next.js sites embed SSR data in `<script id="__NEXT_DATA__">`:

```python
for script in parser.css("script#__NEXT_DATA__"):
    raw = script.text()
    if not raw: continue
    try:
        data = json.loads(raw)
        props = data.get("props", {}).get("pageProps", {})
        items = props.get("listings") or props.get("boats") or []
    except json.JSONDecodeError:
        pass
```

## JSON-LD fallback

Second source of structured data:

```python
for script in parser.css('script[type="application/ld+json"]'):
    data = json.loads(script.text())
    items = data.get("itemListElement", [])
```

## Pitfalls

- **Playwright headless is detected by aggressive Cloudflare.** Don't waste time trying every stealth trick — just add `--from-html` as the documented fallback path.
- **selectolax nodes have no `.find()` / `.find_all()`.** Use `.css()` and `.css_first()` instead.
- **selectolax `.next` walks sibling nodes, not text.** A `<td>`'s `.next` may be a text node, not the next `<td>`. Loop with `while nxt and nxt.tag not in ("td", "TD"): nxt = nxt.next`.
- **Images on foreign CDNs may also be blocked.** Check with `curl -I` first; if blocked, fall back to the --from-html source.
- **Always save results to JSON.** The user inspects data programmatically.
- **VNC access may be blocked by host firewall.** On Proxmox LXC containers, the host can firewall ports 5900/6080 even when the container has no local firewall. If the user can't reach the noVNC URL, try a different port (e.g. 8080) or fall back to `--from-html`.
- **sailboatlistings.com is a reliable alternative to YachtWorld** for sailboat data. No Cloudflare, simple HTML, easy to parse. Use `browse` and `detail` subcommands.

## Linked files in this skill

| Path | Description |
|------|-------------|
| `references/cloudflare-strategies.md` | Cloudflare bypass approaches ranked by reliability. Covers --from-html, headed browser via VNC, headless stealth, Google snippet mining, and alternative sources. |

## Scripts directory (canonical location: `~/workspace/sailboatpurchase/`)

The reference implementation scripts live at `~/workspace/sailboatpurchase/` (not inside the skill dir), created and maintained during this session:

| Script | Purpose |
|--------|---------|
| `~/workspace/sailboatpurchase/yacht_scraper.py` | Sailboat listing scraper — selectolax + Playwright. `yacht-scrape search <url>`, `browse`, `detail`, `--headed`, `--from-html` subcommands. |
| `~/workspace/sailboatpurchase/yacht-scrape` | Bash wrapper running the Python script in the Hermes venv. |
| `~/scripts/hermes-browser` | Browser box launcher — Xvfb :99 + x11vnc :5900 + websockify :6080 + noVNC for headed Playwright access via `http://<lan-ip>:6080/vnc.html`. |
| `~/scripts/noVNC/` | noVNC web client (downloaded from GitHub). Serves the browser's display over WebSocket. |