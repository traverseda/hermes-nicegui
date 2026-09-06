#!/usr/bin/env python3
"""YachtWorld scraper: search listings, read descriptions, view images.

USAGE:
  yacht-scrape search <url> [--headed]     -- search from a YachtWorld URL
  yacht-scrape search --from-html <file>   -- parse saved YachtWorld HTML
  yacht-scrape listing <url> [--headed] [--out dir] -- scrape a listing
  yacht-scrape browse [--limit N]          -- newest from sailboatlistings.com
  yacht-scrape detail <url> [--out dir]    -- sailboatlistings.com detail

Dependencies: uv pip install selectolax playwright && uv run playwright install chromium
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

try:
    from selectolax.parser import HTMLParser
    HAS_SELECTOLAX = True
except ImportError:
    HAS_SELECTOLAX = False

HAS_PLAYWRIGHT = False
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    pass


def eprint(*a, **kw):
    print(*a, file=sys.stderr, **kw)


def text_or_empty(node):
    return node.text(strip=True) if node else ""


def attr_or_empty(node, attr):
    return node.attributes.get(attr, "") if node else ""


# ─── YachtWorld via Playwright ──────────────────────────────────

def scrape_yw_with_playwright(url, timeout_sec=60, headless=True):
    """Navigate to YachtWorld in Playwright, return page HTML.

    headless=False opens a visible Chromium on Xvfb :99 (hermes-browser).
    The user can interact via http://hearth.lan:6080/vnc.html.
    """
    if not HAS_PLAYWRIGHT:
        eprint("ERROR: playwright not installed.")
        sys.exit(1)

    mode = "headed" if not headless else "headless"
    eprint(f"🧭 Launching {mode} Chromium to fetch: {url}")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--start-maximized",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"),
            locale="en-US",
        )
        page = context.new_page()
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            window.chrome = {runtime: {}};
        """)

        try:
            page.goto(url, wait_until="networkidle", timeout=timeout_sec * 1000)
        except Exception as e:
            eprint(f"  ⚠  Nav warning: {e}")

        content = page.content()

        if not headless:
            eprint("  🖥  Browser visible via VNC. Open http://hearth.lan:6080/vnc.html")
            eprint("     Solve captcha if shown. Press Ctrl+C when done (5 min timeout).")
            try:
                page.wait_for_timeout(300000)
            except KeyboardInterrupt:
                pass

        browser.close()

        if "Access Denied" in content[:1000]:
            eprint("  ❌ Blocked by Cloudflare.")
            return None
        if "Just a moment" in content[:500]:
            eprint("  ⚠  Cloudflare challenge not solved.")
            return None

        eprint(f"  ✅ Page loaded ({len(content):,} chars)")
        return content


# ─── Parse YW HTML ─────────────────────────────────────────────

def parse_yw_listings(html):
    """Parse YachtWorld search results from HTML."""
    if not HAS_SELECTOLAX:
        eprint("ERROR: selectolax required.")
        sys.exit(1)
    parser = HTMLParser(html)
    listings = []

    # Method 1: __NEXT_DATA__ (React SSR)
    for script in parser.css("script#__NEXT_DATA__"):
        raw = script.text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        props = data.get("props", {}).get("pageProps", {})
        boats = (props.get("listings") or props.get("boats")
                 or props.get("results") or props.get("searchResults")
                 or props.get("initialProps", {}).get("listings", []))
        for b in boats:
            if isinstance(b, dict):
                photos = b.get("photos") or b.get("images", [])
                images = []
                if isinstance(photos, list):
                    images = [p.get("url") or p.get("src", "") for p in photos if isinstance(p, dict)]
                elif isinstance(photos, dict):
                    images = [photos.get("url", "")]
                listings.append({
                    "title": b.get("title") or b.get("name", ""),
                    "url": b.get("url") or b.get("link", ""),
                    "price": b.get("priceDisplay") or b.get("price", ""),
                    "year": str(b.get("year", "") or ""),
                    "length": str(b.get("length", "") or ""),
                    "location": (b.get("city", "") + ", " + b.get("state", "")).strip(", "),
                    "images": images,
                    "description": b.get("description") or "",
                    "_source": "next_data",
                })
        if listings:
            return listings

    # Method 2: JSON-LD
    for script in parser.css('script[type="application/ld+json"]'):
        raw = script.text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else data.get("itemListElement", [])
        for item in items:
            if isinstance(item, dict):
                obj = item.get("item", item)
                offer = obj.get("offers", {})
                listings.append({
                    "title": obj.get("name", ""), "url": obj.get("url", ""),
                    "price": offer.get("price", ""), "year": "", "length": "",
                    "location": "", "images": [obj.get("image", "")] if obj.get("image") else [],
                    "description": obj.get("description", ""), "_source": "jsonld",
                })
        if listings:
            return listings

    return listings


