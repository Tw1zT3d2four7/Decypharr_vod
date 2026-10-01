# Decypharr VOD for Dispatcharr

<p align="center">
  <img src="logo.png" alt="Decypharr VOD" width="240">
</p>

**Version:** 1.0.5  
**Author:** Tw1zT3d2four7

Decypharr VOD imports media managed by **Decypharr** into Dispatcharr as native VOD content.

v1.0.5 enforces one canonical VOD relation per logical movie, series, and episode and preserves the underlying media identity when Decypharr changes a title, punctuation, or generated file/path name.

## v1.0.5 Feature Set

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
- Legacy genre relations are blocked and purged
- **Source-identity continuity across title/path renames**
- Renamed media reuses the existing Dispatcharr object instead of creating a second object
- Generated `.strm` cleanup follows the current source identity after a rename
- Duplicate plugin-owned movie reconciliation after identity matching
- Duplicate protection and stale-library cleanup
- Real background automatic scanning
- Progressive initial importing
- Fast initial import separated from expensive FFprobe/TMDB enrichment
- Metadata enrichment in configurable batches
- Active synthetic XC account required by Dispatcharr VOD relations
- Protected synthetic-account refresh
- Repair action
- Clean + Verify VOD Database action
- Optional browser transcoding using NVIDIA NVENC, Intel QSV, AMD/Intel VAAPI or CPU
- Deterministic next-episode metadata within the current season
- No Emby dependency

## Canonical identity and renames

v1.0.5 treats the Decypharr source identity as the primary continuity key during an import. When an existing plugin-owned relation has the same Decypharr `info_hash`, the importer reuses the associated Dispatcharr Movie or Series object even if the title, punctuation, or generated path has changed.

The flow is:

```text
Existing Decypharr source identity
        ↓
Existing Dispatcharr VOD relation
        ↓
Existing Movie / Series object
        ↓
Update current title + source metadata
        ↓
Create/update current .strm representation
        ↓
Remove stale generated representation
```

This avoids the unsafe pattern of trying to identify the old object from the newly renamed filename after the new object has already been created.

For example:

```text
Old source title:
Movie: The Thing

Renamed source title:
Movie - The Thing
```

The importer keeps the same Dispatcharr object and updates its title instead of creating another Movie object.

TMDB ID remains the strongest metadata identity when available, while Decypharr source identity provides continuity when the source has been renamed.

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

Legacy genre relations are purged and the relation save path is guarded so they cannot be recreated by the plugin's own genre handling.

## Architecture

```text
                    Decypharr
                         │
                  Authenticated API
                         │
                         ▼
                 Decypharr VOD Plugin
                 Discover / Normalize
                 Identity / Metadata
                 Playback / Cleanup
                         │
                    .strm + metadata
                         │
                         ▼
                     Dispatcharr
                     Native VOD
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

Restart Dispatcharr after installation or upgrade so the v1.0.5 runtime is loaded.

## Large Libraries and Scanning

The scanner separates work into:

```text
Decypharr inventory
       ↓
Persistent release/file cache
       ↓
Source identity resolution
       ↓
Progressive fast import
       ↓
Playable Dispatcharr catalog
       ↓
Metadata batches
       ↓
FFprobe / TMDB / artwork / genres
       ↓
Stale representation cleanup
```

Only one scan may modify the Decypharr VOD catalog at a time.

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

## Movies

Movies become native Dispatcharr VOD Movie objects.

Generated `.strm` files are representations of the Decypharr source. The filename can change when the source title changes without requiring a second Movie object.

## TV Shows

TV content becomes native Dispatcharr Series and Episode objects. Episodes retain season and episode numbers.

Series identity uses the existing Dispatcharr/TMDB identity matching plus Decypharr source identity when a previously imported source is renamed.

## TMDB Metadata and Genre Categories

TMDB is optional.

When enabled, TMDB metadata can provide artwork and genres. A title can belong to multiple categories, but those categories are **not separate streams**.

```text
Movie
├── Action
├── Adventure
└── Science Fiction
```

The title continues to have exactly one canonical VOD relation.

## Synthetic XC Account

Dispatcharr's VOD relation/proxy layer requires a usable XC account. Decypharr VOD maintains a synthetic XC account for this purpose.

## Automatic Scanning

**Auto Scan Interval** is backed by a real background worker. New media can be discovered without pressing **Scan Now**.

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

Supported encoder paths include NVIDIA NVENC, Intel Quick Sync, AMD/Intel VAAPI, and CPU/libx264.

## Repair

The **Repair Integration** action reinstalls the plugin integration hooks, ensures the synthetic Decypharr XC account exists, and restores the expected Dispatcharr integration state.

## Clean + Verify VOD Database

The **Clean + Verify VOD Database** action removes plugin VOD records and verifies the resulting database state without deleting actual Decypharr media.

For a clean v1.0.5 test after older versions have produced duplicate objects, use:

```text
Clean + Verify VOD Database
        ↓
Restart Dispatcharr
        ↓
Scan Now
        ↓
Allow the scan/background enrichment to finish
```

This provides a clean baseline for testing the new source-identity continuity logic.

## Troubleshooting

### A renamed movie still appears twice

First verify that both records are plugin-owned Decypharr records. v1.0.5 uses the Decypharr `info_hash` carried by the existing relation to connect the renamed source to its existing Dispatcharr object.

If the old and new records were created by earlier plugin versions but have no retained Decypharr source identity, run **Clean + Verify VOD Database** and perform a fresh scan. This prevents the plugin from guessing that two unrelated movies are the same media.

### Duplicate genre streams appear

A movie or episode may have multiple TMDB genre categories, but it must have only one canonical VOD relation. Run **Clean + Verify VOD Database**, restart Dispatcharr, and scan again if legacy records remain.

### New media does not appear automatically

Check the Auto Scan Interval and plugin background worker status. **Scan Now** can trigger an immediate inventory scan.

### Browser playback fails

Enable Browser Transcoding and verify that Dispatcharr has access to the required GPU or `/dev/dri` device.

### Decypharr API fails

Verify the API URL and token. The token must have access to the Decypharr browse and download endpoints used by the plugin.

## License

See the repository license for the applicable project terms.
