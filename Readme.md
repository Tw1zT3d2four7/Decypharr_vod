# Decypharr VOD for Dispatcharr

<p align="center">
  <img src="logo.png" alt="Decypharr VOD" width="240">
</p>

**Version:** 1.0.3  
**Author:** Tw1zT3d2four7

Decypharr VOD imports media managed by **Decypharr** into Dispatcharr as native VOD content.

This release enforces a strict **one-canonical-relation rule**: each movie, series, and episode has one VOD stream relation. TMDB genres are metadata/categories and do not create additional VOD streams or genre relations.

## v1.0.3 Feature Set

- Authenticated Decypharr API discovery through `/api/browse/__all__`
- Persistent Decypharr inventory caching
- Parallel child-release discovery with configurable API workers
- Movie, TV episode, season-pack, multi-file and Blu-ray handling
- Canonical Arr-style title parsing
- Native Dispatcharr VOD movie, series and episode objects
- Normalized `.strm` presentation files
- Decypharr API-backed playback with HTTP Range support
- FFprobe technical metadata
- Configurable preferred audio language with source-default fallback
- Optional TMDB metadata, artwork and genre matching
- TMDB genre-based Dispatcharr VOD categories
- TMDB genres retained as metadata/categories on the canonical VOD relation
- **Exactly one canonical VOD relation per movie, series, and episode**
- Legacy `--genre-*` / `-genre-*` VOD relations are blocked and purged
- Hard runtime relation-save guard for legacy genre streams
- Duplicate protection and stale-library cleanup
- Real background automatic scanning
- Progressive initial importing so playable catalog entries can appear before the entire scan finishes
- Fast initial import separated from expensive FFprobe/TMDB enrichment
- Metadata enrichment in configurable batches
- Active synthetic XC account required by Dispatcharr VOD relations
- Protected synthetic-account refresh that returns a successful no-op
- Repair action
- Working Clean + Verify VOD Database action
- Optional browser transcoding using NVIDIA NVENC, Intel QSV, AMD/Intel VAAPI or CPU
- Deterministic next-episode metadata within the current season
- No Emby dependency

## Canonical VOD relation rule

A title may have multiple TMDB genres, but those genres are **categories/metadata only**. They must never become separate playable VOD relations.

For example, `10,000 BC` must result in one canonical stream:

```text
decypharr-movie-1458700
```

and must not produce relations such as:

```text
decypharr--movie-1458700--genre-action
decypharr--movie-1458700--genre-drama
decypharr--movie-1458700--genre-adventure
```

v1.0.3 adds a hard runtime guard around the Dispatcharr VOD relation save boundary. If a legacy genre relation is attempted for the plugin-owned synthetic account, the save is blocked and logged. Legacy genre relations are also purged so stale records cannot remain in the catalog.

This is intentionally stronger than merely cleaning duplicates after a scan: **legacy genre relations are not valid VOD streams and cannot be recreated by the plugin's guarded relation save path.**

## Architecture

```text
                    Decypharr
                         │
                  Authenticated API
                         │
                         ▼
                 Decypharr VOD Plugin
                 Discover / Normalize
                 Metadata / Playback
                         │
                    .strm + metadata
                         │
                         ▼
                     Dispatcharr
                     Native VOD
                         │
                      Playback
                         │
                         ▼
                 Decypharr API Proxy
```

Generated `.strm` files do **not** contain the Decypharr API token.

## Requirements

### Required

- Dispatcharr
- Decypharr
- Decypharr API access
- Decypharr API token
- FFprobe

### Optional

- TMDB API key
- FFmpeg and GPU access when browser transcoding is enabled

## Installation

Install the plugin using the normal Dispatcharr plugin installation process.

The plugin is installed under:

```text
/data/plugins/decypharr_vod/
```

The primary plugin file is:

```text
plugin.py
```

After installation, restart Dispatcharr if required by the plugin manager so the v1.0.3 runtime guard is loaded.

## Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| Decypharr API URL | `http://192.168.1.11:8282` | Decypharr API base URL |
| Decypharr API Token | — | Decypharr authentication token |
| Normalized Library | `/data/plugins/decypharr_vod/library` | Generated `.strm` presentation library |
| TMDB API Key | — | Optional TMDB API key |
| TMDB Metadata | Enabled | Enables TMDB metadata and genre categories |
| FFprobe Path | `/usr/local/bin/ffprobe` | FFprobe executable |
| Preferred Audio Language | eng (English) | Preferred audio track when multiple tracks exist |
| Auto Scan Interval | 60 seconds | Background inventory scan interval |
| Fast Initial Scan | Enabled | Prioritizes a playable catalog before expensive enrichment |
| Metadata Enrichment Batch Size | 10 | Items enriched per background batch |
| Progressive Import Batch Size | 200 | Items handed to the fast importer per batch |
| Decypharr API Workers | 4 | Concurrent discovery workers |
| Browser Transcoding | Disabled | Browser-only H.264/AAC fallback |
| Transcode Encoder | Auto-detect | NVIDIA, Intel QSV, AMD/VAAPI or CPU |
| VAAPI / QSV Render Device | `/dev/dri/renderD128` | Intel/AMD render device |
| FFmpeg Path | `/usr/local/bin/ffmpeg` | FFmpeg executable |
| Max Simultaneous Transcodes | 2 | Browser transcode concurrency |

## Preferred Audio Language

**Preferred Audio Language** controls which audio track is selected when a title contains multiple audio streams.

Selection order is:

1. The configured language, when present.
2. The source's existing default audio track, when the configured language is unavailable.
3. The first available audio track.

