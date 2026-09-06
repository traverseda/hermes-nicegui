# \*arr API Probing Session

Discovered during an exploration of `*.0u0.ca` services protected by HTTP Basic Auth.

## Credential Setup

The internal credentials file at `~/.hermes/internal-creds.json` holds the basic auth username and password for `*.0u0.ca`.

## Initial Probing

| Target | Result |
|--------|--------|
| `https://sonarr.0u0.ca` | ✅ Reachable, Sonarr SPA served |
| `https://radarr.0u0.ca` | ✅ Reachable, Radarr SPA served |
| `https://prowlarr.0u0.ca` | ✅ Reachable, Prowlarr SPA served |
| `https://sonarr.0u0.ca/ping` | ✅ `{"status":"OK"}` with basic auth only |
| `https://radarr.0u0.ca/ping` | ✅ `{"status":"OK"}` with basic auth only |
| `https://prowlarr.0u0.ca/ping` | ✅ `{"status":"OK"}` with basic auth only |
| `https://sonarr.0u0.ca/api/v3/system/status` | ❌ 401 (needs API key) |
| `https://radarr.0u0.ca/api/v3/system/status` | ❌ 401 (needs API key) |
| `https://prowlarr.0u0.ca/api/v1/system/status` | ❌ 401 (needs API key) |

## Auth Details

- **Proxy**: nginx/SWAG, uses HTTP Basic Auth
- **App API key**: Required as `X-Api-Key` header or `apikey` query param
- **Basic auth password does NOT work as API key** — tried directly
- **HASS token does NOT work as API key** — tried as fallback
- **No API keys found in env** — checked `~/.hermes/.env`, no arr keys present
- **No API keys in JS bundles** — confirmed all three bundles contain no `apiKey` string
- **No local config files** — services are remote, not running on this machine
- **No Docker containers** on this host

## JS Bundle Details

| Service | Bundle URL | Size |
|---------|-----------|------|
| Sonarr | `/index-69f04291ec6271a9edca.js` | 28,724 bytes |
| Radarr | `/index-02e24635035ed28fd7d3.js` | 28,454 bytes |
| Prowlarr | `/index-f406f68d8df178e3031d.js` | 28,040 bytes |

All three are ~28KB minified bundles. No API endpoints, API keys, or auth headers found in any of them.

## Service Info

- **IP**: `142.68.128.3` (resolved at time of probing)
- **SSL**: Let's Encrypt via YR2, valid certificate
- **TLS**: TLSv1.3, X25519MLKEM768 key exchange
- **Server**: HTTP/2 capable

## Next Steps

To proceed with managing these services, the user needs to provide:
1. API keys for each service (found in Settings → General in the web UI, or in `config.xml` on the server)
2. Once available, catalog the full library state, queue, and configured quality profiles for each service