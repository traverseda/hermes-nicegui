# SABnzbd — Connection & Auth

Connection details for `sabnzbd.0u0.ca` (not `sab.0u0.ca`).

## Base URL

| Field | Value |
|-------|-------|
| URL | `https://sabnzbd.0u0.ca` |
| Nginx basic auth | Yes (`hermes` + LDAP password) |
| API key | Stored in `~/workspace/arr-stack/secrets.json` |

## Finding the API Key via Web UI

If the API key is missing from secrets.json or the web UI config, scrape it from the HTML:

```bash
curl -sk -u 'hermes:<LDAP_PASSWORD>' 'https://sabnzbd.0u0.ca/config/' | grep -o "apikey=[a-f0-9]*"
```

Or from the browser:
```javascript
// In browser_console, after navigating to https://<user>:<pass>@sabnzbd.0u0.ca/config/
document.body.innerHTML.match(/apikey=[a-f0-9]+/g)
```

The key appears in JavaScript variable assignments like:
```javascript
var folderBrowseUrl = '../api?mode=browse&output=json&apikey=<key>';
```

Current known API key: `<REDACTED_HEX_TOKEN>` (confirmed 2026-07-29).

## Request Pattern

```bash
curl -sk -u 'hermes:<LDAP_PASSWORD>' \
  'https://sabnzbd.0u0.ca/api?mode=queue&output=json&apikey=<key>'
```

Note the API endpoint is `/api?mode=<mode>&output=json` — no `/api/v3/` path prefix like Sonarr/Radarr.

## SSL Certificate

SABnzbd serves a **self-signed certificate** (Proxmox LXCs behind Traefik/npm). Python's `urllib.request.urlopen` will fail with:
```
SSL: CERTIFICATE_VERIFY_FAILED certificate verify failed: self-signed certificate (_ssl.c:1016)
```

**Workarounds:**
- **curl**: use `-k` or `--insecure`
- **Python `requests`**: `verify=False`
- **Python `urllib`**: create a custom `SSLContext` with `check_hostname=False` and `verify_mode=ssl.CERT_NONE`

The `arr-stack` library's `api_get()` function uses `urllib.request.urlopen` without SSL context overrides, so it will fail on SAB's self-signed cert. Use `curl -sk` via terminal or the browser tool chain as a fallback.

If the library needs to call SAB directly, override the SSL context in `api_get()`:
```python
import ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
resp = urlopen(req, timeout=timeout, context=ctx)
```

## Health Check

SABnzbd responds to `/api` on the root path:
```
GET /api?mode=queue&output=json&apikey=<key>
     → {"queue": {"status": "Idle", "speed": "0", ...}}
```

When idle, speed is `0.00 KB/s` and status is `"Idle"`. An empty queue with no active downloads is healthy — no need to flag "under 100 KB/s" as a warning.

## Known Quirks

- `sab.0u0.ca` returns 404 — the correct hostname is `sabnzbd.0u0.ca`
- `sabnzbd.0u0.ca/api` with no API key returns "API Key Required" (HTTP 403)
- The web UI loads at `/` — responsive HTML5 interface via Glitter V2
- No rate limits observed on the /api endpoint
<!-- SECRETS REDACTED during the cron-manager port (2026-08-15).
Live values live in ~/.hermes/.env (ARR_* / JELLYFIN_* keys) and
~/.hermes/internal-creds.json (basic auth). Do not paste real keys
into this file. -->
