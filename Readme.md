# Decypharr VOD Plugin for Dispatcharr

A native Dispatcharr VOD integration for **Decypharr** that scans locally available Decypharr media, organizes it into Dispatcharr's VOD library, and provides a clean interface for browsing and playing movies and TV series.

The plugin is designed to work with Decypharr's filesystem-based media output while allowing Dispatcharr to manage the resulting VOD catalog.

---

## Features

* **Decypharr filesystem integration**

  * Scans media exposed by Decypharr.
  * Supports TV series, seasons, and episodes.
  * Designed around Decypharr's consolidated media tree.

* **Native Dispatcharr VOD integration**

  * Creates and maintains Dispatcharr VOD records.
  * Integrates with Dispatcharr's existing movie, series, season, and episode structure.
  * Uses Dispatcharr's VOD database rather than maintaining a separate catalog.

* **Automatic media directory setup**

  * Creates the plugin's required working directory automatically.
  * No manual `mkdir`, `chmod`, or host-side preparation should be required for normal installation.

* **Filesystem-based media**

  * Designed for media that is already available locally through Decypharr.
  * Does not require downloading or maintaining a second copy of the media.

* **TV series organization**

  * Detects series and episodes from the Decypharr media structure.
  * Associates episodes with their corresponding seasons and series.
  * Designed to accommodate real-world media naming rather than relying on rigid filename assumptions.

* **Self-contained plugin distribution**

  * Distributed as a Dispatcharr-compatible ZIP package.
  * Plugin dependencies and supporting code are included in the distribution.
  * Intended to work immediately after installation without requiring users to manually modify the Dispatcharr container.

---

# Requirements

## Dispatcharr

A working installation of Dispatcharr with VOD functionality enabled.

The plugin is intended for modern Dispatcharr installations and should be installed through Dispatcharr's plugin management interface.

## Decypharr

Decypharr must already be installed and configured.

The plugin expects Decypharr's media filesystem to be available to the Dispatcharr container.

The default Decypharr media root used by this project is:

```text
/mnt/decypharr/__all__
```

Your Decypharr configuration may use a different location. If so, the plugin configuration should be adjusted accordingly.

---

# Docker Mount Requirements

Because Dispatcharr runs inside Docker, the Decypharr media filesystem must be visible from inside the Dispatcharr container.

For installations using `/mnt` as the shared media mount, the Dispatcharr container should have access similar to:

```yaml
volumes:
  - /mnt/:/mnt:rshared
```

The exact Docker configuration may vary depending on the host installation.

After changing Docker mounts, recreate the Dispatcharr container so the new filesystem mapping is active.

You can verify the mount from inside Dispatcharr with:

```bash
docker exec dispatcharr ls -la /mnt
```

The Decypharr media root should then be accessible from inside the container.

---

# Installation

## 1. Download the Plugin

Download the latest release ZIP from the project's GitHub Releases page.

Do **not** extract the ZIP manually unless specifically instructed by the release documentation.

The ZIP is intended to be installed directly through Dispatcharr.

---

## 2. Install Through Dispatcharr

Open the Dispatcharr administration interface and navigate to the plugin management section.

Upload the plugin ZIP and complete the installation.

The plugin should install its required components automatically.

### Automatic Directory Creation

The plugin uses:

```text
/mnt/decypharr_vods
```

as its VOD working/output directory.

This directory is intended to be created automatically by the plugin.

Users should **not** need to manually run:

```bash
mkdir /mnt/decypharr_vods
```

or manually modify permissions as part of a normal installation.

If installation fails with:

```text
[Errno 13] Permission denied: '/mnt/decypharr_vods'
```

this indicates that the Dispatcharr container does not have the required permissions to create the plugin directory under `/mnt`.

See the troubleshooting section below.

---

# Initial Configuration

After installation, configure the plugin from Dispatcharr's plugin interface.

The primary media source is the Decypharr media tree:

```text
/mnt/decypharr/__all__
```

The plugin's generated VOD working directory is:

```text
/mnt/decypharr_vods
```

The plugin should create this directory automatically when required.

---

# Media Scanning

Once configured, start a VOD scan from the plugin interface.

The scanner examines the Decypharr media tree and identifies available VOD content.

For television content, the scanner builds the appropriate hierarchy:

```text
Series
└── Season
    └── Episode
```

For example:

```text
Example Series
├── Season 01
│   ├── Episode 01
│   ├── Episode 02
│   └── Episode 03
└── Season 02
    ├── Episode 01
    └── Episode 02
```

The resulting content is registered with Dispatcharr's VOD system.

---

# Filename and Metadata Handling

The scanner is intentionally designed for real-world media libraries.

Media filenames are not always standardized. A library may contain:

* scene-style names
* release-group names
* years
* quality information
* codec information
* source tags
* language tags
* brackets
* parentheses
* punctuation
* multiple naming conventions

The scanner therefore separates **media identification** from simple destructive filename stripping.

The goal is to identify the actual series/movie and episode information while preserving the underlying media path.

