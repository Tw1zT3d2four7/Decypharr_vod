# Contributing to Decypharr VOD Plugin

Thank you for your interest in contributing to the Decypharr VOD Plugin for Dispatcharr.

Contributions, bug reports, testing, and improvements are welcome.

## Before Contributing

Please check the existing GitHub issues and pull requests before opening a new issue or submitting a change.

For bugs, provide enough information to reproduce the problem without exposing private credentials or personal information.

## Reporting Bugs

When reporting a bug, include:

* Plugin version
* Dispatcharr version
* Decypharr version, when relevant
* Docker deployment information
* Exact error message
* Relevant Dispatcharr log output
* Steps required to reproduce the problem
* Expected behavior
* Actual behavior

Do not include passwords, API keys, authentication tokens, private URLs, or other sensitive information.

## Feature Requests

Feature requests are welcome.

Please describe:

1. The problem the feature would solve.
2. How you expect the feature to work.
3. Any relevant Dispatcharr or Decypharr behavior.
4. Potential compatibility considerations.

## Pull Requests

Before submitting a pull request:

1. Fork the repository.
2. Create a dedicated branch for your change.
3. Make the smallest practical change required.
4. Test the change against a working Dispatcharr installation.
5. Verify that existing functionality continues to work.
6. Update documentation when behavior changes.
7. Update `CHANGELOG.md` when appropriate.
8. Submit the pull request with a clear description.

## Plugin Integrity

Changes should preserve the plugin's self-contained installation model.

Users should not be required to manually:

* Copy files into the Dispatcharr container.
* Install undocumented dependencies.
* Create required directories.
* Modify Dispatcharr source code.
* Apply undocumented host-side patches.

If a change introduces a new requirement, document it clearly and explain why it is necessary.

## Filesystem Safety

The plugin interacts with user media filesystems.

Contributions must avoid unnecessary modification, deletion, renaming, or movement of user media.

The Decypharr media tree should be treated as user-owned source data.

## Database Safety

Changes affecting Dispatcharr's VOD database should be tested carefully.

Do not introduce destructive database operations without a clear justification and appropriate safeguards.

## Testing

Testing should cover the affected functionality whenever practical.

For scanner changes, test with media containing a variety of real-world naming conventions rather than only perfectly formatted filenames.

For filesystem changes, test both:

* Fresh plugin installation
* Existing plugin installation

For VOD changes, verify both catalog creation and playback.

## Code Quality

Keep changes focused and readable.

Avoid unnecessary dependencies and unnecessary changes to Dispatcharr itself.

Security, reliability, compatibility, and predictable behavior take priority over unnecessary complexity.

## License

By contributing to this project, you agree that your contributions may be distributed under the project's MIT License.

