# Decypharr VOD for Dispatcharr

**Version:** 1.0.0  
**Author:** Tw1zT3d2four7

Decypharr VOD imports media managed by **Decypharr** into Dispatcharr as native VOD content.

The plugin uses Decypharr's authenticated API for discovery and playback, creates a normalized local `.strm` presentation library, and keeps the Decypharr API token server-side.

---

## What It Does

- Authenticated Decypharr API discovery
- Movie and TV episode detection
- Native Dispatcharr VOD movie, series, and episode objects
- Normalized `.strm` presentation files
- Decypharr API-backed playback with Range support
- FFprobe technical metadata
- Optional TMDB metadata
- TMDB genre-based VOD categories, including multiple categories per title
- Duplicate protection and stale-library cleanup
- Real background automatic scanning
- Progressive large-library importing
- Active synthetic XC account with protected refresh
- Repair and VOD database cleanup actions
- Optional browser transcoding using NVIDIA, Intel QSV, AMD/VAAPI, or CPU
- No Emby dependency

The plugin does **not** require Dispatcharr to understand or directly access Decypharr's storage layout.

---

# Architecture

```text
                    ┌──────────────────────┐
                    │      Decypharr       │
                    │  /api/browse/...     │
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

Generated `.strm` files do **not** contain the Decypharr API token. Authentication remains inside the plugin.

---

# Requirements

## Required

- Dispatcharr
- Decypharr
- Decypharr API access
- Decypharr API token
- FFprobe

## Optional

- TMDB API key

TMDB metadata can be disabled if it is not required.

---

# Installation

Install the plugin using the normal Dispatcharr plugin installation process.

The installed plugin directory is:

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

| Setting | Default | Description |
| --- | --- | --- |
| Decypharr API URL | `http://192.168.1.11:8282` | Base URL of the Decypharr API |
| Decypharr API Token | — | Authentication token for Decypharr API requests |
| Normalized Library | `/data/plugins/decypharr_vod/library` | Local presentation directory for generated `.strm` files |
| TMDB API Key | — | Optional TMDB API key |
| TMDB Metadata | Enabled | Enables/disables TMDB metadata and genre categories |
| FFprobe Path | `/usr/local/bin/ffprobe` | FFprobe executable |
| Auto Scan Interval | 60 seconds | Interval used by the real background scanner |
| Fast Initial Scan | Enabled | Creates the playable catalog before expensive metadata enrichment |
| Metadata Enrichment Batch Size | 10 | Number of items processed per metadata batch |
| Progressive Import Batch Size | 200 | Number of discovered media items imported per progressive batch |
| Decypharr API Workers | 4 | Concurrent Decypharr API workers used during discovery |
| Browser Transcoding | Disabled | Enables H.264/AAC browser transcoding when the browser cannot play the source |
| Transcode Encoder | Auto-detect | NVIDIA, Intel QSV, AMD/VAAPI, or CPU |
| VAAPI / QSV Device | `/dev/dri/renderD128` | Render device for Intel QSV/VAAPI |
| FFmpeg Path | `/usr/local/bin/ffmpeg` | FFmpeg executable |
| Max Simultaneous Transcodes | 2 | Maximum concurrent browser transcodes |

Example:

```text
Decypharr API URL:
http://192.168.1.11:8282

Normalized Library:
/data/plugins/decypharr_vod/library
```

Paths depend on the Dispatcharr container configuration.

---

# Decypharr API

The plugin uses Decypharr's API rather than directly walking the Decypharr filesystem.

The primary inventory endpoint is:

```text
/api/browse/__all__
```

Release directories are then queried through their Decypharr browse paths.

For playback, the plugin uses the Decypharr download endpoint:

```text
/api/browse/download/{torrent}/{file}
```

The **browse path** identifies the release during discovery. The torrent identifier is used by the download endpoint. The plugin does not assume that an `info_hash` by itself is a valid browse path.

---

# Authentication

