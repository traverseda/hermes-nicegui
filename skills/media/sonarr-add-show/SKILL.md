---
name: sonarr-add-show
description: "Add a TV show to Sonarr by title — lookup, add, and trigger search in one command"
version: 1.0.0
author: hermes-agent
tags: [sonarr, arr-stack, media]
---

# sonarr-add-show.py

Add one or more TV shows to Sonarr from the command line. Looks up the show via TVDB, adds it with the default quality profile (8 — compact HEVC/AV1), and kicks off episode search.

## Usage

```bash
~/.hermes/scripts/sonarr-add-show.py "Home Movies" "Daria"
~/.hermes/scripts/sonarr-add-show.py "The Great North"
~/.hermes/scripts/sonarr-add-show.py "King of the Hill (1997)"
```

## Defaults

- Quality profile: 8 (compact HEVC/AV1 preferred)
- Language: English
- Root folder: `/library/shows`
- Tags: hevc, english
- All seasons monitored (except specials)
- Searches for missing episodes on add

## Example output

```
🔍 Looking up: Home Movies
✓ Home Movies (1999) — added as series #468, searching for episodes
🔍 Looking up: Daria
✓ Daria (1997) — added as series #469, searching for episodes
```

Already-added shows are skipped with a `⏭` indicator — no duplicates.