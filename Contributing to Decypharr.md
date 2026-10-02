# Contributing to Decypharr VOD Plugin

**Release line:** v1.0.6

Thank you for your interest in contributing to the Decypharr VOD Plugin for Dispatcharr.

## v1.0.6 identity and playback rules

The plugin requires exactly one canonical Dispatcharr VOD relation for each logical movie, series, and episode.

TMDB genres are metadata/categories only and must never become additional playable VOD relations.

The importer must preserve Decypharr source identity across title and path renames. When an existing plugin-owned relation has the same Decypharr source identity, the importer should reuse the existing Dispatcharr Movie or Series object and update its current representation rather than creating a second object from the renamed filename.

When **Continuous Season Playback** is enabled, episode relations must expose deterministic next-episode metadata from the first episode through the final episode of the same season. Season boundaries must remain explicit; S01E(last) must not silently advance to S02E01.

Generated `.strm` files are plugin-owned presentation files. Source media in Decypharr must never be deleted by rename reconciliation.

## Testing

Test real-world naming variations including apostrophes, curly apostrophes, colons, dashes, underscores, and source title/path renames.

For rename tests verify that:

1. The existing Dispatcharr object is reused.
2. The current title is applied.
3. Only one canonical VOD relation remains.
4. The current generated `.strm` represents the current source.
5. The stale generated `.strm` is removed during normal scan cleanup.
6. TMDB genres remain categories/metadata.

For continuous playback tests verify that enabling the setting produces ordered S01E01 → S01E02 → ... → S01E(last) metadata and that the final episode is marked as the season boundary. Do not assume server-side metadata alone changes the stock Dispatcharr browser player; frontend consumption of the next-episode metadata is required for an automatic client-side transition.

For bug reports include plugin version, Dispatcharr version, Decypharr version when relevant, exact error messages, relevant logs, reproduction steps, expected behavior, and actual behavior. Never include passwords, API keys, authentication tokens, or private URLs.

## Pull Requests

Use a dedicated branch, keep changes focused, test against a working Dispatcharr installation, verify existing behavior, and update documentation when behavior changes.

## Filesystem and database safety

Decypharr source media is user-owned data. Contributions must not delete or rename source media as part of catalog reconciliation.

Database cleanup must be scoped to plugin-owned VOD records and must have clear safeguards.

## Code quality

Avoid unnecessary dependencies and unnecessary changes to Dispatcharr itself. Security, reliability, compatibility, and predictable behavior take priority over unnecessary complexity.

## License

By contributing to this project, you agree that your contributions may be distributed under the project's MIT License.
