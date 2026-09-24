# Decypharr VOD for Dispatcharr

**Version:** 0.4.8
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
* Automatic scanning
* Integration repair
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

| Setting             | Description                                                   |
| ------------------- | ------------------------------------------------------------- |
| Decypharr API URL   | Base URL of the Decypharr API                                 |
| Decypharr API Token | Authentication token used for Decypharr API requests          |
| Normalized Library  | Local directory used for generated `.strm` presentation files |
| TMDB API Key        | Optional TMDB API key                                         |
| TMDB Metadata       | Enables/disables TMDB metadata                                |
| FFprobe Path        | Path to the FFprobe executable                                |
| Auto Scan Interval  | Automatic scan interval in seconds                            |

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

# Version 0.4.8

Version 0.4.8 uses the authenticated Decypharr API as its media discovery source.

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

---

# Design Philosophy

Decypharr owns the media.

Dispatcharr owns the VOD presentation.

Decypharr VOD connects the two wi
