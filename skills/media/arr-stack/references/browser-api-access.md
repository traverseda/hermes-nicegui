### Release Push (Grab via Browser)

When `arr-search grab` / `arr-hunter` fail with HTTP 415 (missing Content-Type), push releases directly to Sonarr/Radarr via the `release/push` endpoint using the browser tool chain.

**⚠️ Critical format detail: `release/push` expects a single object, NOT an array.** Sending `[ {...} ]` (an array with one element) returns HTTP 400: `"The JSON value could not be converted to ReleaseResource"`. Send a bare object `{...}` instead. This is the opposite of the manual import endpoint (`/api/v3/manualimport`), which does accept an array.

**Workflow:**
1. **Search for the release via Prowlarr** to get its download URL, GUID, and metadata:
   ```
   browser_navigate(url="https://<user>:<pass>@prowlarr.0u0.ca/api/v1/search?query=<search term>&limit=10&apikey=<PROWLARR_API_KEY>")
   ```

2. **Extract the release details** — find the best release (preferred indexer, best codec):
   ```javascript
   browser_console(expression="var d = JSON.parse(document.body.textContent); var pack = d.find(x => x.title.includes('<keyword>') && x.size > <min_size>); JSON.stringify({title: pack.title, downloadUrl: pack.downloadUrl, guid: pack.guid, size: pack.size, publishDate: pack.publishDate, indexer: pack.indexer})")
   ```

3. **Push to Sonarr/Radarr** — POST to `release/push` with all required fields:
   ```javascript
   browser_console(expression="fetch('https://sonarr.0u0.ca/api/v3/release/push?apikey=<SONARR_API_KEY>', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({title: '<title>', downloadUrl: '<downloadUrl>', guid: '<guid>', size: <size>, indexer: '<indexer>', downloadProtocol: 'usenet', protocol: 'usenet', publishDate: '<publishDate>', seriesId: <id>, fullSeason: true, seasonNumber: <season>})}).then(r => r.text()).then(t => {window.__push = t})")
   ```

4. **Read the result** — check `approved: true` and `downloadAllowed: true`:
   ```javascript
   browser_console(expression="window.__push")
   ```

**Required fields for `release/push`:**
- `title` — release title string
- `downloadUrl` — Prowlarr proxy download URL (from Prowlarr search results, not raw NZB link)
- `guid` — unique identifier from the indexer
- `size` — file size in bytes
- `publishDate` — ISO 8601 date string (required, cannot be empty)
- `indexer` — indexer name string (e.g. "NZBgeek", "NZBFinder")
- `downloadProtocol` / `protocol` — `"usenet"` or `"torrent"`
- `seriesId` or `movieId` — the Sonarr/Radarr internal ID
- **Alternative: `tvdbId`** — works in place of `seriesId`. Sonarr resolves the TVDB ID internally. Useful because Prowlarr search results include `tvdbId` directly, so you can push without looking up Sonarr's internal series ID first. Example: `tvdbId: 111051` for Pawn Stars.
- For season packs: `fullSeason: true` + `seasonNumber: <N>`
- For single episodes: `episodeIds: [<id>]`

**Common failure: Missing required fields**  
If the push returns errors like:
```json
[{"propertyName": "DownloadUrl", "errorMessage": "'Download Url' must not be empty."}, ...]
```
You're missing one or more required fields. The `downloadUrl` from Prowlarr search results is the proxied URL (`https://prowlarr.0u0.ca/N/download?...`), not the raw indexer URL. Always use the Prowlarr search result's `downloadUrl` field.

**Post-push behavior: approval ≠ immediate download**  
When `release/push` returns `approved: true` and `downloadAllowed: true`, the release has been registered in Sonarr/Radarr's internal release cache — but it does NOT immediately send the download to SABnzbd. The queue will remain empty until the next RSS sync cycle (or a manual `RssSync` command) picks up the release and triggers the actual download. To speed things up, trigger a `ProcessMonitoredDownloads` command after pushing:

```javascript
browser_console(expression="fetch('https://sonarr.0u0.ca/api/v3/command?apikey=...', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: 'ProcessMonitoredDownloads'})})")
```
<!-- SECRETS REDACTED during the cron-manager port (2026-08-15).
Live values live in ~/.hermes/.env (ARR_* / JELLYFIN_* keys) and
~/.hermes/internal-creds.json (basic auth). Do not paste real keys
into this file. -->