def parse_yw_listing(html):
    """Parse a single YachtWorld listing page."""
    parser = HTMLParser(html)
    result = {"images": []}

    for script in parser.css("script#__NEXT_DATA__"):
        raw = script.text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
            props = data.get("props", {}).get("pageProps", {})
            boat = props.get("boat") or props.get("listing") or {}
            result.update({
                "title": boat.get("title") or boat.get("name", ""),
                "price": boat.get("price") or boat.get("priceDisplay", ""),
                "year": str(boat.get("year", "")),
                "length": str(boat.get("length", "")),
                "location": (boat.get("city", "") + ", " + boat.get("state", "")).strip(", "),
                "description": boat.get("description", ""),
            })
            photos = boat.get("photos") or boat.get("images", [])
            if isinstance(photos, list):
                result["images"] = [p.get("url", "") for p in photos if isinstance(p, dict)]
            elif isinstance(photos, dict):
                result["images"] = [photos.get("url", "")]
            return result
        except (json.JSONDecodeError, TypeError):
            pass

    return result


# ─── sailboatlistings.com ──────────────────────────────────────

def fetch_url(url):
    """Simple HTTP GET with urllib."""
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0 Safari/537.36",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        eprint(f"  HTTP error: {e}")
        return None


def scrape_sbl(limit=20):
    """Scrape newest from sailboatlistings.com."""
    html = fetch_url("https://www.sailboatlistings.com/sailboats_for_sale/")
    if not html:
        return []
    parser = HTMLParser(html)
    listings = []

    for table in parser.css("table[width='728']"):
        if len(listings) >= limit:
            break
        try:
            header_link = table.css_first("a.sailheader")
            if not header_link:
                continue
            title = text_or_empty(header_link)
            href = attr_or_empty(header_link, "href")
            if href and not href.startswith("http"):
                href = "https://www.sailboatlistings.com" + href
            img = table.css_first("img")
            img_src = ""
            if img:
                src = attr_or_empty(img, "src")
                if src:
                    img_src = "https://www.sailboatlistings.com" + src if src.startswith("/") else src
            fields = {}
            for td in table.css("td"):
                txt = td.text(strip=True)
                if txt and txt.endswith(":") and txt.rstrip(":") in (
                    "Length", "Year", "Type", "Hull", "Engine", "Location", "Asking"
                ):
                    key = txt.rstrip(":").lower()
                    nxt = td.next
                    while nxt and nxt.tag not in ("td", "TD"):
                        nxt = nxt.next
                    if nxt:
                        fields[key] = nxt.text(strip=True)
            listings.append({
                "title": title, "url": href, "price": fields.get("asking", ""),
                "length": fields.get("length", ""), "year": fields.get("year", ""),
                "type": fields.get("type", ""), "location": fields.get("location", ""),
                "image": img_src, "source": "sailboatlistings",
            })
        except Exception:
            continue
    return listings


