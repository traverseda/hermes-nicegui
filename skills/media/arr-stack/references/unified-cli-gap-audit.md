# Unified `arr` CLI — Gap Audit & History-Scan Recipe

Audited 2026-08-10, right after the unified CLI was built by opencode (190 tests green, live smoke tests passed: `sonarr series list`, `sabnzbd status`, `prowlarr indexers list` all exit 0). Purpose: make sure the CLI covers every operation previously done via curl, per the user's "no more curl for *arr" mandate.

## Results: what the CLI does NOT cover yet

| # | Gap | Historical curl ops (endpoints) | Notes |
|---|-----|---------------------------------|-------|
| 1 | **Custom formats CRUD** — sonarr + radarr | `GET/POST/PUT/DELETE /api/v3/customformat{/schema,/{id}}` | Biggest gap. Houses the whole systemic-fix workflow (Survivor Québec exemption = negated ReleaseTitleSpecification edits, CF 2/3/4/6 create/update/delete). `quality_profile.py` script exists but no CLI command group. |
| 2 | **Quality-profile detail/edit** | `GET /qualityprofile/{id>` (formatItems w/ CF scores), `PUT /qualityprofile/{id}` | CLI has `quality-profiles list` only. Re-scoring CFs (the "not available"/score fixes) needs `show <id>` + score edit. |
| 3 | **Quality definitions** | `GET/PUT /qualitydefinition/{id}`, `PUT /qualitydefinition/update` | The 720p/1080p preferred/max size tuning (pref=12 max=200 compact-archive work). Not in CLI. |
| 4 | **Command run + poll** | `POST /api/v3/command` (`RssSync` after CF edits, `ManualImport`), `GET /command/{id}` | CLI `commands` is list-only. Futurama RenameFiles flow polled `/command/{id}`. Need `commands run <Name>` + `commands show <id>`. |
| 5 | **Manual import (sonarr)** | `GET /api/v3/manualimport`, ManualImport command | Only via old `arr-queue-fix-import`/`arr-queue-manual-import` scripts, not the CLI. |
| 6 | **Jellyfin module — entire app** | `/Items`, `/Sessions`, `/System/ActivityLog/Entries`, `POST /ScheduledTasks/Running/{id}`, `LiveTv/TunerHosts` CRUD, `/Users`, `/System/Info` | No `arr jellyfin` at all. Channel-curation genre checks, "who's playing", playback history, guide-refresh trigger, tunarr tuner work all still curl/browser. |
| 7 | **SAB server stats / config** | `?mode=server_stats`, `?mode=get_config` | Quota checks (monthly/weekly totals) + server/SSL health have no CLI command. |
| 8 | minor | `POST /rootfolder`, `GET /downloadclient`, `GET /importlist` | Root-folder add, download-client list, import-list list — scripted around but not in CLI. |

Covered fine: series/movie CRUD+lookup, episodes, files rename/delete, queue ops (+blocklist), history, prowlarr indexers/search/apps, cross `search --grab`, hunter, upgrade, health, disk-space.

When extending the CLI to close a gap: edit `SPEC.md` + source in `~/workspace/arr-stack`, run `uv run pytest` and the read-only smoke tests, then **mark the row done here**.

## Recipe: find every past curl against the stack (session-DB audit)

The CLI gap list above came from mining the Hermes session DB for historical terminal commands. Reusable — rerun whenever the CLI grows or "did we ever do X via curl?" comes up:

```python
import sqlite3, json, re
db = sqlite3.connect('/home/hermes/.hermes/state.db')
db.row_factory = sqlite3.Row
targets = re.compile(r'(sonarr|radarr|prowlarr|sabnzbd|sab|jelly|emby)\.0u0\.ca', re.I)
cmds = set()
for row in db.execute("SELECT content, tool_calls FROM messages WHERE role='assistant'"):
    blob = (row['tool_calls'] or '') + ' ' + (row['content'] or '')
    try:
        tc = json.loads(row['tool_calls'] or '[]')
    except json.JSONDecodeError:
        tc = []
    for call in tc:
        try:
            cmd = call['function']['arguments']
            if 'curl' in cmd and targets.search(cmd):
                cmds.add(cmd)
        except (KeyError, TypeError):
            continue
```

Then classify each command by `(app, method, URL-path)` — e.g. `re.search(r'curl -s(?: -X (\w+))?.*(?:sonarr|radarr|prowlarr)\.0u0\.ca(/api/[^?\"\']+)', cmd)` — and group to a unique set. Sonarr/Radarr use `/api/v3/`, Prowlarr `/api/v1/`, Jellyfin no prefix, SAB `?mode=...` query API. The 2026-08-10 audit found **471 curl commands → 166 distinct (app, method, path) ops**.

Key schema facts:
- `messages` table columns: `role`, `content`, `tool_calls` (JSON string of function calls), `tool_name`.
- Terminal commands live in `tool_calls[].function.arguments` (JSON), not `content`.
- `content` may be empty for tool-calling turns — read `tool_calls` instead.
- Dedupe: same command string appears across many sessions.