#!/usr/bin/env -S uv run --script
# /// script
# dependencies = []
# ///
"""
Find shows in the Tunarr library that fit each channel but aren't on it yet.

Usage:
  python3 tunarr-find-fits.py                  # All channels
  python3 tunarr-find-fits.py "Sci-Fi Central" # One channel by name
  python3 tunarr-find-fits.py Sci-Fi           # Partial match
  python3 tunarr-find-fits.py --missing        # Shows in library not on ANY channel

Output: For each channel, shows library candidates with fit score + reasoning.
"""

import json, os, re, subprocess, sys
from pathlib import Path

CHARTERS_PATH = os.path.expanduser("~/.hermes/data/tunarr/charters.md")
SCHEDULES_DIR = os.path.expanduser("~/.hermes/data/tunarr/schedules")


def parse_tunarr_library(text):
    """Parse 'tunarr library browse shows --limit 1000' output into structured records.

    Format:
      3 Body Problem (2024)  Adventure, Drama, Fantasy
        A fateful decision in 1960s China echoes across space and time...

    Show names start with 2 spaces. Descriptions start with 4+ spaces.
    Genres start with an uppercase word after the (Year) or after 2+ spaces.
    """
    shows = []
    current = None
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("Total:"):
            continue

        stripped = line.strip()
        if line.startswith("  ") and not line.startswith("    "):
            parts = stripped.split("  ")
            if len(parts) >= 2:
                name_part = parts[0].strip()
                genre_part = parts[-1].strip()
                name_clean = re.sub(r'\s*\(\d{4}\)\s*$', '', name_part).strip()
                genres = [g.strip() for g in genre_part.split(",") if g.strip()]
                if genres and all(len(g) < 40 for g in genres):
                    if current:
                        shows.append(current)
                    current = {
                        "name": name_clean,
                        "genres": genres,
                        "description": ""
                    }
                    continue

        if current and line.startswith("    "):
            desc = line.strip()
            if desc and not desc.startswith("Total:"):
                if current["description"]:
                    current["description"] += " " + desc
                else:
                    current["description"] = desc

    if current:
        shows.append(current)
    return shows


def parse_charters(text):
    charters = {}
    current_name = None
    current_text = []
    for line in text.splitlines():
        m = re.match(r'^##\s+(\d+)\s*[—–-]+\s*(.+)$', line)
        if m:
            if current_name:
                charters[current_name] = " ".join(current_text).strip()
            current_name = m.group(2).strip()
            current_text = []
        elif current_name:
            current_text.append(line.strip())
    if current_name:
        charters[current_name] = " ".join(current_text).strip()
    return charters


def load_schedules():
    schedules = {}
    for fname in os.listdir(SCHEDULES_DIR):
        if not fname.endswith(".json"):
            continue
        channel = fname.replace(".json", "")
        path = os.path.join(SCHEDULES_DIR, fname)
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            continue
        shows = []
        for slot in data.get("slots", []):
            s = slot.get("show", "")
            s_clean = re.sub(r'\s*\(\d{4}\)\s*$', '', s).strip()
            shows.append({"name": s, "clean_name": s_clean})
        schedules[channel] = shows
    return schedules


def clean_show_name(name):
    return re.sub(r'\s*\(\d{4}\)\s*$', '', name).strip()


