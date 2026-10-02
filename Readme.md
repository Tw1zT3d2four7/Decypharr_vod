# Decypharr VOD for Dispatcharr

<p align="center"><img src="logo.png" alt="Decypharr VOD" width="240"></p>

**Version:** 1.0.7  
**Author:** Tw1zT3d2four7

Decypharr VOD imports Decypharr media into Dispatcharr as native VOD content.

## v1.0.7

v1.0.7 keeps one canonical VOD relation per logical movie, series, and episode and preserves **source identity across title/path renames**.

It also exposes **Continuous Season Playback** as a real boolean plugin setting. The setting is loaded from `plugin.json` into the runtime `Plugin.fields` schema as well, preventing a manifest/runtime schema mismatch from hiding the control in Dispatcharr versions that render settings from the active Plugin class.

When Decypharr changes a title such as:

```text
Movie: The Thing
```

to:

```text
Movie - The Thing
```

the importer uses the existing Decypharr source identity (`info_hash`, with `file_path` as a secondary identity) to reuse the existing Dispatcharr object rather than treating the renamed title as a new movie or series.

### VOD relation rule

TMDB genres are metadata/categories only. They never become separate playable VOD relations.

`10,000 BC` should have one canonical stream:

```text
decypharr-movie-1458700
```

and not separate genre relations.

## Continuous Season Playback

The plugin UI contains:

```text
Continuous Season Playback: ON / OFF
```

When enabled, the plugin exposes ordered next-episode metadata for:

```text
S01E01 → S01E02 → S01E03 → ... → S01E(last)
```

The last episode of the season is marked as the season final. The feature is deliberately season-scoped; it does not automatically jump from S01E(last) into S02E01.

The setting is defined in `plugin.json` and is also copied to the runtime Plugin class by `plugin.py`, so both manifest-driven and runtime-schema-driven Dispatcharr plugin UIs receive the control.

**Important:** Dispatcharr's current native frontend player must consume the next-episode metadata for a browser/player-level automatic transition. The plugin cannot force a stock Dispatcharr frontend `<video>` element to change its source when playback ends solely from server-side plugin code. The plugin therefore provides the ordered relationship and setting without pretending that server-side metadata alone changes the native frontend behavior.

## Scan flow

```text
Decypharr API
     ↓
Discover media
     ↓
Resolve stable source identity
     ↓
Reuse existing Dispatcharr object when available
     ↓
Update current title/path metadata
     ↓
Create/update one canonical VOD relation
     ↓
Write current .strm representation
     ↓
Remove stale generated representations
     ↓
TMDB / FFprobe enrichment
     ↓
Link next episode metadata when Continuous Season Playback is enabled
```

This is intentionally identity-driven rather than trying to discover the old movie by searching for the newly renamed filename after the rename has already occurred.

## Requirements

- Dispatcharr
- Decypharr
- Decypharr API access and token
- FFprobe
- Optional TMDB API key
- Optional FFmpeg/GPU access for browser transcoding

## Installation

Install through the normal Dispatcharr plugin installation process.

The plugin installs under:

```text
/data/plugins/decypharr_vod/
```

Primary entry point:

```text
plugin.py
```

Restart or reload Dispatcharr's plugin discovery after installing or upgrading so the v1.0.7 runtime and settings schema are loaded.

## Configuration

The plugin supports Decypharr API settings, normalized library location, TMDB metadata, FFprobe, preferred audio language, automatic scanning, progressive importing, API workers, **Continuous Season Playback**, browser transcoding, hardware encoder selection, render-device selection, FFmpeg, and maximum simultaneous transcodes.

## Decypharr API

Primary inventory endpoint:

```text
/api/browse/__all__
```

Playback uses the authenticated Decypharr download API. The API token is never written into generated `.strm` files.

## Automatic scanning

The background scanner runs at the configured interval and uses the same identity-aware import path as **Scan Now**. Scan locking prevents overlapping imports.

## TMDB categories

Multiple TMDB genres may be attached to the same movie or series. Categories are metadata and do not create additional streams.

## Clean + Verify VOD Database

This action removes plugin VOD records and verifies the VOD database state without deleting Decypharr source media.

For testing after previous plugin versions have created duplicates:

```text
Clean + Verify VOD Database
        ↓
Restart Dispatcharr
        ↓
Scan Now
        ↓
Allow the complete scan/background enrichment to finish
```

## Testing renamed media

Verify that:

1. The existing Dispatcharr Movie/Series object is reused.
2. Its displayed title follows the current Decypharr title.
3. Only one canonical VOD relation remains.
4. The current `.strm` points to the current relation.
5. The old generated `.strm` disappears after scan reconciliation.
6. TMDB genres remain categories/metadata instead of becoming streams.

## Browser transcoding

Browser transcoding is off by default. Supported paths include NVIDIA NVENC, Intel QSV, AMD/Intel VAAPI, and CPU/libx264.

## License

See the repository license for the applicable project terms.