def scrape_sbl_listing(url):
    """Scrape a single sailboatlistings.com listing."""
    html = fetch_url(url)
    if not html:
        return None
    parser = HTMLParser(html)
    result = {"url": url, "images": [], "source": "sailboatlistings"}
    h1 = parser.css_first("h1")
    if h1:
        result["title"] = text_or_empty(h1)
    for td in parser.css("td"):
        txt = td.text(strip=True)
        if txt in ("Asking:", "Price:"):
            nxt = td.next
            while nxt and nxt.tag not in ("td", "TD"):
                nxt = nxt.next
            if nxt:
                result["price"] = nxt.text(strip=True)
    for img in parser.css("img[src*='sailimg'], img[src*='photos']"):
        src = attr_or_empty(img, "src")
        if src:
            result["images"].append("https://www.sailboatlistings.com" + src if src.startswith("/") else src)
    return result


# ─── CLI ───────────────────────────────────────────────────────

def print_listings(listings):
    if not listings:
        print("  No listings found.")
        return
    print(f"\n{'='*80}\n  {len(listings)} sailboat listings\n{'='*80}\n")
    for i, lst in enumerate(listings, 1):
        print(f"  {i:3d}. {lst.get('title', 'Untitled')}")
        for k, emoji in [("price", "💰"), ("year", "📅"), ("length", "📏"), ("location", "📍")]:
            if lst.get(k):
                print(f"       {emoji} {lst[k]}")
        if lst.get("url"):
            print(f"       🔗 {lst['url']}")
        print()


def main():
    parser = argparse.ArgumentParser(description="YachtWorld / sailboatlistings.com scraper")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search", help="Search YachtWorld")
    p.add_argument("url", nargs="?", help="YachtWorld search URL")
    p.add_argument("--from-html", help="Parse saved HTML instead")
    p.add_argument("--headed", action="store_true", help="Show browser via VNC")

    p = sub.add_parser("listing", help="Scrape a YachtWorld listing")
    p.add_argument("url", help="Listing URL")
    p.add_argument("--out", help="Download images here")
    p.add_argument("--headed", action="store_true")

    p = sub.add_parser("browse", help="Newest from sailboatlistings.com")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("detail", help="View sailboatlistings.com listing")
    p.add_argument("url", help="Listing URL")
    p.add_argument("--out", help="Download images here")

    args = parser.parse_args()

    if args.command == "search":
        html = None
        if args.from_html:
            html = Path(args.from_html).read_text(encoding="utf-8", errors="replace")
        elif args.url:
            html = scrape_yw_with_playwright(args.url, headless=not args.headed)
        else:
            eprint("Provide a URL or --from-html")
            sys.exit(1)
        if not html:
            sys.exit(1)
        listings = parse_yw_listings(html)
        print_listings(listings)
        if listings:
            Path("yachtworld_listings.json").write_text(json.dumps(listings, indent=2, default=str))

    elif args.command == "listing":
        html = scrape_yw_with_playwright(args.url, headless=not args.headed)
        if not html:
            sys.exit(1)
        result = parse_yw_listing(html)
        print(f"\n{'='*70}\n  {result.get('title', 'N/A')}\n{'='*70}")
        for k, lbl in [("price", "Price"), ("year", "Year"), ("length", "Length"), ("location", "Location")]:
            print(f"  {lbl}: {result.get(k, 'N/A')}")
        if result.get("description"):
            print(f"\n  Description:\n{result['description']}")
        if result.get("images"):
            print(f"\n  Images ({len(result['images'])}):")
            for img in result["images"][:10]:
                print(f"    {img}")
        Path("yachtworld_listing.json").write_text(json.dumps(result, indent=2, default=str))

    elif args.command == "browse":
        listings = scrape_sbl(limit=args.limit)
        print_listings(listings)
        if listings:
            Path("sailboatlistings.json").write_text(json.dumps(listings, indent=2, default=str))

    elif args.command == "detail":
        result = scrape_sbl_listing(args.url)
        if not result:
            sys.exit(1)
        print(f"\n{'='*70}\n  {result.get('title', 'N/A')}\n{'='*70}")
        print(f"  Price:    {result.get('price', 'N/A')}")
        if result.get("description"):
            print(f"\n  Description:\n{result['description']}")
        if result.get("images"):
            print(f"\n  Images ({len(result['images'])}):")
            for img in result["images"][:10]:
                print(f"    {img}")
        Path("sbl_listing.json").write_text(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()