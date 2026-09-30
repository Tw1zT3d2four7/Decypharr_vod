# Decypharr VOD for Dispatcharr

<p align="center">
  <img src="logo.png" alt="Decypharr VOD" width="240">
</p>

**Version:** 1.0.0
**Author:** Tw1zT3d2four7

Decypharr VOD imports media managed by **Decypharr** into Dispatcharr as native VOD content.

The plugin uses Decypharr's authenticated API for discovery and playback, creates a normalized local `.strm` presentation library, and keeps the Decypharr API token server-side.

---

## v1.0.0 Feature Set

- Authenticated Decypharr API discovery through `/api/browse/__all__`
- Persistent Decypharr inventory caching
- Parallel child-release discovery with configurable API workers
- Movie, TV episode, season-pack, multi-file and Blu-ray handling
- Canonical Arr-style title parsing
- Native Dispatcharr VOD movie, series and episode objects
- Normalized `.strm` presentation files
- Decypharr API-backed playback with HTTP Range support
- FFprobe technical metadata
- Optional TMDB metadata, artwork and genre matching
- TMDB genre-based Dispatcharr VOD categories
- Multiple TMDB categories per movie or series
- Category/relation reuse to prevent duplicate genre records
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

The plugin does **not** require Dispatcharr to directly access Decypharr's storage layout.

---

# Architecture

```text
                    ┌──────────────────────┐
                    │      Decypharr       │
                    │  /api/browse/...     │
                    └──────────┬───────────┘
                               │
                         Authenticated API
                               │
                               ▼
                    ┌──────────────────────┐
                    │    Decypharr VOD     │
                    │       Plugin         │
                    │ Discover / Normalize │
                    │ Metadata / Playback  │
                    └──────────┬───────────┘
                               │
                         .strm + metadata
                               │
                               ▼
                    ┌──────────────────────┐
                    │      Dispatcharr     │
                    │      Native VOD      │
                    └──────────┬───────────┘
                               │
                            Playback
                               │
                               ▼
                    ┌──────────────────────┐
                    │ Decypharr API Proxy  │
                    │ Authenticated Range  │
                    │       Streaming      │
                    └──────────────────────┘
```

Generated `.strm` files do **not** contain the Decypharr API token.

---

# Requirements

### Required

- Dispatcharr
- Decypharr
- Decypharr API access
- Decypharr API token
- FFprobe

### Optional

- TMDB API key
- FFmpeg and GPU access when browser transcoding is enabled

---

# Installation

Install the plugin using the normal Dispatcharr plugin installation process.

The plugin is installed under:

```text
/data/plugins/decypharr_vod/
```

The primary plugin file is:

```text
plugin.py
```

After installation, restart Dispatcharr if required by the plugin manager.

---

# Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| Decypharr API URL | `http://192.168.1.11:8282` | Decypharr API base URL |
| Decypharr API Token | — | Decypharr authentication token |
| Normalized Library | `/data/plugins/decypharr_vod/library` | Generated `.strm` presentation library |
| TMDB API Key | — | Optional TMDB API key |
| TMDB Metadata | Enabled | Enables TMDB metadata and genre categories |
| FFprobe Path | `/usr/local/bin/ffprobe` | FFprobe executable |
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

For a typical installation:

```text
Decypharr API URL:
http://192.168.1.11:8282

Normalized Library:
/data/plugins/decypharr_vod/library
```

Paths depend on the Dispatcharr container configuration.

---

# Decypharr API Discovery

The plugin uses the Decypharr API rather than directly walking the Decypharr filesystem.

Primary inventory endpoint:

```text
/api/browse/__all__
```

Child release inventories are obtained from their Decypharr browse paths.

Playback uses:

```text
/api/browse/download/{torrent}/{file}
```

The plugin does not assume that an `info_hash` by itself is a valid browse path.

API requests use:

```http
Authorization: Bearer <API_TOKEN>
```

The token is never written into generated `.strm` files.

---

# Large Usenet-Style Libraries

v1.0.0 is intended for libraries much larger than a normal IPTV/VOD collection, including large Usenet-style libraries containing many thousands of releases and video files.

The scanner therefore separates work into several stages:

```text
Decypharr inventory
       ↓
Persistent release/file cache
       ↓
Progressive fast import
       ↓
Playable Dispatcharr catalog
       ↓
Small metadata batches
       ↓
FFprobe / TMDB / artwork / genres
```

### Persistent inventory cache

The plugin caches release child-file inventories. When Decypharr reports an unchanged release, its previously discovered child inventory can be reused instead of requesting the complete child listing again.

### Parallel discovery

**Decypharr API Workers** controls concurrent child-release API requests. The default is 4. Increase it only when the Decypharr and Dispatcharr hosts remain responsive during scanning and playback.

### Progressive import

**Progressive Import Batch Size** controls how many discovered objects are handed to the fast importer at a time. The default is 200.

The purpose is to avoid the old behavior where the user had to wait for the entire library to finish before useful VOD entries appeared.

