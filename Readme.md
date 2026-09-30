# Decypharr VOD for Dispatcharr

**Version:** 1.0.0
**Author:** Tw1zT3d2four7

Decypharr VOD is a Dispatcharr plugin that imports media managed by **Decypharr** into Dispatcharr as native VOD content.

Instead of exposing the Decypharr filesystem directly to Dispatcharr, the plugin communicates with Decypharr through its authenticated API, creates a normalized local presentation library, and provides playback through an authenticated proxy route.

---

## What It Does

Decypharr VOD provides:

* Authenticated Decypharr API discovery
* Movie and TV episode detection
* Native Dispatcharr VOD movie/series/episode objects
* Normalized `.strm` presentation files
* Decypharr API-backed playback
* HTTP Range request support for playback
* FFprobe technical metadata
* Optional TMDB metadata
* Automatic library cleanup
* Duplicate protection
* Reliable background automatic scanning
* TMDB genre-based VOD categories
* Active synthetic XC account with guarded refresh
* Integration repair
* Optional browser transcoding (NVIDIA, Intel, AMD, or CPU)
* No Emby dependency

The plugin is designed so Dispatcharr does **not** need direct access to the Decypharr storage implementation.

---

# Architecture

The media flow is:

```text
                    ┌──────────────────────┐
                    │      Decypharr       │
                    │                      │
                    │  /api/browse/...     │
                    │  /api/browse/        │
                    │      download/...   │
                    └──────────┬───────────┘
                               │
                         Authenticated API
                               │
                               ▼
                    ┌──────────────────────┐
                    │    Decypharr VOD     │
                    │      Plugin          │
                    │                      │
                    │  Discover / Parse    │
                    │  Metadata / Normalize│
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
                    │  Authenticated Range │
                    │       Streaming      │
                    └──────────────────────┘
```

The `.strm` presentation does **not** contain the Decypharr API token.

The plugin keeps authentication on the server side and adds the required authorization when it communicates with Decypharr.

---

# Requirements

## Required

* Dispatcharr
* Decypharr
* Decypharr API access
* A Decypharr API token
* FFprobe

## Optional

* TMDB API key

TMDB metadata can be disabled if it is not required.

---

# Installation

Install the plugin using the normal Dispatcharr plugin installation process.

The plugin directory is:

```text
data/plugins/decypharr_vod/
```

The primary plugin file is:

```text
plugin.py
```

After installation, restart Dispatcharr if required by the plugin manager.

---

# Configuration

The plugin provides the following settings.

| Setting | Description |
| --- | --- |
| Decypharr API URL | Base URL of the Decypharr API |
| Decypharr API Token | Authentication token used for Decypharr API requests |
| Normalized Library | Local directory used for generated `.strm` presentation files |
| TMDB API Key | Optional TMDB API key |
| TMDB Metadata | Enables/disables TMDB metadata |
| FFprobe Path | Path to the FFprobe executable |
| Auto Scan Interval | Automatic scan interval in seconds |
| Browser Transcoding | Enables H.264/AAC transcoding for the Dispatcharr web player (off by default) |
| Transcode Encoder | Auto-detect, NVIDIA, Intel (QSV), AMD/Intel (VAAPI), or CPU only |
| VAAPI / QSV Device | Render device used by Intel QSV and VAAPI (default `/dev/dri/renderD128`) |
| FFmpeg Path | Path to the FFmpeg executable (default `/usr/local/bin/ffmpeg`) |
| Max Simultaneous Transcodes | Upper limit on concurrent browser transcodes (default 2) |

Example:

```text
Decypharr API URL:
http://192.168.1.11:8282

Normalized Library:
/data/plugins/decypharr_vod/library
```

The exact paths will depend on the Dispatcharr container configuration.

---

# Decypharr API

The plugin uses Decypharr's API rather than directly walking the Decypharr filesystem.

The primary inventory endpoint is:

```text
/api/browse/__all__
```

Release directories are then queried using their Decypharr browse path.

For playback, the plugin uses:

```text
/api/browse/download/{torrent}/{file}
```

