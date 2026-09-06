#!/usr/bin/env python3
"""
Review a channel's schedule against the library. Shows:
  - The channel's current shows (from schedule config)
  - The channel's charter (from charters.md)
  - Library shows that fit the channel's theme but aren't on it yet

Usage:
  tunarr-channel-audit.py 15           # Audit by channel number
  tunarr-channel-audit.py Timeline     # Audit by channel name
"""

import json
import os
import re
import subprocess
import sys

CHARTERS_FILE = os.path.expanduser("~/.hermes/data/tunarr/charters.md")
SCHEDULES_DIR = os.path.expanduser("~/.hermes/data/tunarr/schedules")
TUNARR_CLI = os.path.expanduser("~/.hermes/scripts/tunarr")


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.expanduser("~"))
    if result.returncode != 0:
        print(f"Error running {' '.join(cmd)}: {result.stderr}", file=sys.stderr)
    return result.stdout


def get_channel_info(channel_ref):
    """Resolve a channel reference to number + name."""
    output = run([TUNARR_CLI, "channels", "list"])
    # Parse table rows: box-drawing chars separate columns
    # Lines look like: │ 15 │ Timeline           │ 5da17486... │
    rows = []
    for line in output.split("\n"):
        parts = [p.strip() for p in line.split("│")]
        if len(parts) >= 3:
            num_str = parts[1].strip()
            name_str = parts[2].strip()
            if num_str.isdigit():
                rows.append((num_str, name_str))

    if channel_ref.isdigit():
        for num, name in rows:
            if num == channel_ref:
                return num, name
        return channel_ref, None
    else:
        for num, name in rows:
            if name.lower() == channel_ref.lower():
                return num, name
        return None, None


def read_charter(channel_name):
    """Extract the charter for a channel from charters.md."""
    if not os.path.exists(CHARTERS_FILE):
        return None
    with open(CHARTERS_FILE) as f:
        content = f.read()

    # Find the section heading for this channel
    pattern = re.compile(
        rf"##\s+\d+\s+—\s+{re.escape(channel_name)}\s*\n(.*?)(?=\n##\s+\d+\s+—|\Z)",
        re.DOTALL,
    )
    m = pattern.search(content)
    if m:
        return m.group(1).strip()
    return None


def read_schedule(channel_name):
    """Read the schedule config for a channel."""
    slug = channel_name.lower().replace(" ", "-").replace("'", "").replace(":", "")
    path = os.path.join(SCHEDULES_DIR, f"{slug}.json")
    alt_path = os.path.join(SCHEDULES_DIR, f"{channel_name}.json")
    config_path = path if os.path.exists(path) else alt_path if os.path.exists(alt_path) else None
    if not config_path:
        return None
    with open(config_path) as f:
        return json.load(f)


def get_library_shows():
    """Get all shows from the Tunarr library (text table format)."""
    output = run([TUNARR_CLI, "library", "browse", "shows", "--limit", "1000"])
    shows = []
    current = None
    for line in output.split("\n"):
        # Title line: "  Title (Year)  Genre1, Genre2"
        m = re.match(r"  (.+?) \((\d{4})\)\s+(.*)", line)
        if m:
            if current:
                shows.append(current)
            title, year, genres_str = m.group(1), int(m.group(2)), m.group(3)
            genres = [g.strip() for g in genres_str.split(",") if g.strip()]
            current = {"title": title, "year": year, "genre": genres, "overview": ""}
        elif current and line.startswith("    "):
            current["overview"] += " " + line.strip()
    if current:
        shows.append(current)
    return shows


def show_matches_charter(show, keywords):
    """Check if a show's genre/overview matches the charter keywords."""
    title = (show.get("title", "") or "").lower()
    genres = [g.lower() for g in show.get("genre", show.get("genres", [])) if isinstance(g, str)]
    overview = (show.get("overview", "") or "").lower()

    # Check if title/overview contains any keywords
    for kw in keywords:
        kwl = kw.lower()
        if kwl in title or kwl in overview:
            return True
    # Check genre overlap
    for g in genres:
        if g in ["history", "documentary", "war", "biography", "historical",
                  "ancient", "medieval", "period", "world war"]:
            return True
    return False


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(1)

    channel_ref = sys.argv[1]
    num, name = get_channel_info(channel_ref)
    if not num:
        print(f"✗ Channel '{channel_ref}' not found")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  Channel {num} — {name}")
    print(f"{'='*60}")

    # Charter
    charter = read_charter(name)
    if charter:
        print(f"\n  📜 CHARTER\n  {'─'*56}")
        for line in charter.split("\n"):
            print(f"  {line.strip()}")

    # Schedule
    schedule = read_schedule(name)
    if schedule:
        print(f"\n  📺 CURRENT SHOWS")
        print(f"  {'─'*56}")
        slots = schedule.get("slots", [])
        for slot in slots:
            show_name = slot.get("show", "?")
            weight = slot.get("weight", 1)
            reasoning = slot.get("reasoning")
            if reasoning:
                print(f"  {show_name} {'★' * weight}")
                print(f"    └ {reasoning}")
            else:
                print(f"  {show_name} {'★' * weight}")
        print(f"  ({len(slots)} shows total)")
    else:
        print(f"\n  ⚠ No schedule config found")

    # Library candidates
    if charter:
        print(f"\n  🔍 LIBRARY CANDIDATES (history/period/documentary)")
        print(f"  {'─'*56}")

        # Extract keywords from charter
        keywords = re.findall(r'\*([^*]+)\*', charter)
        keywords = [k.strip() for k in keywords]

        shows = get_library_shows()
        schedule_shows = {s["show"] for s in schedule.get("slots", [])} if schedule else set()

        candidates = []
        for show in shows:
            title = show.get("title", "")
            if title in schedule_shows:
                continue
            g = [g.lower() for g in show.get("genre", show.get("genres", [])) if isinstance(g, str)]
            cat = (show.get("category", "") or "").lower()
            overview = (show.get("overview", "") or "").lower()

            # Filter for history/war/doc genre or keyword match
            matches = False
            for kw in keywords:
                if kw.lower() in title.lower() or kw.lower() in overview:
                    matches = True
                    break
            if not matches:
                if any(x in g for x in ["history", "documentary", "war", "biography",
                                         "historical", "ancient", "medieval", "period"]):
                    matches = True
                elif any(x in cat for x in ["history", "documentary"]):
                    matches = True

            if matches:
                year = show.get("year", "")
                genres = show.get("genre", show.get("genres", []))
                ep_count = show.get("totalEpisodes", show.get("episodeCount", "?"))
                candidates.append((title, year, genres, ep_count))

        if candidates:
            candidates.sort(key=lambda x: x[0].lower())
            for title, year, genres, eps in candidates:
                g_str = ", ".join(genres[:3]) if isinstance(genres, list) else str(genres)
                print(f"  • {title} ({year}) — {g_str}")
            print(f"\n  ({len(candidates)} candidates not on channel)")
        else:
            print(f"  (none found outside current lineup)")


if __name__ == "__main__":
    main()