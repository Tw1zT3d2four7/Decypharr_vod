# Contributing to Decypharr VOD Plugin

**Release line:** v1.0.5

Thank you for your interest in contributing to the Decypharr VOD Plugin for Dispatcharr.

Contributions, bug reports, testing, and improvements are welcome.

## Before Contributing

Please check the existing GitHub issues and pull requests before opening a new issue or submitting a change.

For bugs, provide enough information to reproduce the problem without exposing private credentials or personal information.

## Reporting Bugs

When reporting a bug, include plugin version, Dispatcharr version, Decypharr version when relevant, Docker deployment information, exact errors, relevant logs, reproduction steps, expected behavior, and actual behavior. Do not include passwords, API keys, authentication tokens, private URLs, or other sensitive information.

## Pull Requests

Before submitting a pull request:

1. Fork the repository.
2. Create a dedicated branch.
3. Make the smallest practical change required.
4. Test against a working Dispatcharr installation.
5. Verify existing functionality continues to work.
6. Update documentation when behavior changes.
7. Update `CHANGELOG.md` when appropriate.
8. Submit a clear pull request description.

## v1.0.5 Identity and VOD Relation Rules

v1.0.5 requires exactly one canonical Dispatcharr VOD relation for each logical movie, series, and episode.

TMDB genres are metadata/categories only. They must not create additional playable VOD relations.

The importer must preserve Decypharr source identity across title and path renames. When an existing plugin-owned relation carries the same Decypharr source identity, the importer should reuse that Dispatcharr Movie or Series object, update its current title/path metadata, and allow stale generated representations to be removed through normal cleanup.

Do not use a newly renamed filename alone as proof that the media is a new object when a stable Decypharr source identity is available.

## Plugin Integrity

Changes should preserve the plugin's self-contained installation model. Users should not be required to manually copy files into the Dispatcharr container, install undocumented dependencies, modify Dispatcharr source code, or apply undocumented host-side patches.

## Filesystem Safety

The Decypharr media tree is user-owned source data. Contributions must avoid unnecessary modification, deletion, renaming, or movement of source media.

Generated `.strm` files are plugin-owned presentation files and may be reconciled when their source media identity changes.

## Database Safety

Changes affecting Dispatcharr's VOD database should be tested carefully. Do not introduce destructive database operations without a clear justification and appropriate safeguards.

## Testing

Testing should cover affected functionality whenever practical.

For scanner changes, test real-world naming variations, including apostrophes, curly apostrophes, colons, dashes, underscores, and title/path renames.

For VOD changes, verify both catalog creation and playback. In particular, test multi-genre titles such as `10,000 BC` and confirm that genres appear as categories/metadata without creating additional VOD streams.

For rename tests, verify that:

* The existing Dispatcharr object is reused.
* The title is updated rather than duplicated.
* Only one canonical VOD relation remains.
* The new generated `.strm` represents the current source.
* The old generated `.strm` is removed after the current scan reconciles it.

## Code Quality

Keep changes focused and readable. Avoid unnecessary dependencies and unnecessary changes to Dispatcharr itself.

Security, reliability, compatibility, and predictable behavior take priority over unnecessary complexity.

## License

By contributing to this project, you agree that your contributions may be distributed under the project's MIT License.
