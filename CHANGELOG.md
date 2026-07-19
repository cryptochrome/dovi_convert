# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- **Docker:** New `UMASK` environment variable to control file creation permissions on bind-mounted volumes. Defaults to `022` (standard Linux: `rw-r--r--`). Set to `000` for UNRAID compatibility (`rw-rw-rw-`, matching UNRAID's shared `nobody:users` model) or `002` for typical NAS / multi-user media setups (`rw-rw-r--`, group-writable).
- **Docker:** Added support for rootless Docker by allowing `PUID=0` and `PGID=0` when container root maps to unprivileged outer IDs.

### Fixed
- **UI polish:** Minor CLI output polish.
- **Docker:** Fixed a font rendering issue in the web terminal that caused text to appear stretched or with excessive spacing between characters for many users. (ref: #40)
- **Docker:** Container now supports low PUIDs (e.g. `99` for UNRAID, Synology) by overriding Debian's default `UID_MIN=1000` constraint during user creation.

## [v8.2.0] - 2026-03-16

Version 8.2 finally introduces the Backup & Restore feature. It allows you to backup the Dolby Vision enhancement layer before or during a conversion. You can then delete the original file and, as long as you keep the converted file, restore the original at any time using the backed up EL. Benefits: Save tons of disk space. Instead of keeping the original file around, you just store the enhancement layer, which is much smaller. 

This release is dedicated to my cat Pixel. Rest in Peace. 

### Added
- **Backup & Restore:** 
    - Use `dovi_convert backup movie.mkv` to backup the enhancement layer to `movie.dovi`.
    - Use `dovi_convert restore movie.mkv` to restore the full DoVi 7.1 file using the converted `movie.mkv` and the `movie.dovi` backup archive. 
    - Use `dovi_convert convert movie.mkv --backup` to backup the enhancement layer during a conversion
    - Use `dovi_convert convert movie.mkv --backup --delete` to backup during conversion and delete the original in one go.
    - Supports `--output` flag to control where the backup archive is stored (Limitation: if backing up during a conversion, the backup archive AND the converted file will go to the output directory - will be improved in the next release).
    - Refer to the full documentation for more information.

### Changed
- Improved signal handler to make it context-aware and print appropriate exit messages depending on command used
- Removed old, unused code

### Removed
- Removed the macOS notification that was sent after conversions. The feature was never really built out and mostly useless. I may re-introduce a proper notification system in the future. 


## [v8.1.1] - 2026-01-30

This is a maintenance release with some bug fixes and code improvements

### Changed
- Removed unused code in the cleanup function.
- Refactored stats collection and batch summaries
- Streamlined batch pre-flight and summary UI

### Fixed
- Fixed `scan` and `inspect` failing on files in folders containing commas (e.g., "Movie, The (2025)"). (Fixes #36) 
- Fixed an issue where `CTRL-C` wasn't handled properly during batch conversions

## [v8.1.0] - 2026-01-27

### Added
- **Compact Batch Output Mode:** `convert` batch mode now displays a clean, table-based progress view by default.
    - Use `--verbose` / `-v` flag to see the previous detailed per-file output.
- **No-Color-Mode:** dovi_convert now respects the NO_COLOR environment variable and disables CLI colors when set.
- **Session Persistence in Docker:** added tmux to the container for persistent sessions. For more information, read the [Docker guide](https://docs.doviconvert.com/installation/docker).

### Changed
- **Improved colors** for light theme terminals (I was told they exist). Yellow is now Magenta and Cyan is now Blue. Should improve readability in terminals with light backgrounds.
- **`scan` output:** cleaned up and fixed some text alignment and padding issues.


## [v8.0.0] - 2026-01-24 - BREAKING CHANGES

This is a major release that introduces significant changes to dovi_convert. Please read the docs and the release notes carefully before upgrading. **Breaking:** This version introduces a completely rewritten argument parser and command syntax, to align with Unix standards and to make the tool more user-friendly. 

This release also merges the `convert` and `batch` commands into a single, unified `convert` command. 

If you are scripting dovi_convert, you will need to update your scripts to use the new command syntax. 

### Added
- **Unified Convert Command:** The `batch` command has been merged into `convert`. You can now pass both files and directories to `convert`:
  - `dovi_convert convert file.mkv` - converts a single file
  - `dovi_convert convert dir/` - scans directory, shows summary, asks for confirmation and converts files found (batch behavior)
  - `dovi_convert convert *.mkv` - converts multiple files inline
  - Directories trigger the familiar batch-style pre-scan with summary and confirmation prompts.
  - The `--recursive` flag now works with `convert` when directories are provided.

### Changed
- **`--yes` Behavior:** When using `--yes` for automated workflows, Simple FEL files are now skipped by default to avoid unexpected prompts. Use `--yes --include-simple` to include them.
- **Command Syntax:** The tool now distinguishes between commands and flags: `dovi_convert [command] [flags] [files]` 
   - for example: `dovi_convert convert --safe file.mkv` instead of `dovi_convert -convert file.mkv -safe`. 
   - Commands are now single words without leading hyphens, and flags use a double-hyphen. 
   - Some of them have short abbreviations (e.g. `-y` for `--yes`). 
   - See `dovi_convert help` for more info.
- **Command Validation:** Added command flag validation to prevent invalid flag combinations.
- **Code Organization:** Reorganized some code for better maintainability.
- **Help Text Refactor** Refactored help text, separated from main app class (it shouldn't have been in there in the first place).
- **Quick Help:** Streamlined and shortened quick help output, with hint to use `dovi_convert help` for all commands and flags.

### Removed
- `batch` command removed, merged into `convert`.

### Deprecated
- old command syntax with `-[command]` (e.g. `-convert`)

## [v7.3.2] - 2026-01-18    

This is a last "cleanup" release before the next major version 8.0.0

### Added
- **Homebrew** - you can now install dovi_convert via Homebrew (e.g. `brew install dovi_convert`)

### Fixed
- `-cleanup` incorrectly flagged valid backups as orphans when the original filename contained multiple dots.

## [v7.3.1] - 2026-01-16

### Fixed
- **Critical:** Fixed incorrect metadata interpretation in base layer peak brightness detection. The tool was reading the wrong HDR10 metadata field, which could affect FEL complexity verdicts in `-scan` and `-inspect`.

### Changed
- `-inspect` now indicates when MaxCLL metadata is unavailable and a default value is being used ("1000 nits (default)").


## [v7.3.0] - 2026-01-14

### Added
- **HDR10 Conversion:** New `-hdr10` flag for `-convert` to convert to HDR10 instead of Dolby Vision profile 8.1. Retains HDR10+ metadata if present in the source. Useful for Complex FEL files where you want to prevent devices like the Shield from converting to 8.1 when it shouldn't. Read the docs for more info.

- **Scan Filter:** New `-candidates` flag for `-scan` to show only files that can be converted, filtering out SDR, HDR10, Profile 8, etc. Useful for quickly identifying conversion targets in large libraries.


### Changed
- Cleaned up `-help` message formatting.
- `-scan` now displays a clear message when no MKV files are found instead of an empty table.


## [v7.2.0] - 2026-01-12

### Added
- **Custom Output Directory:** New `-o` flag to specify a custom output directory for converted files (`-convert file.mkv -o /output`). Works with `-convert` and `-batch`. Read docs for more info. (Ref #4)

### Changed
- Improved error messages when tools like mkvmerge or mediainfo fail.
- Improved `-debug` logging
- If more than 50% of probes fail during -scan, we now treat it as an error and exit.


## [v7.1.0] - 2026-01-07

### Breaking Change
- **Batch Recursion Syntax:** The shorthand `-batch 2` no longer works. Use `-batch -r 2` instead, matching `-scan` and `-cleanup`.

### Added
- **Multi-File Convert:** Convert multiple files in one command (`-convert file1.mkv file2.mkv`). Works with wildcards (`-convert *.mkv`). A summary is displayed at the end.
- **Target Directories:** Point `-scan` or `-batch` at specific directories instead of navigating there first (`-batch /movies`). Multiple directories are supported (`-scan /movies /tv`).
- **Recursive with Directories:** The `-r` flag works with target directories (`-batch /movies -r 3`).
- **Mixed Inputs for Scan:** Combine files and directories in a single `-scan` command (`-scan /movies file.mkv`).
- **Temp Directory:** New `-temp` flag to redirect intermediate files to a fast drive (SSD). This dramatically improves conversion speed when source files are on slow storage like HDDs or network shares. Example: `-convert movie.mkv -temp /mnt/ssd`. Works with `-convert` and `-batch`. 
- **Directory Grouping:** Recursive scans now group files by directory with clear headers, making it easier to see which files belong where. 

### Changed
- **Docker:** The container no longer modifies permissions on bind-mounted directories. Users must ensure their `PUID`/`PGID` matches the ownership of their files (standard Docker practice).
- **Docker:** Users can now bind-mount a temp directory for the new `-temp` feature. Example: `-v /mnt/ssd:/cache` then use `-temp /cache`.


## [v7.0.1] - 2026-01-04
### Fixed
- **Better Error Handling:** Fixed a bug where temporary files were not deleted if the script exited due to a conversion error (Ref #21).

### Changed
- **Conversion Verification:** Implemented a fallback mechanism for frame verification. If `MediaInfo` reports a frame mismatch (common with inaccurate source metadata), the script now double-checks using `ffprobe` stream analysis before failing (Ref #21).
- **WSL Path Limit:** Improved handling of long file paths in WSL, which can exceed the 255 character limit (Ref #14).
- **Windows:** Improved exiting on native Windows (WSL required) (Ref #22).

## [v7.0.0] - 2026-01-03 (Stable Release)

> **This is a major release.** The tool has been completely rewritten in Python, replacing the original Bash script.

### Breaking Changes
- **Python 3.8+ is now required.** The Bash version is no longer maintained.
- **Dependencies changed:** Removed `jq`, `bc`, `curl`. The script now only requires Python and core media tools.
- **Docker:** The `:beta` tag will be deprecated. Switch to `:latest`.

### Highlights (from Beta Cycle)
- **Complete Python rewrite** for improved maintainability and cross-platform compatibility.
- **5x faster** RPU analysis (`-inspect`) due to native Python processing with streaming parser.
- **Reduced dependencies** — no more shell utilities like `jq`, `bc`, `curl`, `sed`, `awk`, or `grep`.
- **Improved error handling** — better detection of tool failures and MediaInfo issues.
- **WSL Required** — Native Windows is explicitly blocked. Windows users must use WSL2 or Docker.

### Fixed (since beta5)
- Process safety improvements for `-inspect` command.

### Note for Docker Users
If you were using the `:beta` tag, please update to `:latest`. The `:beta` tag will be removed after a transition period.

---

## [v7.0.0-beta5] - 2026-01-01
### Fixed
- **Process Safety:** Fixed an issue where the `-inspect` command could potentially give a false "safe" verdict if one of the internal tools crashed silently. It now correctly reports a failure in this scenario.

### Changed
- **Internal Optimization:** Refactored core analysis commands to improve stability and make future feature updates safer.
- **Error Handling:** Improved detection and handling of MediaInfo-related issues.
- **Windows:** Added a hard error message when running on Native Windows (WSL required). Native Windows is not supported.
- **UI:** Removed misleading "(Slow)" label from export step and improved color contrast in scan advisory.

## [v7.0.0-beta4] - 2026-01-01
### Changed
- Significantly reduced memory usage and improved speed during `-inspect` analysis (Streaming Parser).

## [v7.0.0-beta3] - 2025-12-30
### Changed
- Improved Docker container compatibility (Temporary files now use bind-mount).

## [v6.6.5] - 2025-12-30
### Changed
- Improved Docker container compatibility (Temporary files now use bind-mount).
## [v7.0.0-beta2] - 2024-12-30
### Fixed
- **Interactive Batch Exclusion:** Responding "No" to the "Include Simple-FEL" prompt now correctly filters those files from the queue instead of cancelling the entire batch.
- **Ctrl+C Handling:** Fixed an issue where the script would hang if interrupted during batch mode prompts.
- **Redundant Prompts:** Fixed an issue where Simple-FEL files would trigger a confirmation prompt for every single file even after being approved at the batch start.

### Changed
- **Terminology:** Updated batch warning messages to refer to `-scan` instead of `-check`.
- **Messaging:** Improved clarity of Simple-FEL inclusion messages ("Explicitly included").

## [v7.0.0-beta1] - 2024-12-30 (Public Beta)
### Python Rewrite & Performance
- **Rewritten from scratch:** The entire tool has been rewritten in Python, replacing the original Bash script.
    - **Why?** To ensure future maintainability, improve stability, and allow for more new features in the future (Bash was getting out of hand).
- **Performance:** RPU analysis (`-inspect`) is **5x faster** due to native Python processing.
- **Dependencies:** Removed `jq`, `bc`, `curl`, `sed`, `awk`, `grep`. The script now only uses the standard library and core media tools (`ffmpeg`, etc.).
- **Parity:** Functionally identical to the v6 Bash version.


## [6.6.4] - 2025-12-28
### Added
- **New `-scan` command** - A better-named alias for `-check`. Both commands work identically. `-check` remains supported for backwards compatibility.

### Changed
- README completely rewritten for clarity and structure.
- All "deep scan" terminology replaced with "scan" throughout the script.
- macOS: Homebrew no longer required if all dependencies are already installed.
- macOS: Added guidance for users without Homebrew during dependency check.
- dovi_tool install instructions now link to main repo page.

## [6.6.3] - 2025-12-26
### Fixed
- Fixed `-include-simple` flag not suppressing per-file Simple FEL prompts during batch runs (Fixes #5).

## [6.6.2] - 2025-12-26
### Changed
- Cleanup release: Updated internal help text to better reflect features and changes from recent versions.

## [6.6.1] - 2025-12-25
### Fixed
- Fixed `jq` errors when scanning directories containing macOS resource fork files.
- Fixed random "File not found" errors and corrupted filenames during recursive scans.
- Scan commands now fully exclude macOS resource fork artifacts (`._*` files and folders).

### Changed
- Improved color theme of the -check output to better distinguish between different file types.
- Recursive scans now display filenames only (not folder paths) for cleaner output.

## [6.6] - 2025-12-25

### Added
- **Automatic Update Check:** The tool now silently checks for updates in the background without slowing down execution (Zero-Latency). If a new version is found, a notification is displayed on the *next* run.
- **Manual Update Check:** Added `-update-check` command to manually check for the latest version and report status immediately.
- **Automatic Dependency Installation (Beta):** If dependencies are missing, the tool now offers to install them automatically. Supports Homebrew (macOS), apt (Ubuntu/Debian), dnf (Fedora), and pacman (Arch).
- **Simple FEL Warning:** The `-convert` command now prompts for confirmation when processing Simple FEL files to match the safety behavior of batch mode.

### Changed
- **Dependencies:** Added `curl` to the list of required dependencies (standard on most systems).

## [6.5.1] - 2025-12-24
### Added
- Batch output now displays separate counts for MEL and Simple FEL files.
- Added warnings when "Simple FEL" files are detected in batch mode.
- `-include-simple` flag to allow automated processing of "Simple FEL" files in batch mode.
- Added context to the check command explaining "Simple FEL" results.

### Changed
- Improved detection logic to be more robust and accurate.
- Improved handling of titles mastered at 4000 nits.
- Batch mode now defaults to pausing if "Simple FEL" files are found, even with `-y`.
- Updated help text descriptions for clarity.

## [6.5] - 2025-12-22
### Added
- **Deep Scan FEL Detection**: New analysis logic that inspects the RPU structure to deterministically identify "Complex" FEL titles (e.g. FEL that elevates luminance and shouldn't be converted to profile 8.1).
- **`-force` Flag**: Override safety warnings for Complex FEL titles (e.g., `dovi_convert -convert movie.mkv -force`). Works for single file and batch mode.
- **Improved Batch Summary**: Batch mode now tracks "Ignored" (Not Profile 7), "Skipped" (Unsafe), and "Converted" files separately. It also distinguishes between "Simple" and "Forced" conversions in the final report.
- **`-inspect` Command**: New standalone tool to inspect full RPU structure and verify Complex FEL verdicts by checking the entire file's brightness metadata.

### Changed
- **Default Behavior**: `-check` now performs a Deep Scan on Profile 7 files by default.
- **Strict Enforcement**: The tool now hard-fails if the input file is not Dolby Vision Profile 7 (removed interactive prompts for non-P7 files).
- **Safe Mode UI**: Progress numbering aligned to standard workflow (`[1/3] Extracting`, `[1/3] Converting`) for consistency.
- **Arguments**: Arguments can now be passed in any order. Fixed `-r` argument parsing logic.
- **Safety**: Conversion now defaults to skipping/warning on Complex FELs to prevent data loss.

## [6.4.2] - 2025-12-19
### Added
- **Piping Implementation:** Standard conversion now pipes `ffmpeg` output directly to `dovi_tool`. This eliminates temporary disk usage for the video stream and improves processing speed.
- **Batch Summary:** The `-batch` command scans the directory and displays total file count and size before requesting confirmation.
- **Smart Fallback:** Added logic to detect specific stream errors (often caused by Seamless Branching) and offer Safe Mode as a retry option.
- **Debug Logging:** Added `-debug` flag to write full tool output to `dovi_convert_debug.log` for troubleshooting.
- **Global `-y` Flag:** Added support for auto-confirming interactive prompts (batch start and cleanup).
- **Metrics:** Script now reports space saved (EL discarded) and average FPS upon completion.

### Changed
- **Cleanup Scope:** `-cleanup` now defaults to non-recursive scanning. Added `-r` flag to enable recursive mode.
- **Cleanup UI:** The command now lists all files to be deleted before asking for confirmation.
- **UI:** Replaced static text with a spinner for progress indication.
- **Validation:** Added strict argument parsing to prevent syntax errors.

## [6.3]
### Added
- Initial public release.
