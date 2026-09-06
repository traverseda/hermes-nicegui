# Jellyfin IPTV — Finding iptv-org Channel Streams

The official iptv-org m3u playlists (https://iptv-org.github.io/) strip Warner Bros. Discovery channels (Cartoon Network, History, Discovery, Animal Planet, etc.) due to DMCA takedowns. The stream URLs still exist in closed GitHub issues on the iptv-org/iptv repo.

## Verified Working Streams (as of 2026-07-25)

### WBD / DMCA'd Channels (from GitHub issues)
| Channel | URL | Source Issue |
|---------|-----|-------------|
| Cartoon Network East | `http://23.237.104.106:8080/USA_CARTOON_NETWORK/index.m3u8` | #35518, #34660, #37801 |
| Cartoon Network (SkyGo) | `https://cdn4.skygo.mn/live/disk1/Cartoon_Network/HLSv3-FTA/Cartoon_Network.m3u8` | #34922 |
| Discovery Channel | `http://463758a0.ucomist.net/iptv/42L7TCH8YYKLBRHNPV52HBRN/6846/index.m3u8` | #39644 (similar ucomist pattern) |
| Global News | `https://live.corusdigitaldev.com/groupd/live/49a91e7f-1023-430f-8d66-561055f3d0f7/live.isml/master.m3u8` | #9374, #8388 — Corus official CDN, 720p HLS, CloudFront (YUL edge) |
| Animal Planet East | `https://dplus.gammacdn.workers.dev/videos/99.m3u8` | #17970 |

### Dead / Retry URLs (tested non-responsive)
- `https://fl2.moveonjoy.com/CARTOON_NETWORK/index.m3u8` — 403
- `http://livex.pop-app.live/s4n/poplive/ch215/playlist.m3u8` — History Cahnnel (dead)
- `http://38.91.57.12:2082/history/tracks-v1a1/mono.m3u8` — History Channel (dead)
- `http://38.96.178.201:80/live/Discovery/index.m3u8` — Discovery Channel (403)
- `https://i.mjh.nz/PlutoTV/` — all WBD channel IDs returned 404
- `http://fl3.moveonjoy.com/<CHANNEL>/index.m3u8` — all channels unreachable
- bozztv.com streams — all 404

## How to Find Working URLs

### iptv-org Issues Search Patterns

## Search Pattern

```text
site:github.com/iptv-org/iptv/issues "<CHANNEL_NAME>" stream m3u8 URL
```

Always cross-reference with:
- The iptv-org country playlist: `https://iptv-org.github.io/iptv/countries/<cc>.m3u`
- The Free-TV/IPTV playlist: `https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_<country>.m3u8`
- i.mjh.nz PlutoTV channel list: `https://i.mjh.nz/PlutoTV/all.xml`
- Corus CDN issues (Global, etc.): `site:github.com/iptv-org/iptv/issues "corusdigitaldev"`

## Stream Testing Methodology

Verify a stream before adding it to a playlist:

```bash
# Quick check — 200 + non-trivial size = likely alive
curl -s -o /dev/null -w "%{http_code} %{size_download}" --max-time 8 <URL>

# Check content is a real HLS manifest
curl -s --max-time 8 <URL> | head -10
# Should see #EXTM3U, #EXT-X-STREAM-INF, etc.
```

Results to expect:
- **200 + size≥100** — likely working HLS manifest
- **403** — geo-blocked or IP-restricted (try with User-Agent header)
- **404/000** — dead stream
- **Redirect (303/302)** — Nextcloud share pages; use `-L` to follow

## Verified Working Streams (curated, as of 2026-07-25)

### WBD / DMCA'd Channels (from GitHub issues)
| Channel | URL | Source | Notes |
|---------|-----|--------|-------|
| Cartoon Network East | `http://23.237.104.106:8080/USA_CARTOON_NETWORK/index.m3u8` | #35518 | Personal IP server — may drop |
| Cartoon Network (SkyGo) | `https://cdn4.skygo.mn/live/disk1/Cartoon_Network/HLSv3-FTA/Cartoon_Network.m3u8` | #34922 | Mongolian CDN — more stable |
| Discovery Channel | `http://463758a0.ucomist.net/iptv/42L7TCH8YYKLBRHNPV52HBRN/6846/index.m3u8` | #39644 | ucomist pattern; has .ts segments |
| Animal Planet East | `https://dplus.gammacdn.workers.dev/videos/99.m3u8` | #17970 | Gamma CDN worker; only active channel on that server |

### News
| Channel | URL | Source | Notes |
|---------|-----|--------|-------|
| CBC News | `https://d2ny9lo79ujali.cloudfront.net/CBC_News_International.m3u8` | iptv-org Canada | CloudFront, 1080p |
| Global News | `https://live.corusdigitaldev.com/groupd/live/49a91e7f-1023-430f-8d66-561055f3d0f7/live.isml/master.m3u8` | #9374, #8388 | **Corus official CDN**, 720p, CloudFront YUL edge (Montreal — close to Halifax) |

### Corus 40.160.24.53 Server
| Channel | URL | Notes |
|---------|-----|-------|
| Much (MuchMusic) | `http://40.160.24.53/MUCH/index.m3u8` | 720p; Jellyfin may need extra scan cycle |
| HGTV Canada | `http://40.160.24.53/HGTV/index.m3u8` | 720p; labeled "Home Network" in iptv-org playlist but URL says HGTV |

### Dead / No Longer Available (tested this session)
| Channel | Attempted URLs | Status |
|---------|---------------|--------|
| History Channel (all variants) | `http://livex.pop-app.live/...`, `http://38.91.57.12:2082/...`, bozztv, moveonjoy, i.mjh.nz PlutoTV | All dead |
| Food Network Canada | moveonjoy `fl2` (403), `fl3` (unreachable), iptv-org Canada (not listed) | No free stream found |
| National Geographic | `http://103.178.78.151:7505/...`, `http://66.102.120.18:8000/...` (both 000), bozztv (403) | All dead |
| Discovery Channel (Corus pattern) | 40.160.24.53 server (404) | Not on that server |
| YTV / Treehouse / Teletoon | 40.160.24.53 (404), iptv-org Canada (no URLs) | No free stream found |
| CTV Atlantic / CTV 2 Atlantic | bozztv (404), iptv-org Canada URL (000) | No free stream found |
| CTV News | 9c9media URL (000) | Dead |
| BNN Bloomberg | Only DASH format available (`.mpd`) — not compatible with Jellyfin Live TV |
| Smithsonian Channel | Not found in any public playlist | No stream found |
| Nova Scotia Legislature TV | YouTube live only (`AhgqAbhS3s0`) | No direct HLS available |
| CBC Documentary | CBC Akamai linear channels (all 404) | Not available as free stream |

## Key Discovery Patterns

### Corus CDN (Global News, Corus-owned channels)
The URL pattern `https://live.corusdigitaldev.com/groupd/live/<UUID>/live.isml/master.m3u8` serves official Corus streams. The UUID `49a91e7f-1023-430f-8d66-561055f3d0f7` was found in multiple iptv-org issues for Global News. These are served via CloudFront (verify headers: `x-serviced-by: Corus-NVIR-Cache-*`).

### 40.160.24.53 Server
This IP hosts multiple Corus channels under different path names. Only `/MUCH/` and `/HGTV/` returned 200. The naming is case-sensitive — all caps works, mixed case doesn't.

### What Usually Works vs What Doesn't
- **Official CDN streams** (Corus, CBC CloudFront) — most reliable but may be geo-blocked
- **bozztv.com** — mostly dead (all tested returned 404)
- **moveonjoy.com** (`fl2`, `fl3`) — mostly dead (403 or unreachable)
- **IP-based servers** (raw IP:port) — may work temporarily but are personal servers that die often
- **PlutoTV via i.mjh.nz** — channel IDs change frequently; all WBD-specific IDs returned 404 this round