## Decypharr API Discovery

The plugin uses the Decypharr API rather than directly walking the Decypharr filesystem.

Primary inventory endpoint:

```text
/api/browse/__all__
```

Playback uses:

```text
/api/browse/download/{torrent}/{file}
```

API requests use:

```http
Authorization: Bearer <API_TOKEN>
```

The token is never written into generated `.strm` files.

## Large Libraries and Scanning

The scanner separates work into:

```text
Decypharr inventory
       ↓
Persistent release/file cache
       ↓
Progressive fast import
       ↓
Playable Dispatcharr catalog
       ↓
Metadata batches
       ↓
FFprobe / TMDB / artwork / genres
```

Only one scan may modify the Decypharr VOD catalog at a time. The scanner uses scan locking to prevent overlapping scans.

**Scan Now** remains available for an immediate scan, while the background scanner periodically runs the same import pipeline.

## Media Classification

The plugin supports:

- Movies
- Normal `SxxEyy` TV episodes
- `NxN` episode notation
- Season-only releases
- Multi-file releases
- Blu-ray/M2TS releases
- Obfuscated season-pack members when release-level information identifies the season

The canonical parser uses structural release boundaries rather than globally deleting words from filenames.

## Movies

Movies become native Dispatcharr VOD Movie objects.

Example normalized layout:

```text
library/
└── movies/
    └── Movie Name (Year)/
        └── Movie Name (Year) Quality.strm
```

## TV Shows

TV content becomes native Dispatcharr Series and Episode objects. Episodes retain season and episode numbers.

## TMDB Metadata and Genre Categories

TMDB is optional.

When enabled, TMDB metadata can provide artwork and genres. Existing Dispatcharr VOD categories are reused case-insensitively, and missing categories are created.

A title can belong to multiple categories:

```text
Movie
├── Action
├── Adventure
└── Science Fiction
```

Those categories are **not separate streams**. The title continues to have exactly one canonical VOD relation.

## Synthetic XC Account

Dispatcharr's VOD relation/proxy layer requires a usable XC account. Decypharr VOD therefore maintains a synthetic XC account.

The account remains active and is not converted to STD. Normal provider refresh behavior is intercepted for this plugin-owned account because it does not represent a real upstream XC server.

## Automatic Scanning

**Auto Scan Interval** is backed by a real background worker. The worker periodically runs the same scan pipeline used by manual scanning.

New media can therefore be discovered without pressing **Scan Now**.

Automatic scanning is separate from the synthetic XC provider refresh and does not require `player_api.php`.

## Series Next Episode

The plugin records deterministic next-episode metadata within the current season, including next episode relation ID, UUID, season, and episode number when available.

The final episode of a season is treated as a season boundary rather than automatically advancing into another season.

## Playback

Generated `.strm` files use Dispatcharr's native VOD proxy path:

```text
.strm
  ↓
Dispatcharr native VOD proxy
  ↓
Decypharr VOD relation
  ↓
Authenticated Decypharr download request
  ↓
Media stream
```

The Decypharr token remains server-side.

## Browser Transcoding

Browser transcoding is **off by default**.

Supported encoder paths include:

- NVIDIA NVENC
- Intel Quick Sync (QSV)
- AMD/Intel VAAPI
- CPU/libx264

The plugin cannot grant a Docker container GPU access. Docker Compose must expose the required device/GPU to Dispatcharr.

NVIDIA example:

```yaml
services:
  dispatcharr:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu, video, compute, utility]
```

Intel/AMD example:

```yaml
services:
  dispatcharr:
    devices:
      - /dev/dri:/dev/dri
```

## Repair

The **Repair Integration** action reinstalls the plugin integration hooks, ensures the synthetic Decypharr XC account exists, reinstalls the synthetic XC refresh guard, and restores the expected Dispatcharr integration state.

Repair is safe to run repeatedly.

## Clean + Verify VOD Database

The **Clean VOD Database** action is intentionally destructive to Dispatcharr's VOD records. It removes VOD relations and objects used by the plugin while preserving the synthetic M3U account.

It does **not** delete actual Decypharr media.

For a fresh v1.0.3 test after previous versions have created duplicate genre relations, use **Clean + Verify VOD Database first**, restart Dispatcharr, and then run **Scan Now**.

## Data Ownership and Cleanup

Decypharr remains the source of truth for media files. Dispatcharr stores the normalized VOD presentation and relations required for playback.

Cleaning the Dispatcharr VOD database does not delete media from Decypharr.

## Troubleshooting

### Duplicate genre streams appear

A movie or episode may have multiple TMDB genre categories, but it must have only one canonical VOD relation.

After installing v1.0.3:

1. Run **Clean + Verify VOD Database**.
2. Restart Dispatcharr so the v1.0.3 plugin entry point and relation guard are loaded.
3. Run **Scan Now**.
4. Check the Dispatcharr logs for:

```text
Decypharr VOD v1.0.3 BLOCKED legacy genre relation
```

If that message appears, v1.0.3 has caught an attempted legacy genre relation at the relation-save boundary rather than allowing it to become another playable stream.

### New media does not appear automatically

Check the Auto Scan Interval and plugin background worker status. **Scan Now** can be used to trigger an immediate inventory scan.

### Browser playback fails

Enable Browser Transcoding and select Auto-detect or an available encoder. Verify that the Dispatcharr container has access to the required GPU or `/dev/dri` device.

### Decypharr API fails

Verify the API URL and token. The token must have access to the Decypharr browse and download endpoints used by the plugin.

## License

See the repository license for the applicable project terms.