The important distinction is that the Decypharr **browse path** is used to locate the release.

The Decypharr torrent identifier is used by the **download endpoint**.

The plugin does not assume that an `info_hash` is itself a valid browse path.

---

# Authentication

Decypharr API requests use:

```http
Authorization: Bearer <API_TOKEN>
```

The API token is stored in the plugin configuration.

The token is **never written into generated `.strm` files**.

This is intentional.

A generated `.strm` file represents the media location handled by the Dispatcharr plugin. Authentication remains inside the plugin's server-side API/proxy implementation.

---

# Media Discovery

The plugin discovers video files from Decypharr's API inventory.

Supported video formats are determined by the plugin's configured video-extension list.

For each Decypharr release, the plugin determines whether the content represents:

* A movie
* A TV episode
* A multi-file release
* A Blu-ray structure

The plugin then converts the discovered source into a normalized Dispatcharr representation.

---

# Movies

Movies are imported as native Dispatcharr VOD movies.

The plugin creates a normalized structure similar to:

```text
library/
└── movies/
    └── Movie Name (Year)/
        └── Movie Name (Year) Quality.strm
```

The `.strm` file points to the plugin's playback route rather than exposing the Decypharr API token.

---

# TV Shows

TV content is imported as native Dispatcharr series and episode objects.

Episodes are associated with their detected:

```text
Season
Episode Number
```

The plugin also marks imported series as having locally fetched episode details so Dispatcharr does not attempt to retrieve the episodes through an unrelated synthetic provider.

---

# Blu-ray Handling

The plugin supports Blu-ray-style releases containing M2TS streams.

Blu-ray releases are treated as a logical media release rather than treating every `.m2ts` file as an independent movie.

For TV Blu-ray releases, numbered usable streams can be mapped sequentially to episode numbers.

The plugin also preserves explicitly episode-labelled files when present.

Small menu, sample, and unusable streams are excluded from the primary media selection.

---

# Duplicate Handling

The plugin performs logical deduplication before updating Dispatcharr.

When multiple physical source files resolve to the same logical movie or episode, the plugin selects the largest usable source.

This prevents:

* Duplicate Dispatcharr relations
* Duplicate streams
* Scan-order-dependent source selection
* Multiple representations of the same logical episode

Canonical stream identities are used for movies and episodes so repeated scans do not continuously create duplicates.

---

# Metadata

## FFprobe

FFprobe is used to obtain technical media information such as:

* Video codec
* Audio codec
* Resolution
* Frame rate
* Bitrate
* Container information
* Audio channels

FFprobe can operate against the Decypharr API-backed media source using authenticated requests.

Configure the FFprobe executable using:

```text
FFprobe Path
```

Default:

```text
/usr/local/bin/ffprobe
```

---

## TMDB

TMDB metadata is optional.

When enabled, the plugin can use the configured TMDB API key to obtain metadata for imported movies and series.

TMDB support can be disabled using:

```text
TMDB Metadata
```

---

# Normalized Library

The normalized library is a presentation layer for Dispatcharr.

It does **not** replace Decypharr's storage.

For example:

```text
/data/plugins/decypharr_vod/library/
```

may contain generated `.strm` files while the actual media remains managed by Decypharr.

This separation allows Dispatcharr to work with a predictable local library representation without requiring Dispatcharr to understand Decypharr's underlying storage layout.

---

# Playback

Playback is handled through the plugin's local integration route.

The playback path is conceptually:

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

The proxy supports HTTP Range requests and forwards relevant content headers so clients can perform normal media seeking and streaming.

---

# Browser Transcoding

Dispatcharr's web player is a browser `<video>` element. Browsers cannot reliably
play HEVC, HDR10, Dolby audio, or MKV remuxes, so those titles may start and stop
within seconds in the web player even though the stream itself is healthy.

When **Browser Transcoding** is enabled, the plugin checks each Decypharr file the
Dispatcharr web player opens and transcodes it to **H.264 / AAC** live **only if the
browser cannot play it as-is**. HDR10 sources are tone-mapped to SDR. Every other
client, including VLC, Emby, Jellyfin, Kodi, and TiviMate, is served the original file
untouched.