Decypharr API requests use:

```http
Authorization: Bearer <API_TOKEN>
```

The token is stored in plugin configuration and is never written into generated `.strm` files.

Authentication stays server-side so the generated library does not expose the Decypharr credential.

---

# Media Discovery

The plugin discovers video files from Decypharr's API inventory.

For each release it determines whether the content represents:

- A movie
- A TV episode
- A multi-file release
- A Blu-ray structure

The discovered source is converted into the normalized Dispatcharr representation.

---

# Movies

Movies are imported as native Dispatcharr VOD movies.

Example normalized layout:

```text
library/
└── movies/
    └── Movie Name (Year)/
        └── Movie Name (Year) Quality.strm
```

The `.strm` file uses the plugin playback route rather than exposing the Decypharr API token.

---

# TV Shows

TV content is imported as native Dispatcharr series and episode objects.

Episodes are associated with detected season and episode numbers. The plugin also records locally fetched episode details so Dispatcharr does not try to obtain them through an unrelated synthetic provider.

---

# Blu-ray Handling

Blu-ray-style releases containing M2TS streams are treated as logical releases rather than treating every M2TS file as an independent movie.

For TV Blu-ray releases, numbered usable streams can be mapped sequentially to episode numbers. Explicitly episode-labelled files are preserved when present.

Small menu, sample, and unusable streams are excluded from primary media selection.

---

# Duplicate Handling

Logical deduplication is performed before Dispatcharr is updated.

When multiple physical source files resolve to the same logical movie or episode, the plugin selects the largest usable source.

Canonical stream identities prevent repeated scans from continuously creating duplicate relations.

---

# Metadata

## FFprobe

FFprobe can provide technical information including:

- Video codec
- Audio codec
- Resolution
- Frame rate
- Bitrate
- Container information
- Audio channels

Configure the executable with **FFprobe Path**.

Default:

```text
/usr/local/bin/ffprobe
```

## TMDB

TMDB metadata is optional.

When enabled, the plugin can obtain metadata for imported movies and series using the configured TMDB API key.

Set **TMDB Metadata** to disabled to turn TMDB enrichment off.

---

# TMDB Genre Categories

v1.0.0 uses TMDB genres as native Dispatcharr VOD categories when TMDB metadata is enabled.

For every genre returned for a movie or series:

1. The plugin looks for an existing Dispatcharr VOD category with that genre name.
2. If it does not exist, the category is created automatically.
3. The movie or series is related to that category.
4. Existing categories and relations are reused on later scans.

A title with multiple TMDB genres can therefore belong to multiple Dispatcharr categories.

Example:

```text
Movie
├── Action
├── Adventure
└── Science Fiction
```

No duplicate category is created simply because another title uses the same genre.

The old generic Decypharr Movies and Decypharr TV category approach is not used for v1.0.0 genre classification.

---

# Normalized Library

The normalized library is a presentation layer for Dispatcharr. It does **not** replace Decypharr's storage.

For example:

```text
/data/plugins/decypharr_vod/library/
```

may contain generated `.strm` files while the actual media remains managed by Decypharr.

---

# Playback

Playback is handled through the plugin's local integration route:

```text
Dispatcharr .strm
       ↓
Decypharr VOD playback route
       ↓
Authenticated Decypharr API request
       ↓
/api/browse/download/{torrent}/{file}
       ↓
Media stream
```

The proxy supports HTTP Range requests and forwards relevant content headers so clients can seek and stream normally.

---

# Synthetic XC Account

The plugin maintains a synthetic XC account because Dispatcharr's native VOD relation and proxy layer requires a usable XC account.

The account remains **active**. It is not a real upstream IPTV provider and is not disabled after import.

The plugin protects this account from normal provider refresh behavior:

