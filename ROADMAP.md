# Project Roadmap

This document outlines planned future development for `dovi_convert`.

> **Note:** This roadmap is subject to change based on user feedback and technical feasibility.

## Planned for the next major version (9.x)

- **New Deep Inspection mode** - Analyzes the HDR10 base layer frame by frame, extracting peak brightness values to compare against Dolby Vision RPU peak brightness values. This will be a much more accurate method for detecting brightness expansion in the FEL.

- **Fast-path Complex FEL detection** - When certain conditions are met in the HDR10 and DV metadata, we will assume Complex FEL. This is ultra-fast, as it only analyzes a few key metadata fields.

- **Caching system for scan and inspect results** - Store the results of `-scan` and `-inspect` in a cache to avoid re-analyzing files. Once cached, results are reused for future scans and conversions.

- **Auto-Inspect Simple FEL During Scan** — New `-inspect-simple` flag for `-scan` that automatically runs full inspection on all Simple FEL files after the scan completes. Eliminates the need to manually run `-inspect` on each file. ([#16](https://github.com/cryptochrome/dovi_convert/issues/16))

## Planned for the future (in no particular order)

- **Watch Folder Support** - for Docker users, set up a watch folder to automatically trigger conversions when new files are added to the folder.

## Under Consideration

- **Web Interface (Docker Phase 2)** — Browser-based management UI for NAS users: visual file browser, batch selection, live progress monitoring, and backup management.

- **`-keep-both` Option:** Preserves the original filename (no added *.bak.dovi_convert suffix) and adds `.p81.mkv` suffix to converted file. This allows you to keep both files as .mkv files. Useful for seeding or multi-version-capable media servers (like Plex). If you think this is useful, please open a discussion or issue, so I know you want this.