The feature is **off by default**. When it is off, nothing changes.

## When it activates

Even with the setting enabled, a file is transcoded only when the web player asks for
it and the file is one of these:

* Video other than H.264 (8-bit), VP8, VP9, or AV1, for example HEVC/x265
* HDR10 or HLG video, or 10-bit / non-4:2:0 H.264
* Audio other than AAC, MP3, Opus, Vorbis, or FLAC, for example AC-3, E-AC-3, DTS, TrueHD
* A container the browser cannot open, such as MPEG-TS or AVI
* An MKV file in a browser other than Chrome, Edge, or another Chromium browser

H.264/AAC files in MP4, and H.264/AAC MKV files in Chromium browsers, play directly
with no transcoding. The result of the check is cached for ten minutes per title.

## How the encoder is chosen

With **Transcode Encoder** set to *Auto-detect*, the plugin runs a one-second test
encode with each encoder and uses the first one that works:

1. NVIDIA (NVENC)
2. Intel Quick Sync (QSV)
3. AMD / Intel (VAAPI)
4. CPU (libx264)

A listed encoder is not enough; the test proves the container can actually use the
GPU. You can also pick a specific encoder. If the chosen encoder does not work, the
plugin logs the reason and falls back to the CPU. Use the **Test Transcoding**
action to see the result for each encoder on your system.

## GPU access in Docker

The plugin cannot give a container access to a GPU. Add it to the Dispatcharr
container in your compose file.

NVIDIA (requires the NVIDIA Container Toolkit):

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

Intel or AMD:

```yaml
services:
  dispatcharr:
    devices:
      - /dev/dri:/dev/dri
```

Depending on your host, the container user may also need the `video` or `render`
group (`group_add`). AMD GPUs use the VAAPI option.

## Behavior and limits

* Playback starts from the beginning. Seeking is limited to what the browser has
  already buffered, and duration may not be shown.
* Audio is downmixed to stereo AAC. Subtitles are not included.
* Sources larger than 1080p are scaled down to 1080p.
* **Max Simultaneous Transcodes** protects the server. Extra browser requests
  receive a "capacity reached" response until one finishes. GPU encoders may also
  have their own session limits.
* Browsers open several connections for one file (an initial burst, and again on
  seek). The newest request for a title from a client replaces that client's older
  transcode, so they never stack up.
* If anything in the transcode path fails, playback falls back to the original
  file.
* The Decypharr API token is never placed on a command line. The transcoder reads
  the media through Dispatcharr's own VOD endpoint.

## Testing status

The NVIDIA encode path was benchmarked with FFmpeg on an NVIDIA GTX 960, and the
CPU path and routing logic were tested with real FFmpeg. The Intel QSV and AMD
VAAPI command lines follow standard FFmpeg usage but have not been tested on
hardware. If they fail on your system, the plugin falls back to the CPU.
Reports are welcome.

---

# Scanning

The plugin provides a:

```text
Scan Now
```

action.

A scan:

1. Connects to Decypharr
2. Retrieves the current media inventory
3. Resolves release paths
4. Retrieves release contents
5. Identifies movies and episodes
6. Deduplicates logical media
7. Generates/updates normalized `.strm` files
8. Updates Dispatcharr VOD relations
9. Updates technical metadata
10. Optionally updates TMDB metadata
11. Removes stale normalized files
12. Removes stale plugin-owned relations
13. Removes orphaned plugin-owned objects

A scan does not delete the underlying Decypharr media.

---

# Automatic Scanning

The plugin supports automatic scanning through:

```text
Auto Scan Interval (seconds)
```

The default interval is:

```text
60
```

Only one scan is allowed to run at a time.

This prevents overlapping scans from modifying the same Dispatcharr objects simultaneously.

---

# Repair Integration

The plugin provides:

```text
Repair
```

The repair action reinstalls the plugin's integration hooks and playback route and ensures the synthetic Decypharr account exists.

Use this if the Dispatcharr integration has been disrupted without needing to rebuild the plugin configuration.

---

# Safety and Data Ownership

