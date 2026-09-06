# Cloudflare Bypass Strategies — Ranked by Reliability

## 1. `--from-html` (most reliable)

User saves the page from their REAL browser (which passes Cloudflare automatically) and feeds the HTML to the tool.

```bash
yacht-scrape search --from-html ~/Downloads/yachtworld.html
```

**How the user saves the page:**
1. Open the URL in their regular browser
2. Ctrl+S / Cmd+S → "Web Page, Complete" → save to a file
3. Pass the file path to the scraper

**Why it works:** The user's browser has a real profile, real cookies, real session — Cloudflare trusts it.

## 2. Headed Playwright via Xvfb + VNC (interactive)

The user can interact with a headed Chromium on a virtual display via VNC.

**Setup (done once):**
```bash
# Start the browser box
~/scripts/hermes-browser start
```
This starts Xvfb :99, x11vnc on localhost:5900, and websockify on port 6080 serving noVNC.

**Usage:**
1. Open http://hearth.lan:6080/vnc.html in the user's browser
2. The VNC display shows the Xvfb virtual desktop
3. Run the scraper with `--headed`:
   ```bash
   ~/workspace/sailboatpurchase/yacht-scrape search --headed "https://..."
   ```
4. Playwright opens headed Chromium on display :99
5. The user sees the Cloudflare captcha via VNC, clicks through it
6. Playwright detects the page loaded, captures the HTML, returns it

**Limitation:** The user must actively be at their browser to solve captchas — not suitable for fully automated batch runs.

**LXC/container firewall:** On Proxmox LXC containers, the host may firewall ports even when the container has no iptables/ufw running. If the user can't reach `http://<lan-ip>:6080/vnc.html`, check:
- Proxmox host firewall (`pve-firewall` or datacenter-level rules)
- Whether the port needs to be bound to a different IP or port (try port 8080)
- Alternative: use `--from-html` instead (the user saves the page locally)

### hermes-browser commands

```bash
~/scripts/hermes-browser start       # Start display + VNC + websockify
~/scripts/hermes-browser stop        # Stop everything
~/scripts/hermes-browser status      # Check what's running
~/scripts/hermes-browser url         # Print the noVNC URL
~/scripts/hermes-browser navigate <url>  # Open URL in headed browser (5 min timeout)
~/scripts/hermes-browser screenshot   # Take a screenshot
```

## 3. Headless Playwright with stealth (least reliable)

Works for sites with basic Cloudflare (turnstile, challenge-pass after JS eval) but **fails** on sites using managed challenge (5-second interactive challenge).

```python
page.add_init_script("""
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    window.chrome = {runtime: {}};
""")
```

**Evidence of failure:** The page returns ~5,334 bytes with `<title>Just a moment...</title>`, `<meta name="robots" content="noindex,nofollow">`, and a CSP that only allows `https://challenges.cloudflare.com`. The challenge never completes in headless mode.

## 4. Google snippet mining (data-light, zero-setup)

When the target site is fully blocked but you need quick data, use the `web_search` tool to mine Google's search-result snippets. Many e-commerce/marketplace sites have rich snippets with prices visible in Google results.

**How:**
```python
results = web_search("site:targetsite.com/category $ price range")
```
Google's indexed descriptions often contain prices, years, locations, and other structured data embedded in the snippet text.

**Limits:**
- Only returns what Google indexed (fragments, not complete data)
- No pagination control — Google decides what to show
- Works best for "latest" or "newest" queries where Google shows fresh results

## 5. Alternative source

When Cloudflare is impenetrable, find a different site with the same data category:

| Blocked | Alternative |
|---------|-------------|
| YachtWorld | sailboatlistings.com (no bot protection) |
| BoatTrader | boats.com (same group, same CF) |
| Zillow/Redfin | Realtor.com (less aggressive) |

## What NOT to try (waste of time)

- **playwright-extra / puppeteer-extra stealth plugins** — major sites' CF detection uses TLS fingerprinting + browser feature detection + IP reputation. Stealth plugins only mask `navigator.webdriver`.
- **Rotating user agents** — CF's JS challenge detects the browser engine at the protocol level, not just the UA string.
- **Cloudscraper / requests with cookies** — the challenge requires executing JavaScript; a pure-HTTP client cannot pass a JS challenge.