### Fast initial import

**Fast Initial Scan** is enabled by default. The fast importer creates provisional playable VOD entries before the expensive FFprobe/TMDB enrichment pass completes.

This is specifically intended to allow playback while a large initial scan is still running.

### Metadata batching

**Metadata Enrichment Batch Size** defaults to 10. FFprobe and TMDB operations are deliberately separated from the initial catalog-building pass so a huge library does not create one enormous metadata operation.

### Scan locking

Only one scan may modify the Decypharr VOD catalog at a time. Both an in-process lock and a process lock are used to prevent overlapping scans from corrupting or competing over the same VOD relations.

### Important classification rule

Fast import must never turn an unresolved TV season-pack into a movie merely because TMDB or FFprobe has not run yet.

Season-pack members are grouped by release and given deterministic episode identities when the release contains enough information to establish a season. Unresolved TV-looking `season_file` items are deferred rather than intentionally converted to Movies.

---

# Media Classification

The plugin supports:

- Movies
- Normal `SxxEyy` TV episodes
- `NxN` episode notation
- Season-only releases
- Multi-file releases
- Blu-ray/M2TS releases
- Obfuscated season-pack members when release-level information identifies the season

The canonical parser is designed around structural boundaries instead of globally deleting words from filenames.

Technical release tokens such as `1080p`, `WEB-DL`, `BluRay`, `HEVC`, `AAC`, `HDR`, `Atmos`, `Netflix`, and similar tokens are treated as release boundaries rather than blindly stripped from arbitrary title text.

---

# Movies

Movies become native Dispatcharr VOD Movie objects.

Example normalized layout:

```text
library/
└── movies/
    └── Movie Name (Year)/
        └── Movie Name (Year) Quality.strm
```

The `.strm` points to Dispatcharr's native VOD proxy path rather than exposing the Decypharr credential.

---

# TV Shows

TV content becomes native Dispatcharr Series and Episode objects.

Episodes retain season and episode numbers. Episode metadata is fetched locally by the plugin when available so the synthetic XC provider is not required to retrieve episode information.

---

# Blu-ray Handling

Blu-ray releases containing M2TS streams are treated as logical releases instead of treating every internal transport stream as a separate movie.

Internal stream names such as numeric M2TS members and `BDMVSTREAM...` entries are not used as movie titles.

For TV Blu-ray releases, usable numbered streams can be assigned deterministic episode order. Small menu/sample/trailer streams are filtered before expensive probing when possible.

---

# Duplicate Protection

Logical deduplication occurs before Dispatcharr is updated.

Canonical movie and episode stream IDs prevent repeated scans from creating a new relation for the same logical content.

Existing TMDB genre categories and plugin-owned relations are reused rather than recreated on every scan.

---

# TMDB Metadata and Genre Categories

TMDB is optional.

When TMDB metadata is enabled, the plugin can obtain metadata for movies and series, including artwork and genres.

For every TMDB genre:

1. Search for an existing Dispatcharr VOD category using a case-insensitive name match.
2. Create the category if it does not exist.
3. Reuse the category on later scans.
4. Attach the title to every applicable genre category.

A movie or series can therefore belong to multiple categories:

```text
Movie
├── Action
├── Adventure
└── Science Fiction
```

The plugin does not depend on the synthetic XC account's `player_api.php` for genre/category discovery.

The old generic `Decypharr Movies` / `Decypharr TV` classification is not the source of TMDB genre categories in v1.0.0.

---

# Synthetic XC Account

Dispatcharr's VOD relation/proxy layer requires a usable XC account. Decypharr VOD therefore maintains a synthetic XC account.

The account remains **active**.

It is deliberately **not disabled** and is not converted to STD.

Normal provider refresh behavior is intercepted for the plugin-owned synthetic account because the account does not represent a real upstream XC server.

A protected refresh returns a successful no-op instead of attempting to connect to a placeholder address such as `127.0.0.1:80`.

The refresh guard is installed during plugin load and Repair and is idempotent.

The plugin's Scan Now and automatic scanner remain responsible for the actual Decypharr inventory.

---

# Automatic Scanning

**Auto Scan Interval** is backed by a real background worker.

The worker periodically runs the same scan pipeline used by manual scanning. It uses the persistent inventory signature to determine whether work is necessary and uses the scan lock to prevent overlapping scans.

New media can therefore be discovered without pressing **Scan Now**.

**Scan Now** remains available for an immediate scan.

Automatic scanning is separate from the synthetic XC provider refresh and does not require `player_api.php`.

---

# Series Next Episode

v1.0.0 records deterministic next-episode metadata on imported episode relations.

For each episode, the plugin can record:

- Next episode relation ID
- Next episode UUID
- Next season
- Next episode number
- Whether the current episode is the final episode of the season

Automatic next-episode metadata is deliberately limited to the **same season**. The final episode of a season is marked as a season boundary instead of automatically advancing into another season.

This supplies the backend information required for a player to implement:

```text
Episode finishes
      ↓
Another episode in this season?
   ┌──┴──┐
  YES    NO
   ↓      ↓
Next     Stop at
episode  season end
```

The plugin does not claim to replace Dispatcharr's frontend player code. The stored relation metadata is the backend side of the feature.

---

# Playback

Generated `.strm` files use Dispatcharr's native VOD proxy path.

Playback flow:

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

HTTP Range requests and relevant upstream headers are forwarded so clients can seek and stream normally.

The Decypharr token remains server-side.

---

# Browser Transcoding

Browser transcoding is **off by default**.

When enabled, the plugin can convert browser-incompatible media to H.264/AAC. This is intended for the Dispatcharr web player and does not force other clients such as VLC, Emby, Jellyfin, TiviMate or Kodi through the transcoder.

Supported encoder paths include:

- NVIDIA NVENC
- Intel Quick Sync (QSV)
- AMD/Intel VAAPI
- CPU/libx264

The **Transcode Encoder** setting can be left on Auto-detect.

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

---

# Repair

The **Repair Integration** action:

- Reinstalls the plugin's integration hooks.
- Ensures the synthetic Decypharr XC account exists.
- Reinstalls the synthetic XC refresh guard.
- Restores the plugin's expected Dispatcharr integration state.

Repair is safe to run repeatedly.

---

# Clean + Verify VOD Database

The **Clean VOD Database** action is intentionally destructive to Dispatcharr's VOD records.

It removes the VOD relations and objects used by the plugin, including:

```text
vod_m3uepisoderelation
vod_m3umovierelation
vod_m3useriesrelation
vod_m3uvodcategoryrelation
vod_episode
vod_movie
vod_series
vod_vodcategory
vod_vodlogo
```

M3U account records are preserved, including the synthetic Decypharr account.

The action verifies that the VOD object and relation counts are zero before reporting success. If verification fails, the cleanup reports an error rather than claiming that the database was cleaned.

This action does **not** delete the actual Decypharr media.

---

# Data Ownership and Cleanup

Decypharr remains the source of truth for the underlying media.

The plugin owns its normalized Dispatcharr representation and plugin-generated `.strm` files.

Normal scans clean up stale plugin-owned `.strm` files and relations. They do not delete the underlying Decypharr media.

The Clean + Verify action is separate and explicitly destructive to Dispatcharr's VOD database records.

---

# Troubleshooting

## New media is not visible immediately

Wait for the configured Auto Scan Interval or use **Scan Now**.

If the plugin is actively scanning, playback should still use the existing VOD catalog. The scan lock prevents a second scan from competing with the active scan.

## A series episode is classified as a movie

Check the release name and whether it contains an identifiable `SxxEyy`, `NxN`, season marker, or release-level season identity. v1.0.0 intentionally avoids converting unresolved `season_file` content into Movies during the fast pass.

## Synthetic XC Refresh reports a connection error

Run **Repair Integration**. The plugin installs an idempotent refresh guard for the plugin-owned synthetic account. The account must remain active.

## TMDB categories are missing

Verify that **TMDB Metadata** is enabled and that a valid TMDB API key is configured. TMDB genres are only available when TMDB metadata is enabled and the title is successfully matched.

## Browser playback needs transcoding

Enable **Browser Transcoding**, confirm FFmpeg is available, and verify that the required GPU/device is exposed to the Dispatcharr container if using hardware encoding.

## Clean + Verify reports an error

Do not continue scanning until the reported verification result is understood. The action is designed to fail rather than falsely claim the VOD database is empty.

---

# Version

This branch is **v1.0.0**.

All versioned plugin metadata and documentation in this branch should remain at **1.0.0 / v1.0.0** until a deliberate version bump is made.

`main` is not modified by this branch.


## Large Usenet-Style Library Scanner

v1.0.0 is designed for very large Usenet-style Decypharr libraries. The initial scan uses a **true progressive, bounded-memory import path**: releases are classified independently, season packs are resolved before their batch is emitted, and playable Dispatcharr VOD records are created without retaining the complete media inventory in Python memory.

The progressive path no longer performs a second whole-library season-pack pass. Each release is normalized before it reaches the import callback, preventing obfuscated season-pack files from being misclassified as movies while also avoiding a full-library deferred list.

Fast-import records are placed into a persistent metadata queue. Expensive FFprobe/TMDB enrichment is processed in small batches after the playable catalog is available. This keeps the scan responsive and reduces the chance that a large initial scan interferes with active VOD playback.

The persistent Decypharr API inventory cache reuses unchanged release child inventories, and changed releases are fetched concurrently according to the **Decypharr API Workers** setting. The scanner also folds a compact inventory signature instead of retaining the complete media objects solely to calculate a scan signature.

Automatic scanning, manual Scan Now, metadata enrichment, and stale-library cleanup continue to use the same plugin-owned relations and `.strm` presentation library. A scan lock prevents overlapping scans from modifying the VOD database at the same time.