Decypharr remains the source of truth for the actual media.

The plugin owns its normalized Dispatcharr representation.

The plugin's cleanup operations are limited to plugin-owned normalized files, relations, and objects.

It does **not** delete the underlying Decypharr media when performing a normal scan.

---

# Troubleshooting

## "Decypharr Root not found"

Older versions of the plugin relied on direct filesystem scanning.

Current versions use the Decypharr API.

If the plugin reports a filesystem-root error, verify that the installed plugin version is current and that the API-based version of the plugin is actually loaded.

---

## No media discovered

Verify:

1. Decypharr is running.
2. The API URL is correct.
3. The API token is valid.
4. `/api/browse/__all__` returns releases.
5. Releases contain supported video files.
6. The plugin scan completes without errors.

---

## Playback fails

Verify:

1. Decypharr API access works.
2. The API token is configured.
3. The generated `.strm` file does not contain an invalid/stale URL.
4. The Decypharr download endpoint is reachable from Dispatcharr.
5. The media file is available in Decypharr.
6. HTTP Range requests are being handled correctly.

---

## Duplicate movies or episodes

Run:

```text
Repair
```

followed by:

```text
Scan Now
```

The plugin uses canonical logical identities and deduplication to prevent repeated relations.

---



# Version 1.0.0 Changes

## TMDB Genre Categories

When TMDB metadata is enabled, every genre returned for a movie or series is converted
into a Dispatcharr VOD category. Categories are created automatically when first
encountered and reused on later scans.

A movie or series with multiple TMDB genres is linked to each matching category without
creating duplicate Movie or Series objects. The old generic Decypharr Movies and
Decypharr TV categories are no longer used by v1.0.0.

## Synthetic XC Account

The synthetic XC account remains **active** because Dispatcharr's native VOD relation
and proxy layer requires a usable XC account. It is still not a real upstream provider.

v1.0.0 installs guards around Dispatcharr's normal XC refresh tasks. A user Refresh on
this synthetic account is intercepted and returns a successful no-op instead of trying
to contact the placeholder local server. The plugin's Scan Now and background scanner
remain responsible for the VOD inventory.

## Reliable Automatic Scanning

The Auto Scan Interval is backed by a real background worker inside Dispatcharr.

The worker checks the Decypharr API inventory at the configured interval and calculates
a stable inventory signature. If nothing changed, the import pipeline is skipped. When
media is added, removed, or changed, the same processing pipeline used by Scan Now runs
automatically.

Only one scan may run at a time, including across separate Dispatcharr workers.
**Scan Now** remains available and forces an immediate scan.

This means newly added Decypharr media no longer requires a manual Scan Now to enter the
Dispatcharr VOD collection, provided the background worker is running.

# Version 1.0.0

Version 1.0.0 is the current release line.

Key characteristics:

* API-based media discovery
* Decypharr authentication
* Browse-by-path release resolution
* API-backed playback
* Server-side authentication
* Token-free `.strm` presentation
* Native Dispatcharr VOD integration
* Movie and TV support
* Blu-ray/M2TS handling
* Logical deduplication
* FFprobe metadata
* Optional TMDB metadata
* Normalized library cleanup
* Scan locking
* Repair action
* Optional browser transcoding with hardware auto-detection

---

# Design Philosophy

Decypharr owns the media.

Dispatcharr owns the VOD presentation.

Decypharr VOD connects the two through a normalized, API-backed presentation layer.


Version 1.0.0 is the current release line.

Key characteristics:

* API-based media discovery
* Decypharr authentication
* Browse-by-path release resolution
* API-backed playback
* Server-side authentication
* Token-free `.strm` presentation
* Native Dispatcharr VOD integration
* Movie and TV support
* Blu-ray/M2TS handling
* Logical deduplication
* FFprobe metadata
* Optional TMDB metadata
* Normalized library cleanup
* Scan locking
* Repair action
* Optional browser transcoding with hardware auto-detection

---

# Design Philosophy

Decypharr owns the media.

Dispatcharr owns the VOD presentation.

Decypharr VOD connects the two through a normalized, API-backed presentation layer.