# Keyword profiles per channel — derived from charters
CHANNEL_PROFILES = {
    "Sci-Fi Central": {
        "keywords": ["sci-fi", "science fiction", "space", "alien", "future", "robot", "artificial intelligence",
                     "cyborg", "clone", "time travel", "parallel universe", "alternate reality", "dystopian",
                     "post-apocalyptic", "mars", "starship", "galaxy", "cyber", "superhero"],
        "genre_hits": ["Science Fiction", "Fantasy"],
        "genre_misses": ["Comedy", "Reality", "Food", "Documentary", "Sport"],
        "description": "speculative fiction — future, space, tech"
    },
    "Animation Station": {
        "keywords": ["anime", "animated", "mecha", "action cartoon", "serious", "epic", "dramatic"],
        "genre_hits": ["Animation", "Anime"],
        "genre_misses": ["Comedy", "Children", "Reality"],
        "description": "serious animation — not primarily comedy"
    },
    "Cartoon Corner": {
        "keywords": ["children", "family", "kids", "colorful", "charming", "fun"],
        "genre_hits": ["Animation", "Children", "Family"],
        "genre_misses": ["Horror"],
        "anti_keywords": ["adult", "inappropriate", "politically incorrect", "sick", "twisted", "alcoholic"],
        "description": "family-friendly animation"
    },
    "Comedy Tonight": {
        "keywords": ["comedy", "funny", "sitcom", "humor", "laughs", "satire", "parody", "absurd", "hilarious",
                     "comedian", "sketch"],
        "genre_hits": ["Comedy"],
        "genre_misses": ["Horror", "Documentary"],
        "description": "comedy — sitcoms, absurdist, comedic travel"
    },
    "Crime & Thriller": {
        "keywords": ["crime", "thriller", "detective", "spy", "espionage", "murder", "serial killer", "mafia",
                     "mob", "organized crime", "police", "fbi", "cia", "investigation", "undercover", "agent",
                     "noir", "heist", "drug"],
        "genre_hits": ["Crime", "Thriller", "Suspense", "Mystery"],
        "genre_misses": ["Comedy", "Science Fiction", "Fantasy", "Animation"],
        "description": "criminals, investigators, spies"
    },
    "The Spooky Channel": {
        "keywords": ["horror", "supernatural", "zombie", "ghost", "demon", "vampire", "paranormal", "haunted",
                     "psychic", "werewolf", "creepy", "scary", "monster", "apocalypse", "possession", "witch",
                     "cursed", "serial killer", "psychological horror"],
        "genre_hits": ["Horror", "Thriller"],
        "genre_misses": ["Comedy", "Food", "Reality", "Sport", "Western"],
        "description": "horror, supernatural, and the uncanny"
    },
    "Fantasy Realm": {
        "keywords": ["fantasy", "magic", "mythical", "sword", "sorcery", "dragon", "medieval", "kingdom",
                     "enchanted", "quest", "legend", "myth", "fairy tale", "elves", "wizard", "prophecy",
                     "gods", "goddess", "demigod"],
        "genre_hits": ["Fantasy", "Adventure"],
        "genre_misses": ["Comedy", "Documentary", "Reality", "Sport", "Food"],
        "description": "epic fantasy, swords-and-sorcery"
    },
    "Reality Relief": {
        "keywords": ["survival", "competition", "reality", "game show", "challenge", "contest", "wilderness",
                     "bushcraft", "forging", "blacksmith", "survivor"],
        "genre_hits": ["Reality", "Game Show"],
        "genre_misses": ["Horror", "Fantasy", "Science Fiction", "Food"],
        "description": "survival and competition"
    },
    "90s Nostalgia": {
        "keywords": ["1990", "90s", "nineties"],
        "genre_hits": [],
        "genre_misses": ["Documentary", "Food", "Reality", "Soap"],
        "description": "90s comfort programming — aired 1990-1999"
    },
    "Mixed Bag": {
        "keywords": ["weird", "experimental", "anthology", "surreal", "strange", "philosophical", "art house",
                     "psychological", "mind-bending", "trippy", "dreamlike"],
        "genre_hits": [],
        "genre_misses": ["Reality", "Food", "Sport"],
        "description": "weird, experimental, anthology"
    },
    "The Kitchen": {
        "keywords": ["cooking", "food", "chef", "baking", "restaurant", "cuisine", "kitchen", "culinary",
                     "pastry", "gourmet", "recipe"],
        "genre_hits": ["Food"],
        "genre_misses": ["Horror", "Science Fiction", "Fantasy", "Action", "Crime"],
        "description": "food television"
    },
    "How It Works": {
        "keywords": ["science", "experiment", "manufacturing", "engineering", "how it's made", "invention",
                     "technology", "build", "mechanic", "physics", "chemistry", "biology"],
        "genre_hits": ["Documentary"],
        "genre_misses": ["Horror", "Fantasy", "Romance", "Soap"],
        "description": "science, experiments, manufacturing"
    },
    "The Water Cooler": {
        "keywords": ["drama", "political", "prestige", "character", "relationship", "family", "legal",
                     "medical", "war", "historical", "period", "social"],
        "genre_hits": ["Drama"],
        "genre_misses": ["Reality", "Food", "Game Show", "Children", "Sport"],
        "description": "prestige character drama"
    },
    "Animated Comedy": {
        "keywords": ["adult animation", "adult cartoon", "adult animated"],
        "genre_hits": ["Animation", "Comedy"],
        "genre_misses": ["Children", "Family"],
        "description": "adult animation"
    },
    "Timeline": {
        "keywords": ["history", "historical", "war", "period", "era", "century", "ancient", "medieval",
                     "WWII", "civil war", "biography", "documentary history", "docuseries"],
        "genre_hits": ["History", "War", "Documentary"],
        "genre_misses": ["Science Fiction", "Fantasy", "Horror", "Comedy"],
        "description": "wars, mysteries, period drama"
    },
    "Wanderlust": {
        "keywords": ["travel", "food travel", "road trip", "adventure travel", "countryside", "farming",
                     "farm", "exploration", "world", "globetrotting", "road", "journey", "voyage"],
        "genre_hits": ["Travel", "Adventure", "Documentary"],
        "genre_misses": ["Horror", "Fantasy", "Science Fiction", "Crime"],
        "description": "travel, farming, countryside, open roads"
    },
    "Nature": {
        "keywords": ["nature", "wildlife", "ocean", "planet", "animal", "documentary nature", "ecosystem",
                     "environment", "earth", "forest", "ocean", "arctic", "desert", "prehistoric", "dinosaur"],
        "genre_hits": ["Documentary", "Nature"],
        "genre_misses": ["Horror", "Fantasy", "Science Fiction", "Crime", "Comedy"],
        "description": "the natural world — Attenborough, Planet Earth"
    },
    "The Neighborhood": {
        "keywords": ["family", "community", "warm", "wholesome", "heartwarming", "neighbors",
                     "small town", "suburban"],
        "genre_hits": ["Animation", "Comedy", "Family"],
        "genre_misses": ["Horror", "Crime", "Reality", "Food", "Sport"],
        "description": "warm animated comedies about families and communities"
    },
}