- The account remains usable for VOD relations and `.strm` playback.
- A user-initiated Refresh on the synthetic account is intercepted.
- The guarded refresh returns a successful no-op instead of attempting to contact a placeholder server such as `127.0.0.1:80`.
- The plugin's Scan Now and automatic scanner remain responsible for the Decypharr VOD inventory.
- The refresh guard is installed during plugin load and Repair and is idempotent, so repeated plugin loads do not stack duplicate patches.

---

# Scanning

The plugin provides:

```text
Scan Now
```

A normal scan:

1. Connects to Decypharr.
2. Retrieves the current inventory.
3. Resolves release paths and contents.
4. Identifies movies and episodes.
5. Deduplicates logical media.
6. Generates or updates normalized `.strm` files.
7. Updates Dispatcharr VOD objects and relations.
8. Updates metadata when required.
9. Removes stale normalized files.
10. Removes stale plugin-owned relations/objects where appropriate.

A scan does **not** delete the underlying Decypharr media.

## Fast Initial Scan

**Fast Initial Scan** is enabled by default.

The initial import is designed for large libraries. Discovery emits completed media batches progressively instead of waiting for the entire inventory to finish before importing anything.

The fast pass prioritizes creation of the playable VOD catalog and avoids waiting for FFprobe/TMDB enrichment before the catalog can be populated.

## Progressive Import

**Progressive Import Batch Size** controls how many discovered items are handed to the fast importer at a time.

With the default of 200, newly discovered batches can become visible in the VOD collection while the remaining Decypharr inventory is still being discovered.

## Metadata Enrichment

Metadata enrichment is separated from the initial fast import.

**Metadata Enrichment Batch Size** controls how many pending items are processed in one enrichment batch. This limits FFprobe/TMDB activity so a large library does not require one huge metadata operation.

## API Workers

**Decypharr API Workers** controls the concurrent API workers used during Decypharr discovery.

The default is 4.

## Scan Lock

Only one scan may run at a time. The scan lock prevents overlapping scans from modifying the same Dispatcharr objects simultaneously.

---

# Automatic Scanning

**Auto Scan Interval** is backed by a real background worker inside Dispatcharr.

The worker checks the Decypharr inventory at the configured interval.

The scanner tracks the inventory so changes can be detected without requiring the user to press **Scan Now**. New or changed media is sent through the plugin's import/update pipeline, while removed media is handled by the stale-library cleanup logic.

This means newly added Decypharr media can enter the Dispatcharr VOD collection automatically while the plugin is running.

**Scan Now** remains available when an immediate scan is wanted.

Automatic scanning is not the same thing as Dispatcharr's provider refresh. The synthetic XC account is deliberately protected from normal XC refresh behavior.

---

# Series Next-Episode Metadata

v1.0.0 records deterministic next-episode metadata for imported series episodes.

The metadata includes the next episode identity and season/episode information. The next episode is linked only when it belongs to the **same season**.

The final episode of a season is explicitly marked as the season boundary, so the backend does not automatically cross from one season into the next.

This provides the backend information needed for a future/native player workflow such as:

```text
Episode ends
   ↓
Next episode exists in same season?
   ├── Yes → player may offer/continue to next episode
   └── No  → stop at season boundary
```

The plugin currently stores the backend next-episode metadata; it does **not** replace Dispatcharr's React/frontend player bundle or claim to implement the player's automatic-next-episode UI by itself.

---

# Browser Transcoding

Dispatcharr's web player cannot reliably play every HEVC, HDR, Dolby-audio, MPEG-TS, AVI, or MKV combination.

When **Browser Transcoding** is enabled, the plugin can transcode incompatible browser playback to H.264/AAC. HDR sources can be tone-mapped to SDR.

The feature is **off by default**.

Other clients continue to receive the original media through the normal playback route.

## Encoder Selection

With **Transcode Encoder** set to Auto-detect, the plugin tests available encoders and selects a working hardware path when possible, with CPU fallback.

Supported paths include:

- NVIDIA NVENC
- Intel Quick Sync (QSV)
- AMD/Intel VAAPI
- CPU/libx264