---

# Architecture

The plugin operates as a bridge between Decypharr's filesystem and Dispatcharr's VOD subsystem.

```text
                 Decypharr
                     │
                     ▼
             /mnt/decypharr/__all__
                     │
                     ▼
                VOD Scanner
                     │
          ┌──────────┴──────────┐
          │                     │
       Movies              TV Series
                                │
                         ┌──────┴──────┐
                         │             │
                      Seasons       Episodes
                         │             │
                         └──────┬──────┘
                                ▼
                      Dispatcharr VOD DB
                                │
                                ▼
                         VOD Playback
```

The plugin does not need to duplicate the actual media content simply to make it visible to Dispatcharr.

---

# Docker Compatibility

The plugin is designed specifically for containerized Dispatcharr deployments.

The important requirement is that the Dispatcharr container can access the same Decypharr filesystem exposed on the host.

For example:

```text
HOST
│
├── /mnt/decypharr/__all__
│
└── /mnt/decypharr_vods
        │
        ▼
DISPATCHARR CONTAINER
│
└── /mnt/
    ├── decypharr/
    │   └── __all__
    │
    └── decypharr_vods/
```

The host filesystem remains the source of truth for the media.

---

# Troubleshooting

## Permission denied creating `/mnt/decypharr_vods`

Error:

```text
[Errno 13] Permission denied: '/mnt/decypharr_vods'
```

The plugin attempts to create its required directory automatically.

This error means the Dispatcharr process does not currently have permission to create the directory at that location.

First verify that `/mnt` is available inside the Dispatcharr container:

```bash
docker exec dispatcharr ls -ld /mnt
```

Then verify the container's effective user:

```bash
docker exec dispatcharr id
```

Finally verify the Docker mounts:

```bash
docker inspect dispatcharr \
  --format '{{range .Mounts}}{{println .Source " -> " .Destination}}{{end}}'
```

A typical installation should expose the host `/mnt` tree inside Dispatcharr.

---

## Decypharr Root Not Found

If the plugin reports:

```text
Decypharr root not found
```

verify the path from inside the Dispatcharr container:

```bash
docker exec dispatcharr ls -la /mnt/decypharr/__all__
```

If the directory exists on the host but not inside the container, the problem is the Docker volume mapping rather than the plugin scanner.

---

## Scan Finds No Media

Verify that Decypharr has actually populated its media tree:

```bash
ls -la /mnt/decypharr/__all__
```

Then verify the same path from Dispatcharr:

```bash
docker exec dispatcharr ls -la /mnt/decypharr/__all__
```

If the host can see the files but Dispatcharr cannot, check the Docker volume configuration.

---

## Episodes Appear but Do Not Play

If series and episodes are successfully imported but playback fails, verify that the resulting media path is accessible from inside the Dispatcharr container.

For example:

```bash
docker exec dispatcharr ls -la /mnt/decypharr/__all__
```

The scanner and playback system must be able to resolve the same underlying filesystem.

---

# Development

The repository is structured as a Dispatcharr plugin project.

Development should be performed against a test Dispatcharr installation rather than directly against a production VOD database.

Recommended development workflow:

```text
Modify source
    ↓
Build plugin ZIP
    ↓
Install ZIP in Dispatcharr
    ↓
Run scan
    ↓
Verify database records
    ↓
Verify playback
```

When testing scanner changes, use a small media subset before performing a complete library scan.

---

# Release Packaging

Releases are distributed as a ZIP package compatible with Dispatcharr's plugin installer.

The release package should contain everything required for the plugin to operate.

Users should not need to:

* manually copy Python files
* manually install dependencies
* manually create directories
* manually modify plugin files
* manually patch Dispatcharr
* manually execute setup scripts

A clean Dispatcharr installation should be sufficient.

---

# Design Goals

The project is built around several principles:

### Self-contained

Installation should be handled by the plugin rather than requiring users to perform host-side setup.

### Non-destructive

The plugin should never modify or rename the user's original Decypharr media unnecessarily.

### Compatible with real media libraries

Media naming conventions vary widely. The scanner should use structured parsing and metadata where available instead of depending on brittle filename stripping rules.

### Container-aware

Paths must be valid from the perspective of the Dispatcharr container, not merely the Docker host.

### Dispatcharr-native

Imported VOD content should use Dispatcharr's existing VOD infrastructure rather than creating a parallel media database.

---

# Status

**Development Status:** Active Development

The project is currently being tested against real-world Decypharr media libraries and Dispatcharr VOD installations.

Testing currently includes:

* Decypharr filesystem discovery
* TV series detection
* Season detection
* Episode detection
* Dispatcharr VOD database integration
* Media path resolution
* Playback validation
* Fresh ZIP installation
* Automatic directory creation

---

# Disclaimer

This project is an independent community plugin and is not affiliated with, endorsed by, or officially supported by the Dispatcharr or Decypharr projects.

Always maintain backups of important configuration and media metadata before testing third-party plugins or performing large VOD database operations.