def score_show_for_channel(show, channel_name):
    profile = CHANNEL_PROFILES.get(channel_name)
    if not profile:
        return 0, ["no profile"]

    score = 0
    reasons = []

    name_lower = show["name"].lower()
    desc_lower = show["description"].lower()
    genres_lower = [g.lower() for g in show["genres"]]
    all_text = name_lower + " " + desc_lower

    for gh in profile["genre_hits"]:
        if gh.lower() in genres_lower:
            score += 3
            reasons.append(f"genre:{gh}")

    for gm in profile["genre_misses"]:
        if gm.lower() in genres_lower:
            score -= 4
            reasons.append(f"anti-genre:{gm}")

    anti_keywords = profile.get("anti_keywords", [])
    for ak in anti_keywords:
        if re.search(r'\b' + re.escape(ak.lower()) + r'\b', all_text):
            score -= 3
            reasons.append(f"anti-kw:{ak}")

    for kw in profile["keywords"]:
        if re.search(r'\b' + re.escape(kw.lower()) + r'\b', all_text):
            score += 1
            reasons.append(f"keyword:{kw}")

    for kw in profile["keywords"]:
        if re.search(r'\b' + re.escape(kw.lower()) + r'\b', name_lower):
            score += 2
            reasons.append(f"title-match:{kw}")
            break

    if channel_name in ("Nature", "How It Works", "Timeline"):
        if "documentary" in genres_lower:
            score += 2
            reasons.append("doc-bonus")

    return score, reasons


def main():
    args = sys.argv[1:]
    filter_channel = None
    show_missing_only = False

    for arg in args:
        if arg == "--missing":
            show_missing_only = True
        else:
            filter_channel = arg

    charters_text = ""
    try:
        with open(CHARTERS_PATH) as f:
            charters_text = f.read()
    except FileNotFoundError:
        charters_text = ""
    charters = parse_charters(charters_text)

    schedules = load_schedules()
    all_programmed_shows = set()
    for ch, shows in schedules.items():
        for s in shows:
            all_programmed_shows.add(s["clean_name"])

    result = subprocess.run(
        [os.path.expanduser("~/.hermes/scripts/tunarr"), "library", "browse", "shows", "--limit", "1000"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        print(f"ERROR: tunarr library browse failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    library = parse_tunarr_library(result.stdout)

    if not library:
        print("ERROR: No shows parsed from library output", file=sys.stderr)
        sys.exit(1)

    channel_names = sorted(schedules.keys(), key=lambda n: list(charters.keys()).index(n) if n in charters else 99)

    if show_missing_only:
        on_any_channel = set()
        for ch, shows in schedules.items():
            for s in shows:
                on_any_channel.add(s["clean_name"])

        print("=" * 70)
        print("  SHOWS IN LIBRARY NOT ON ANY CHANNEL")
        print("=" * 70)
        print()
        unassigned = [s for s in library if clean_show_name(s["name"]) not in on_any_channel]
        for s in sorted(unassigned, key=lambda x: x["name"]):
            genres = ", ".join(s["genres"])
            print(f"  \u2022 {s['name']}  ({genres})")
            if s["description"]:
                desc = s["description"][:120]
                print(f"    {desc}")
            print()
        print(f"  ({len(unassigned)} unassigned shows)")
        return

    if filter_channel:
        matched = [n for n in channel_names if filter_channel.lower() in n.lower()]
        if not matched:
            print(f"No channel matching '{filter_channel}'. Available: {', '.join(channel_names)}")
            sys.exit(1)
        channel_names = [matched[0]]

    for ch_name in channel_names:
        already_on = {s["clean_name"] for s in schedules.get(ch_name, [])}
        charter = charters.get(ch_name, "")

        candidates = []
        for show in library:
            clean = clean_show_name(show["name"])
            if clean in already_on:
                continue
            score, reasons = score_show_for_channel(show, ch_name)
            if score >= 2:
                candidates.append((score, show, reasons))

        candidates.sort(key=lambda x: -x[0])

        print("=" * 70)
        print(f"  {ch_name}")
        if charter:
            print(f"  \ud83d\udcdc {charter[:150]}")
        print("=" * 70)
        print(f"  Currently: {len(already_on)} shows")
        print(f"  Library candidates (score \u2265 2): {len(candidates)}")
        print()

        if not candidates:
            print("  (no strong candidates found)")
            print()
            continue

        for score, show, reasons in candidates[:15]:
            genres = ", ".join(show["genres"])
            reason_str = ", ".join(reasons[:5])
            desc = show["description"][:100] if show["description"] else ""
            print(f"  [{score:2d}] {show['name']}  ({genres})")
            print(f"       {reason_str}")
            if desc:
                print(f"       {desc}")
            print()

        if len(candidates) > 15:
            print(f"  ... and {len(candidates) - 15} more")
        print()


if __name__ == "__main__":
    main()