Use **Test Transcoding** to check the available encoder path in the running Dispatcharr container.

## Docker GPU Access

The plugin cannot grant GPU access to a container. GPU/device access must be configured in Docker Compose.

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

The container user may also need the appropriate `video`/ `render` group.

---

# Repair

The **Repair** action reinstalls the plugin's integration hooks and playback route and ensures the synthetic Decypharr account exists.

Use Repair if the Dispatcharr integration has been disrupted without rebuilding the plugin configuration.

Repair also reinstalls the synthetic XC refresh guard.

---

# Clean + Verify VOD Database

The **Clean VOD Database** action is a destructive maintenance operation intended for rebuilding the Dispatcharr VOD database before a clean import.

It removes plugin/VOD records from these VOD tables:

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

M3U account records are preserved.

After deletion, the action verifies the VOD object/relation counts.

**This action should only be used when you intentionally want to rebuild the Dispatcharr VOD database. It does not delete the underlying Decypharr media.**

---

# Data Ownership and Cleanup

Decypharr remains the source of truth for the actual media.

The plugin owns its normalized Dispatcharr representation.

Normal scan cleanup is limited to plugin-owned normalized files, relations, and objects. It does **not** delete the underlying Decypharr media.

The **Clean + Verify VOD Database** action is separate and explicitly destructive to Dispatcharr's VOD database records.

---

# Troubleshooting

## "Decypharr Root not found"

Current versions use the Decypharr API rather than direct filesystem scanning.

Verify that the installed plugin version is current and that the API-based plugin is actually loaded.

## No media discovered

Verify:

1. Decypharr is running.
2. The API URL is correct.
3. The API token is valid.
4. `/api/browse/__all__` returns releases.
5. Releases contain supported video files.
6. The plugin scan completes without errors.

## Playback fails

Verify:

1. Decypharr API access works.
2. The API token is configured.
3. The generated `.strm` entry is valid.
4. The Decypharr download endpoint is reachable from Dispatcharr.
5. The media is available in Decypharr.
6. HTTP Range requests are working.

## Duplicate movies or episodes

Use **Repair**, then run **Scan Now**.

If you intentionally want a complete Dispatcharr VOD rebuild, use **Clean + Verify VOD Database**, then run a clean scan.

## Synthetic XC Refresh reports a connection error

Run **Repair** first.

The synthetic account is supposed to remain active, but its normal XC refresh is guarded so it does not attempt to connect to a fake upstream server.

## Browser playback fails

If the source is HEVC/HDR/DTS/TrueHD/MKV or another format the browser cannot play, enable **Browser Transcoding** and run **Test Transcoding**.

---

# v1.0.0 Changes

### TMDB Genre Categories

TMDB genres are converted into Dispatcharr VOD categories. Categories are created on first encounter and reused. A movie or series can have multiple genre-category relations.

### Active Synthetic XC Account

The synthetic XC account remains active for Dispatcharr VOD relations and playback. Refresh is guarded as a successful no-op instead of contacting a placeholder upstream.

### Real Automatic Scanning

Auto Scan Interval now controls a real background worker. Inventory changes can be detected and imported without requiring a manual Scan Now.

### Progressive Large-Library Import

Large libraries can be imported progressively. The playable catalog is prioritized before slower metadata enrichment, with configurable batch sizes and API worker concurrency.

### VOD Database Maintenance

A **Clean + Verify VOD Database** action is available for intentional Dispatcharr VOD database rebuilds.

### Series Metadata

Next-episode metadata is recorded within each season. The final episode is marked as the season boundary.

---

# Design Philosophy

**Decypharr owns the media.**

**Dispatcharr owns the VOD presentation.**

Decypharr VOD connects the two through a normalized, authenticated API-backed presentation layer.

The plugin is designed to keep credentials server-side, avoid unnecessary duplicate provider work, and make large Decypharr libraries usable as native Dispatcharr VOD content.
