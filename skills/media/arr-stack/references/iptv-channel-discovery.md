# IPTV Channel Discovery — iptv-org DMCA'd Channels

## The Problem

The main iptv-org M3U playlist (`https://iptv-org.github.io/iptv/index.m3u`) is missing many popular channels because Warner Bros. Discovery and other rightsholders filed DMCA takedowns. Channels like **History Channel, Cartoon Network, Discovery Channel, CNN, Boomerang, TBS, TNT, Adult Swim, Animal Planet, HGTV, Food Network, Travel Channel, and others** were removed from the main repo.

However, the stream URLs themselves are still live at their source hosts — only the references in the repo were removed. Users posted the working URLs in GitHub issues before they were removed.

## The Approach

1. **Search the iptv-org/iptv issue tracker** for the channel name:
   ```
   site:github.com/iptv-org/iptv/issues "History Channel" stream URL
   site:github.com/iptv-org/iptv/issues "Cartoon Network" m3u8
   ```

2. **Look for closed issues** with label `approved:streams:add` or `streams:add` — these had confirmed working URLs at the time they were opened.

3. **Check for more recent replacement issues** — many DMCA'd channels had their URLs stripped from the original issue, but users opened new "Find: <channel>" issues with updated links.

4. **Verify the URL still works** before adding to playlists:
   ```bash
   curl -sI --max-time 10 "http://example.com/playlist.m3u8" | head -5
   ```
   A 200 or 302 response suggests the stream is alive. A 404 or timeout means it's dead.

## Key Canadian Channels to Find

For Halifax NS / Canada:
- **CBC, CTV, Global** — usually available in the main Canada playlist
- **History Channel (Canada)** — WBD-owned, DMCA'd, find in issues
- **Discovery Channel (Canada)** — WBD-owned
- **Cartoon Network / Adult Swim (Canada)** — WBD-owned
- **Global News Halifa** — local news variant
- **CTV Atlantic / CTV News** — Atlantic Canada news

## Merging into Jellyfin

1. Collect all working stream URLs
2. Build an M3U file with proper `#EXTINF` headers:
   ```
   #EXTINF:-1 tvg-id="HistoryChannel.ca" tvg-name="History Channel" tvg-logo="https://logo.url" group-title="Canada",History Channel
   http://live.stream.url/playlist.m3u8
   ```
3. Upload to Jellyfin via web UI: Dashboard → Live TV → M3U Tuner
4. Or use Jellyfin API to add a URL-based M3U tuner

## URL Freshness — Real-World Findings (2026-07-25)

**Most streams from iptv-org issues >6 months old are dead.** Tested extensively this session:

### Working (as of 2026-07-25)
| Source Pattern | Example | Status |
|---------------|---------|--------|
| Raw IP + port (personal server) | `http://23.237.104.106:8080/<CHANNEL>/index.m3u8` | ⚠️ Cartoon Network worked, but personal servers are fragile |
| SkyGo Mongolia CDN | `https://cdn4.skygo.mn/live/disk1/<CHANNEL>/HLSv3-FTA/<CHANNEL>.m3u8` | ✅ 720p, more stable |
| ucomist.net | `http://463758a0.ucomist.net/iptv/<HASH>/<ID>/index.m3u8` | ✅ Discovery worked, has actual .ts segments |
| gamma CDN worker | `https://dplus.gammacdn.workers.dev/videos/<N>.m3u8` | ✅ Animal Planet (ch 99) worked; most other channels returned "Stream Not Found" |

### Dead / Retry
| Source Pattern | Symptom | Notes |
|---------------|---------|-------|
| moveonjoy.com (`fl2`, `fl3`) | 403 or unreachable | Was a major source, all dead now |
| bozztv.com | 404 | Was in Free-TV/IPTV Canada playlist, all gone |
| i.mjh.nz/PlutoTV | 404 | WBD channel IDs changed/expired |
| pop-app.live / livex.pop-app.live | unreachable | Used for many WBD channels (History, Food, etc.) |
| Raw IPs (38.91.x.x, 38.96.x.x) | timeout or 403 | History, Discovery IPs all dead |

### Verification Technique
```bash
# Check both HTTP status AND content (a 200 with 16 bytes = "Stream Not Found")
curl -s -o /dev/null -w "%{http_code} %{size_download}" --max-time 8 "<URL>"

# For a positive test, also check the actual content has playlist markers
curl -s --max-time 8 "<URL>" | head -3
# Expect to see: #EXTM3U, #EXT-X-VERSION, #EXTINF, and actual .ts or sub-m3u8 URLs
```

## Hosting Custom Playlists

Once you've curated the channels, host the M3U file at a URL Jellyfin can reach. See `references/nextcloud-webdav.md` for the Nextcloud pattern (UUID-based user IDs, public shares).