#!/usr/bin/env python3
"""
dovi_convert - Dolby Vision Profile 7 -> 8.1 Converter

Copyright (C) 2025-2026 cryptochrome

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

DESCRIPTION:
  Automates conversion of Dolby Vision Profile 7 MKV files (UHD Blu-ray)
  into Profile 8.1. This ensures compatibility with devices that do not support
  the Enhancement Layer.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# =============================================================================
# CONSTANTS
# =============================================================================

VERSION = "8.2.0"
REPO_URL = "https://api.github.com/repos/cryptochrome/dovi_convert/releases/latest"

# ANSI formatting (always enabled)
BOLD = "\033[1m"
RESET = "\033[0m"

# ANSI colors (respect NO_COLOR standard: https://no-color.org/)
if os.environ.get("NO_COLOR"):
    RED = GREEN = YELLOW = MAGENTA = CYAN = BLUE = DEFAULT = ""
else:
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    BLUE = "\033[34m"
    DEFAULT = "\033[39m"

ANSI_ESCAPE_RE = re.compile(r"\033\[[0-9;]*m")

# Cache directory
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "dovi_convert"
UPDATE_FILE = CACHE_DIR / "latest_version"

# Dependency map: command -> (brew, apt, dnf, pacman)
DEP_MAP = {
    "mkvmerge": ("mkvtoolnix", "mkvtoolnix", "mkvtoolnix", "mkvtoolnix"),
    "mkvextract": ("mkvtoolnix", "mkvtoolnix", "mkvtoolnix", "mkvtoolnix"),
    "dovi_tool": ("dovi_tool", "dovi_tool", "dovi_tool", "dovi_tool"),
    "mediainfo": ("mediainfo", "mediainfo", "mediainfo", "mediainfo"),
    "ffmpeg": ("ffmpeg", "ffmpeg", "ffmpeg", "ffmpeg"),
}

# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class VideoInfo:
    """Video track metadata from mkvmerge/mediainfo."""
    track_id: Optional[int] = None
    delay: int = 0
    language: str = "und"
    name: str = ""
    mi_info_string: str = ""
    fps: str = ""
    frame_count: int = 0
    mkvmerge_error: str = ""


@dataclass
class ScanResult:
    """Result of FEL complexity analysis."""
    verdict: str = "UNKNOWN"  # SAFE, COMPLEX, UNKNOWN
    reason: str = "Analysis failed"


@dataclass
class Config:
    """Runtime configuration flags."""
    debug_mode: bool = False
    safe_mode: bool = False
    force_mode: bool = False
    auto_yes: bool = False
    include_simple: bool = False
    delete_backup: bool = False
    candidates_only: bool = False
    hdr10_mode: bool = False
    verbose_mode: bool = False
    create_backup: bool = False
    temp_dir: Optional[Path] = None
    output_dir: Optional[Path] = None
    backup_source: Optional[Path] = None


@dataclass
class ParsedArgs:
    """Structured result from argument parsing."""
    command: str = ""                 # "convert", "scan", "batch", etc.
    files: List[Path] = field(default_factory=list)
    directories: List[Path] = field(default_factory=list)
    recursive: bool = False           # Was -r/--recursive specified?
    recursive_depth: int = 1          # scan, batch, cleanup
    
    # Global flag (applies to all commands)
    debug: bool = False
    
    # Command-specific flags
    force: bool = False               # convert, batch
    safe: bool = False                # convert, batch, inspect
    yes: bool = False                 # batch, cleanup
    include_simple: bool = False      # batch
    delete_backup: bool = False       # convert, batch
    hdr10: bool = False               # convert
    candidates_only: bool = False     # scan
    verbose: bool = False             # convert (batch mode - show detailed output)
    create_backup: bool = False       # convert (create EL backup before conversion)

    # Path options
    temp_dir: Optional[Path] = None   # convert, batch, backup, restore
    output_dir: Optional[Path] = None # convert, batch, backup, restore
    backup_source: Optional[Path] = None  # restore (external backup location)

@dataclass
class FlagDef:
    """Definition for a command-line flag."""
    long: str                           # "--temp"
    field: str                          # "temp_dir" (ParsedArgs field)
    commands: frozenset                 # frozenset({"convert", "batch"})
    short: Optional[str] = None         # "-t"
    takes_value: bool = False           # Does it require an argument?
    optional_value: bool = False        # Can the value be omitted? (for -r)
    default_value: Any = None           # Default when optional_value omitted


# =============================================================================
# FLAG REGISTRY - Single source of truth for all command-line flags
# =============================================================================

FLAGS = [
    # Global flag (applies to all commands)
    FlagDef("--debug", "debug",
            frozenset({"convert", "scan", "inspect", "cleanup", "update-check", "backup", "restore"})),

    # convert flags
    FlagDef("--force", "force", frozenset({"convert"}), short="-f"),
    FlagDef("--safe", "safe", frozenset({"convert", "inspect"}), short="-s"),
    FlagDef("--delete", "delete_backup", frozenset({"convert"})),
    FlagDef("--temp", "temp_dir", frozenset({"convert", "backup", "restore"}), short="-t",
            takes_value=True),
    FlagDef("--output", "output_dir", frozenset({"convert", "backup", "restore"}), short="-o",
            takes_value=True),
    FlagDef("--backup", "create_backup", frozenset({"convert"}), short="-b"),
    FlagDef("--hdr10", "hdr10", frozenset({"convert"})),
    FlagDef("--yes", "yes", frozenset({"convert", "cleanup"}), short="-y"),
    FlagDef("--include-simple", "include_simple", frozenset({"convert"})),
    FlagDef("--recursive", "recursive", frozenset({"scan", "convert", "cleanup"}), short="-r",
            takes_value=True, optional_value=True, default_value=5),
    FlagDef("--verbose", "verbose", frozenset({"convert"}), short="-v"),

    # scan only
    FlagDef("--candidates", "candidates_only", frozenset({"scan"})),

    # restore only
    FlagDef("--source", "backup_source", frozenset({"restore"}), takes_value=True),
]

# Build lookup tables (derived, not manually maintained)
FLAGS_BY_LONG: Dict[str, FlagDef] = {f.long: f for f in FLAGS}
FLAGS_BY_SHORT: Dict[str, FlagDef] = {f.short: f for f in FLAGS if f.short}
FLAGS_BY_FIELD: Dict[str, FlagDef] = {f.field: f for f in FLAGS}


@dataclass
class ConversionMetrics:
    """Metrics for a conversion operation."""
    start_time: float = 0.0
    orig_size: int = 0
    frame_count: int = 0
    fps: str = ""


@dataclass
class BatchStats:
    """Statistics for batch processing."""
    success_list: List[str] = field(default_factory=list)
    fail_list: List[Tuple[Path, str]] = field(default_factory=list)  # (path, error_reason)
    ignored_count: int = 0
    skipped_count: int = 0
    complex_count: int = 0
    success_simple_count: int = 0
    success_forced_count: int = 0
    success_simple_fel_count: int = 0
    converted_size: int = 0
    elapsed: float = 0.0
    queue_count: int = 0
    aborted: bool = False


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def version_gt(v1: str, v2: str) -> bool:
    """Returns True if v1 > v2 (semantic version comparison, suffixes ignored)."""
    def parse(v):
        return v.lstrip("v").split("-", 1)[0]

    try:
        p1 = [int(x) for x in parse(v1).split(".")]
        p2 = [int(x) for x in parse(v2).split(".")]
    except ValueError:
        return False

    return p1 > p2





def is_wsl() -> bool:
    """Check if running in Windows Subsystem for Linux."""
    try:
        if Path("/proc/version").exists():
            with open("/proc/version", "r") as f:
                return "microsoft" in f.read().lower()
    except Exception:
        pass
    return False


def check_wsl_path_limit(filepath: Path) -> bool:
    """
    Check if a file in WSL is on a Windows drive and exceeds the 255 char limit.
    Uses /proc/mounts to robustly identify Windows filesystems (drvfs/9p).
    Fails HARD if system mounts cannot be read.
    """
    try:
        abs_path = str(filepath.resolve())
        best_match = None
        
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    mount_point = parts[1]
                    fs_type = parts[2]
                    
                    if abs_path.startswith(mount_point):
                        # Keep the most specific mount point
                        if best_match is None or len(mount_point) > len(best_match[0]):
                             best_match = (mount_point, fs_type)
        
        if best_match:
            fs_type = best_match[1]
            if fs_type in ("drvfs", "9p") and len(abs_path) > 255:
                # Confirmed: Windows Drive + Long Path
                return True
                
    except Exception as e:
        # Fail Hard on Broken System
        print(f"\n{RED}Critical Error: Can't read /proc/mounts.{RESET}")
        print(f"{RED}Your WSL environment appears broken or is not supported.{RESET}")
        print(f"Debug: {e}")
        sys.exit(1)
        
    return False


def human_size_gb(size_bytes: int) -> str:
    """Convert bytes to human readable GB string."""
    gb = size_bytes / 1024 / 1024 / 1024
    return f"{gb:.2f} GB"


def get_file_size(filepath: Path) -> int:
    """Get file size in bytes."""
    try:
        return filepath.stat().st_size
    except OSError:
        return 0


def pq_to_nits(code_val: int) -> int:
    """Convert PQ code value (0-4095) to nits using ST.2084 EOTF."""
    if code_val <= 0:
        return 0
    
    # ST.2084 Constants
    m1 = 2610.0 / 16384.0
    m2 = 2523.0 / 32.0
    c1 = 3424.0 / 4096.0
    c2 = 2413.0 / 128.0
    c3 = 2392.0 / 128.0
    
    # Normalize 12-bit code value (0-4095) to 0-1
    V = code_val / 4095.0
    
    if V <= 0:
        return 0
    
    try:
        # Calculate V^(1/m2)
        vp = math.pow(V, 1.0 / m2)
        
        # Calculate max(vp - c1, 0)
        num = max(vp - c1, 0)
        
        # Calculate c2 - c3*vp
        den = c2 - c3 * vp
        if den == 0:
            den = 0.000001
        
        # Calculate R = (num / den)^(1/m1)
        base_val = max(num / den, 0)
        
        nits = 10000.0 * math.pow(base_val, 1.0 / m1)
        return int(round(nits))
    except (ValueError, OverflowError):
        return 0


def find_l1_values(obj, max_vals: list) -> None:
    """Recursively search JSON structure for L1 max_pq values, appending to max_vals."""
    if isinstance(obj, dict):
        for key in ["Level1", "l1", "L1"]:
            if key in obj:
                l1_data = obj[key]
                if isinstance(l1_data, dict):
                    for mkey in ["max_pq", "max", "Max"]:
                        if mkey in l1_data:
                            val = l1_data[mkey]
                            if isinstance(val, (int, float)):
                                max_vals.append(int(val))
        for v in obj.values():
            find_l1_values(v, max_vals)
    elif isinstance(obj, list):
        for item in obj:
            find_l1_values(item, max_vals)


def truncate_middle(filename: str, max_width: int) -> str:
    """Truncate filename in the middle, preserving start and end."""
    if len(filename) <= max_width:
        return filename
    available = max_width - 3  # for "..."
    start_len = available // 2
    end_len = available - start_len
    return filename[:start_len] + "..." + filename[-end_len:]


# =============================================================================
# SPINNER CLASS
# =============================================================================

class Spinner:
    """Animated spinner with elapsed time display."""
    
    def __init__(self, label: str):
        self.label = label
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.start_time = 0.0
        
        # Check for UTF-8 support
        lang = os.environ.get("LANG", "") + os.environ.get("LC_ALL", "")
        self.use_braille = "UTF-8" in lang.upper()
        
        if self.use_braille:
            self.spinstr = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        else:
            self.spinstr = "|/-\\"
    
    def _spin(self) -> None:
        idx = 0
        while self.running:
            elapsed = int(time.time() - self.start_time)
            minutes = elapsed // 60
            seconds = elapsed % 60
            char = self.spinstr[idx % len(self.spinstr)]
            
            # Clear line and print
            sys.stdout.write(f"\r\033[K{self.label} {char} ({minutes}m {seconds:02d}s)")
            sys.stdout.flush()
            
            idx += 1
            time.sleep(0.1)
    
    def start(self) -> None:
        """Start the spinner."""
        self.running = True
        self.start_time = time.time()
        # Hide cursor
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()
        self.thread = threading.Thread(target=self._spin, daemon=True)
        self.thread.start()
    
    def stop(self) -> None:
        """Stop the spinner."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)
        # Show cursor
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


class CompactBatchSpinner:
    """Specialized spinner for compact batch mode table rows."""

    def __init__(self, prefix: str, filename: str, fmt_str: str, filename_width: int):
        self.prefix = prefix
        self.filename = filename
        self.fmt_str = fmt_str
        self.filename_width = filename_width
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.start_time = 0.0

        # Capture stdout at init time (before suppress_output() redirects it)
        self._stdout = sys.stdout

        # Check for UTF-8 support
        lang = os.environ.get("LANG", "") + os.environ.get("LC_ALL", "")
        self.use_braille = "UTF-8" in lang.upper()

        if self.use_braille:
            self.spinstr = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
        else:
            self.spinstr = "|/-\\"

    def _spin(self) -> None:
        idx = 0
        while self.running:
            elapsed = int(time.time() - self.start_time)
            minutes = elapsed // 60
            seconds = elapsed % 60
            char = self.spinstr[idx % len(self.spinstr)]

            # Format: [prefix] filename   format   spinner (elapsed)
            progress = f"{char} ({minutes}m {seconds:02d}s)"
            line = f"{self.prefix} {self.filename:<{self.filename_width}}{self.fmt_str:<17}{progress}"
            self._stdout.write(f"\r\033[K{line}")
            self._stdout.flush()

            idx += 1
            time.sleep(0.1)

    def start(self) -> None:
        """Start the spinner."""
        self.running = True
        self.start_time = time.time()
        # Hide cursor
        self._stdout.write("\033[?25l")
        self._stdout.flush()
        self.thread = threading.Thread(target=self._spin, daemon=True)
        self.thread.start()

    def stop(self, success: bool) -> None:
        """Stop spinner and print final status."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)
        # Show cursor
        self._stdout.write("\033[?25h")
        self._stdout.flush()

        # Print final status
        if success:
            status = f"{GREEN}done{RESET}"
        else:
            status = f"{RED}failed{RESET}"

        line = f"{self.prefix} {self.filename:<{self.filename_width}}{self.fmt_str:<17}{status}"
        self._stdout.write(f"\r\033[K{line}\n")
        self._stdout.flush()


@contextmanager
def suppress_output():
    """Temporarily suppress stdout for compact batch mode."""
    old_stdout = sys.stdout
    sys.stdout = open(os.devnull, 'w')
    try:
        yield
    finally:
        sys.stdout.close()
        sys.stdout = old_stdout


# =============================================================================
# DEPENDENCY MANAGER
# =============================================================================

class DependencyManager:
    """Handles dependency checking and auto-installation."""
    
    PM_INDEX = {"brew": 0, "apt": 1, "dnf": 2, "pacman": 3}
    
    @staticmethod
    def get_pkg_name(cmd: str, pm: str) -> str:
        """Get package name for command and package manager."""
        if cmd in DEP_MAP:
            idx = DependencyManager.PM_INDEX.get(pm, 0)
            return DEP_MAP[cmd][idx]
        return cmd
    
    @staticmethod
    def check_pkg_available(pkg: str, pm: str) -> bool:
        """Check if package is available in repos."""
        try:
            if pm == "brew":
                result = subprocess.run(["brew", "info", pkg], capture_output=True)
            elif pm == "apt":
                result = subprocess.run(["apt-cache", "show", pkg], capture_output=True)
            elif pm == "dnf":
                result = subprocess.run(["dnf", "info", pkg], capture_output=True)
            elif pm == "pacman":
                result = subprocess.run(["pacman", "-Si", pkg], capture_output=True)
            else:
                return False
            return result.returncode == 0
        except Exception:
            return False
    
    @staticmethod
    def detect_package_manager() -> Tuple[str, str, bool, bool]:
        """Detect package manager. Returns (pm, install_cmd, needs_sudo, is_arch)."""
        if sys.platform == "darwin":
            if shutil.which("brew"):
                return ("brew", "brew install", False, False)
            return ("", "", False, False)
        
        has_brew = shutil.which("brew") is not None
        
        if shutil.which("apt"):
            return ("apt", "sudo apt install -y", True, False)
        if shutil.which("dnf"):
            return ("dnf", "sudo dnf install -y", True, False)
        if shutil.which("pacman"):
            return ("pacman", "sudo pacman -S --noconfirm", True, True)
        if has_brew:
            return ("brew", "brew install", False, False)
        
        return ("", "", False, False)
    
    @staticmethod
    def find_missing() -> List[str]:
        """Find missing dependencies."""
        missing = []
        for cmd in DEP_MAP.keys():
            if not shutil.which(cmd):
                if cmd not in missing:
                    missing.append(cmd)
        return missing
    
    @staticmethod
    def install_dependencies(missing: List[str]) -> None:
        """Install missing dependencies."""
        pm, pm_install, needs_sudo, is_arch = DependencyManager.detect_package_manager()
        
        if not pm:
            print(f"{RED}Unsupported system.{RESET} Please install dependencies manually:")
            for dep in missing:
                print(f"  - {dep}")
            sys.exit(1)
        
        if needs_sudo:
            print()
            print(f"{MAGENTA}Note: Installation requires administrator privileges.{RESET}")
            print("You may be prompted for your password.")
            print()
        
        installed = []
        failed = []
        manual = []
        already_installed = set()
        
        total = len(missing)
        for idx, cmd in enumerate(missing, 1):
            pkg = DependencyManager.get_pkg_name(cmd, pm)
            
            if pkg in already_installed:
                continue
            
            print(f"[{idx}/{total}] Installing {cmd} ({pkg})... ", end="", flush=True)
            
            if not DependencyManager.check_pkg_available(pkg, pm):
                # Try brew fallback for dovi_tool on Linux
                if cmd == "dovi_tool" and pm != "brew" and shutil.which("brew"):
                    brew_pkg = DependencyManager.get_pkg_name(cmd, "brew")
                    if DependencyManager.check_pkg_available(brew_pkg, "brew"):
                        print("(via Homebrew) ", end="", flush=True)
                        pm_install = "brew install"
                        pkg = brew_pkg
                    else:
                        print(f"{MAGENTA}Not in repos.{RESET}")
                        manual.append(cmd)
                        continue
                else:
                    print(f"{MAGENTA}Not in repos.{RESET}")
                    manual.append(cmd)
                    continue
            
            try:
                result = subprocess.run(
                    pm_install.split() + [pkg],
                    capture_output=True,
                    timeout=300
                )
                if result.returncode == 0:
                    print(f"{GREEN}Done.{RESET}")
                    installed.append(cmd)
                    already_installed.add(pkg)
                else:
                    print(f"{RED}Failed.{RESET}")
                    failed.append(cmd)
            except Exception:
                print(f"{RED}Failed.{RESET}")
                failed.append(cmd)
        
        # Summary
        print()
        print("---------------------------------------------------")
        print(f"{BOLD}INSTALLATION SUMMARY{RESET}")
        print("---------------------------------------------------")
        
        if installed:
            print(f"Installed:    {GREEN}{' '.join(installed)}{RESET}")
        if failed:
            print(f"Failed:       {RED}{' '.join(failed)}{RESET}")
        if manual:
            print(f"Manual Setup: {MAGENTA}{' '.join(manual)}{RESET}")
        
        if manual or failed:
            print()
            needs_manual = manual + failed
            for dep in needs_manual:
                if dep == "dovi_tool":
                    print("dovi_tool must be installed manually:")
                    if is_arch:
                        print("  AUR:    https://aur.archlinux.org/packages/dovi_tool-bin")
                    print("  GitHub: https://github.com/quietvoid/dovi_tool")
                    print()
                    print("Tip: Install Homebrew (https://brew.sh) - a universal package manager.")
                    print("     Once installed, dovi_convert will use it to install dovi_tool automatically.")
                else:
                    print(f"{dep} must be installed manually using your package manager.")
            
            print()
            print("Please install, then run dovi_convert again.")
            print("---------------------------------------------------")
            sys.exit(1)
        
        print("---------------------------------------------------")
        print(f"{GREEN}All dependencies installed successfully!{RESET}")
        print()


# =============================================================================
# UPDATE CHECKER
# =============================================================================

class UpdateChecker:
    """Handles version checking and update notifications."""
    
    @staticmethod
    def check_background() -> None:
        """Fetch latest version in background thread."""
        def _fetch():
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                req = urllib.request.Request(
                    REPO_URL,
                    headers={"User-Agent": "dovi_convert"}
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read().decode())
                    tag = data.get("tag_name", "")
                    if tag:
                        UPDATE_FILE.write_text(tag)
            except Exception:
                pass
        
        thread = threading.Thread(target=_fetch, daemon=True)
        thread.start()
    
    @staticmethod
    def check_foreground() -> None:
        """Check for updates and display if available (from cache)."""
        if UPDATE_FILE.exists():
            try:
                latest = UPDATE_FILE.read_text().strip()
                if version_gt(latest, VERSION):
                    print(f"{BLUE}---------------------------------------------------{RESET}")
                    print(f"{BOLD}Update Available:{RESET} {GREEN}{latest}{RESET} (Current: v{VERSION})")
                    print("Get it at: https://github.com/cryptochrome/dovi_convert")
                    print(f"{BLUE}---------------------------------------------------{RESET}")
                    print()
            except Exception:
                pass
    
    @staticmethod
    def check_manual() -> None:
        """Manual update check with live fetch."""
        print(f"{BOLD}Checking for updates...{RESET}")
        try:
            req = urllib.request.Request(
                REPO_URL,
                headers={"User-Agent": "dovi_convert"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                latest_tag = data.get("tag_name", "")
        except Exception:
            print(f"{RED}Error: Could not fetch update info from GitHub.{RESET}")
            return
        
        if not latest_tag:
            print(f"{RED}Error: Could not fetch update info from GitHub.{RESET}")
            return
        
        print(f"Latest version on GitHub: {latest_tag}")
        print(f"Installed version:        v{VERSION}")
        
        if version_gt(latest_tag, VERSION):
            print(f"\n{GREEN}Update Available!{RESET}")
            print("Download at: https://github.com/cryptochrome/dovi_convert")
        else:
            print(f"\n{GREEN}You are up to date.{RESET}")


# =============================================================================
# MEDIA TOOL WRAPPER
# =============================================================================

class MediaToolWrapper:
    """Wraps external media tools (ffmpeg, mediainfo, mkvmerge, dovi_tool)."""
    
    def __init__(self, debug_mode: bool = False):
        self.debug_mode = debug_mode
        self.debug_log = Path("dovi_convert_debug.log")
    
    def log(self, msg: str) -> None:
        """Write to debug log if enabled."""
        if self.debug_mode:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(self.debug_log, "a") as f:
                f.write(f"[{timestamp}] {msg}\n")
    
    def run_logged(self, cmd: List[str], cwd: Optional[Path] = None) -> Tuple[int, str, str]:
        """Run command with logging. Returns (returncode, stdout, stderr)."""
        if self.debug_mode:
            self.log(f"--- Command: {' '.join(cmd)} ---")
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=cwd
            )
            
            if self.debug_mode and result.stdout:
                self.log(result.stdout)
            if self.debug_mode and result.stderr:
                self.log(result.stderr)
            
            return result.returncode, result.stdout, result.stderr
        except Exception as e:
            return 1, "", str(e)
    
    def get_video_info(self, filepath: Path) -> VideoInfo:
        """Extract video track information from MKV file."""
        info = VideoInfo()
        
        if not filepath.exists():
            info.mi_info_string = "FILE_NOT_FOUND"
            return info
        
        # 1. Get Track ID & Properties from mkvmerge
        try:
            result = subprocess.run(
                ["mkvmerge", "-J", str(filepath)],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                # mkvmerge returns errors in stdout JSON, not stderr
                error_msg = ""
                try:
                    err_json = json.loads(result.stdout)
                    errors = err_json.get("errors", [])
                    if errors:
                        error_msg = errors[0].strip()
                except Exception:
                    error_msg = result.stderr.strip() if result.stderr else ""
                
                info.mi_info_string = "MKVMERGE_FAIL"
                info.mkvmerge_error = error_msg
                if self.debug_mode:
                    self.log(f"mkvmerge error: {filepath}")
                    self.log(f"Command: mkvmerge -J {filepath}")
                    self.log(f"Exit code: {result.returncode}")
                    self.log(f"Error: {error_msg or '(empty)'}")
                return info
            
            mkv_json = json.loads(result.stdout)
            
            # Find first video track
            for track in mkv_json.get("tracks", []):
                if track.get("type") == "video":
                    info.track_id = track.get("id")
                    props = track.get("properties", {})
                    info.delay = props.get("minimum_timestamp", 0)
                    info.language = props.get("language", "und")
                    info.name = props.get("track_name", "")
                    break
            
            if info.track_id is None:
                info.mi_info_string = "NO_TRACK"
                return info
                
        except Exception as e:
            info.mi_info_string = "MKVMERGE_FAIL"
            info.mkvmerge_error = str(e)
            if self.debug_mode:
                self.log(f"mkvmerge exception: {filepath}")
                self.log(f"Error: {e}")
            return info
        
        # 2. Get Dolby Vision profile from MediaInfo
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=JSON", str(filepath)],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                info.mi_info_string = "MEDIAINFO_FAIL"
                if self.debug_mode:
                    self.log(f"mediainfo error (code {result.returncode}): {result.stderr}")
                
                # Check for WSL Path Limit (Issue #14)
                if is_wsl() and check_wsl_path_limit(filepath):
                    info.mi_info_string = "MEDIAINFO_WSL_LIMIT"
                else:
                    info.mi_info_string = "MEDIAINFO_FAIL"
                return info
            
            # Check for empty output
            if not result.stdout.strip():
                info.mi_info_string = "MEDIAINFO_FAIL"
                if self.debug_mode:
                    self.log("MediaInfo Error: Empty stdout result")
                return info

            mi_json = json.loads(result.stdout)
            
            for track in mi_json.get("media", {}).get("track", []):
                if track.get("@type") == "Video":
                    hdr_format = track.get("HDR_Format", "") or ""
                    hdr_profile = track.get("HDR_Format_Profile", "") or ""
                    codec_id = track.get("CodecID", "") or ""
                    info.mi_info_string = f"{hdr_format} {hdr_profile} {codec_id}"
                    info.fps = track.get("FrameRate", "")
                    try:
                        info.frame_count = int(track.get("FrameCount", 0))
                    except (ValueError, TypeError):
                        info.frame_count = 0
                    break
                    
        except Exception as e:
            info.mi_info_string = "MEDIAINFO_FAIL"
            if self.debug_mode:
                self.log(f"MediaInfo Exception: {e}")
            return info
        
        return info

    
    def get_bl_peak(self, filepath: Path) -> tuple[int, bool]:
        """Get base layer peak brightness (MaxCLL) in nits. Returns (value, is_default)."""
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=Video;%MaxCLL%", str(filepath)],
                capture_output=True,
                text=True
            )
            maxcll_str = result.stdout.strip()
            if maxcll_str and maxcll_str.isdigit():
                maxcll = int(maxcll_str)
                if maxcll >= 100:  # Sanity check
                    return (maxcll, False)
        except Exception as e:
            if self.debug_mode:
                self.log(f"get_bl_peak exception: {e}")
        
        return (1000, True)  # Default when MaxCLL unavailable

    
    def get_duration_ms(self, filepath: Path) -> int:
        """Get video duration in milliseconds."""
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=Video;%Duration%", str(filepath)],
                capture_output=True,
                text=True
            )
            dur_str = result.stdout.strip().split(".")[0]
            return int(dur_str) if dur_str else 0
        except Exception:
            return 0
    
    def get_frame_count(self, filepath: Path) -> Optional[int]:
        """Get video frame count. Returns None if mediainfo fails or returns non-numeric output."""
        raw = ""
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=Video;%FrameCount%", str(filepath)],
                capture_output=True,
                text=True
            )
            raw = result.stdout.strip()
            return int(raw)
        except ValueError:
            raw_display = f'"{raw}"' if raw else "(empty)"
            print(f"{YELLOW}⚠ Frame count unavailable (mediainfo returned: {raw_display}) — verification skipped{RESET}")
            self.log(f"get_frame_count failed for {filepath}: mediainfo returned {raw_display}")
            return None
        except Exception as e:
            self.log(f"get_frame_count failed for {filepath}: {e}")
            return None
    
    def get_fps(self, filepath: Path) -> str:
        """Get video frame rate."""
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=Video;%FrameRate%", str(filepath)],
                capture_output=True,
                text=True
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def get_frame_count_ffprobe(self, filepath: Path) -> int:
        """Get highly accurate frame count using ffprobe stream analysis (Slow)."""
        try:
            # -count_packets reads the actual stream
            cmd = [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-count_packets", "-show_entries", "stream=nb_read_packets",
                "-of", "default=noprint_wrappers=1:nokey=1", str(filepath)
            ]
            
            if self.debug_mode:
                self.log(f"Running strict frame count: {' '.join(cmd)}")
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            return int(result.stdout.strip())
        except Exception as e:
            if self.debug_mode:
                self.log(f"ffprobe frame count failed: {e}")
            return 0


# =============================================================================
# HELP TEXT DISPLAY
# =============================================================================

class HelpText:
    """Handles all help and documentation display."""

    @staticmethod
    def print_usage() -> None:
        """Print quick usage help."""
        print()
        print(f"{BLUE}{BOLD}dovi_convert v{VERSION}{RESET}")
        print()
        print(f"{BOLD}Commands:{RESET}")
        print("  scan [path]           Scan file(s) or directory.")
        print("  inspect [file]        Full RPU structure inspection.")
        print("  convert [file|dir]    Convert files or directories.")
        print("  backup [file]         Create EL backup before conversion.")
        print("  restore [file]        Restore Profile 7 from backup.")
        print("  cleanup               Delete tool backups.")
        print("  update-check          Check for software updates.")
        print("  help                  Show full manual.")
        print()
        print(f"{MAGENTA}Run '{BOLD}dovi_convert help{RESET}{MAGENTA}' for all commands and options.{RESET}")
        print()

    @staticmethod
    def print_help() -> None:
        """Print detailed manual page."""
        help_text = f"""{BOLD}dovi_convert - Dolby Vision Profile 7 -> 8.1 Converter{RESET}

{BOLD}DESCRIPTION{RESET}
  This tool automates the conversion of Dolby Vision Profile 7 MKV files (UHD Blu-ray)
  into Profile 8.1. This ensures compatibility with devices that do not support the
  Enhancement Layer (Apple TV 4K, Shield, etc.), preventing fallback to HDR10.

{BOLD}THE CONVERSION{RESET}
  The conversion process strips the Enhancement Layer (EL) from the video while
  injecting the RPU (dynamic metadata) into the base layer. This creates a
  Profile 8.1 compatible file. All audio and subtitle tracks are preserved.

  {BOLD}Single File{RESET}: Convert individual files with full control.
  {BOLD}Batch Mode{RESET}: Recursively scans directories and batch-converts files.

{BOLD}MODES OF OPERATION{RESET}
  {BOLD}1. Standard Mode (Default){RESET}
     Pipes the video stream directly into the conversion tool.
     Fast, efficient, and requires zero temporary disk space.

  {BOLD}2. Safe Mode (--safe){RESET}
     Extracts the video track to a temporary file on disk, then converts.
     Slower, but robust against files with irregular timestamps or
     'Seamless Branching' structures (common on Disney/Marvel discs).
     The tool will automatically offer this mode if Standard conversion fails.

{BOLD}FILE SCANNING & ANALYSIS{RESET}
  {BOLD}Default Scan{RESET}
    When scanning MKV files, the tool first identifies the video format (HDR10,
    Dolby Vision Profile, etc.). For Profile 7 files with FEL, it analyzes the
    RPU metadata to distinguish between:
    1. {GREEN}Simple FEL / MEL{RESET}: No active brightness expansion detected. Likely safe to convert.
    2. {RED}Complex FEL{RESET}: Expands luminance beyond base layer. Conversion skipped.

  {BOLD}Inspection (Manual){RESET}
    For a definitive analysis, a dedicated inspection mode is available.
    It reads the entire file frame-by-frame to verify whether brightness
    expansion is present in the FEL. Use this to verify Simple FEL verdicts,
    or if you want absolute certainty.

{BOLD}AUTOMATIC BACKUPS{RESET}
  The tool automatically preserves your original file before any modification.
  It uses a specific naming convention to distinguish its backups from your own files.
  Backup File: {BLUE}[filename].mkv.bak.dovi_convert{RESET}

{BOLD}KNOWN LIMITATION: Single Video Track{RESET}
  The {BOLD}converted{RESET} file will contain exactly one video track (the main movie).
  Any secondary video streams (e.g., Picture-in-Picture commentary or Multi-Angle views)
  will be dropped because the conversion process isolates the main video track.

  {BOLD}No Risk of Data Loss:{RESET} Your original source file (containing all tracks)
  is automatically preserved as a backup. You can restore it if needed.

{BOLD}COMMANDS{RESET}

  {BOLD}scan [path] [path2] ...{RESET}
       Scan files or directories to identify format and conversion candidates.
       Detects HDR formats and analyzes Profile 7 files for FEL complexity.

       If no path is given, scans all MKV files in the current directory.

       Options:
          {BOLD}-r, --recursive [depth]{RESET}   Scan subdirectories. Default depth: 5.
          {BOLD}--candidates{RESET}              Show only conversion candidates (Profile 7).

  {BOLD}convert [file|dir] [file2|dir2] ...{RESET}
       Converts file(s) or directory contents to Profile 8.1.
       Skips 'Complex FEL' files to prevent data loss.
       The original file is NOT deleted; it is renamed to *.mkv.bak.dovi_convert.

       When files are specified: Converts them directly (inline mode).
       When directories are specified: Pre-scans all files, shows summary,
       then converts with confirmation (batch mode).

       Options:
          {BOLD}--hdr10{RESET}                   Convert to HDR10 instead of DoVi Profile 8.1.
                                    (Only available for single file conversions.)
          {BOLD}-b, --backup{RESET}              Create EL backup before conversion.
                                    Allows restoring to Profile 7 later.
          {BOLD}-f, --force{RESET}               Force convert 'Complex FEL' files.
          {BOLD}-t, --temp [path]{RESET}         Write temp files to a faster drive.
          {BOLD}-o, --output [path]{RESET}       Output directory for converted files.

       Additional options for directories:
          {BOLD}-r, --recursive [depth]{RESET}   Scan subdirectories. Default depth: 5.
          {BOLD}-y, --yes{RESET}                 Skip prompts, skip Simple FEL files.
          {BOLD}-v, --verbose{RESET}             Show detailed per-file output (default is compact).
          {BOLD}--include-simple{RESET}          Allow auto-conversion of Simple FEL in Auto-Yes mode.
          {BOLD}--delete{RESET}                  Auto-delete backups after successful conversion.

  {BOLD}inspect [file]{RESET}
       Full frame-by-frame inspection of brightness metadata.
       Verifies whether the FEL contains active brightness expansion.
       Use this to verify Simple FEL verdicts, or if you want absolute certainty.

       Note: Reads entire file. Slower than the default scan.

       Options:
          {BOLD}-s, --safe{RESET}   Force Safe Mode (Disk Extraction fallback).

  {BOLD}backup [file]{RESET}
       Creates a compact backup of the Enhancement Layer (EL) before conversion.
       The backup archive ({BLUE}*.dovi{RESET}) is ~10-15% of the original file size.
       Use this to preserve restoration capability while freeing disk space.

       {BOLD}Single file only.{RESET} Use --backup with convert for batch operations.

       Options:
          {BOLD}-t, --temp [path]{RESET}     Write temp files to a faster drive.
          {BOLD}-o, --output [path]{RESET}   Output directory for the .dovi archive.

  {BOLD}restore [file]{RESET}
       Restores a Profile 7 FEL file from a P8.1 or HDR10 file + its .dovi backup.
       Output: {BLUE}[filename].restored.mkv{RESET}

       {BOLD}Single file only.{RESET} The backup archive must exist in the same directory
       as the input file, or be specified via --source.

       Options:
          {BOLD}--source [path]{RESET}       Path to the .dovi backup archive.
          {BOLD}-t, --temp [path]{RESET}     Write temp files to a faster drive.
          {BOLD}-o, --output [path]{RESET}   Output directory for the restored file.

  {BOLD}cleanup{RESET}
       Scans for and deletes {BLUE}*.mkv.bak.dovi_convert{RESET} files in the current directory.
       {BOLD}Safety Check:{RESET} Checks if 'Parent' MKV exists before deleting orphan backups.

       Options:
          {BOLD}-r, --recursive{RESET}   Recursive scan.
          {BOLD}-y, --yes{RESET}         Skip confirmation prompts.

  {BOLD}update-check{RESET}
       Checks if a newer version of dovi_convert is available.

{BOLD}OPTION DETAILS{RESET}

  {BOLD}-f, --force{RESET} (convert)
       {RED}Force Conversion.{RESET}
       Overrides the 'Complex FEL' detection. Use this if you want to convert
       a Complex FEL file despite the potential loss of brightness data.

  {BOLD}--include-simple{RESET} (convert)
       {MAGENTA}Auto-Include Simple FEL.{RESET}
       When using --yes (Auto-Yes), Simple FEL files are normally skipped to allow
       manual review. This flag includes them when converting directories.

  {BOLD}-s, --safe{RESET} (convert)
       {MAGENTA}Force Safe Mode (Extraction).{RESET}
       Forces extraction of the video track to disk before converting.
       This is the robust fallback method usually triggered automatically on error,
       but you can force it manually here for known problematic files.

  {BOLD}--delete{RESET} (convert)
       {RED}Auto-Delete Mode.{RESET}
       Automatically deletes the backup (Original Source) file immediately
       after a successful conversion and verification.
       Use this for large batches where you don't have disk space to store backups.

  {BOLD}-b, --backup{RESET} (convert)
       {CYAN}EL Backup Mode.{RESET}
       Creates a compact backup of the Enhancement Layer before conversion.
       The .dovi archive (~10-15% of original) allows restoring to Profile 7 later.
       Combine with --delete to free disk space while keeping restoration capability.

  {BOLD}--debug{RESET} (global)
       {MAGENTA}Debug Mode.{RESET}
       Generates a 'dovi_convert_debug.log' file in the current directory
       containing full ffmpeg/dovi_tool output. Essential for troubleshooting.

  {BOLD}-y, --yes{RESET} (convert, cleanup)
       {MAGENTA}Auto-Yes Mode.{RESET}
       Automatically answers 'Yes' to confirmation prompts.
       Simple FEL files are skipped unless --include-simple is also used.

  {BOLD}-t, --temp [path]{RESET} (convert)
       {MAGENTA}Temp Directory.{RESET}
       Write temporary files to a separate directory.
       Use this when source files are on slow storage (HDD, NAS).
       The temp directory should be on a fast drive (SSD/NVMe).

  {BOLD}-o, --output [path]{RESET} (convert)
       {MAGENTA}Output Directory.{RESET}
       Place converted files in specified directory.

       Files:       Placed directly in output directory.
       Directories: Basename of source preserved, subdirectories mirrored.

  {BOLD}--hdr10{RESET} (convert, files only)
       {MAGENTA}HDR10 Mode.{RESET}
       Converts to HDR10 (with HDR10+ metadata, if available in the source)
       instead of DoVi Profile 8.1. Read docs for use cases.

       Not available when converting directories.
"""
        # Use pager if available and stdout is a tty
        if shutil.which("less") and sys.stdout.isatty():
            try:
                proc = subprocess.Popen(
                    ["less", "-R"],
                    stdin=subprocess.PIPE,
                    text=True
                )
                proc.communicate(input=help_text)
                return
            except Exception:
                pass
        print(help_text)


# =============================================================================
# BACKUP MANAGER
# =============================================================================

class BackupManager:
    """Handles EL backup creation and restoration for Dolby Vision files."""

    ARCHIVE_EXTENSION = ".dovi"
    RESTORED_SUFFIX = ".restored"
    EL_FILENAME = "el.hevc"

    def __init__(self, app: 'DoviConvertApp'):
        self.app = app
        self.media = app.media
        self.config = app.config

    def _get_archive_path(self, filepath: Path) -> Path:
        """Determine archive output path based on config."""
        archive_name = filepath.stem + self.ARCHIVE_EXTENSION
        if self.config.output_dir:
            return self.config.output_dir / archive_name
        return filepath.parent / archive_name

    def _get_temp_path(self, filename: str, source_dir: Path) -> Path:
        """Get path for a temporary file, using --temp if specified, otherwise source directory."""
        if self.config.temp_dir:
            path = self.config.temp_dir / filename
        else:
            path = source_dir / filename
        self.app.temp_files.append(path)  # Register with app for Ctrl-C cleanup
        return path

    def locate_backup(self, filepath: Path) -> Optional[Path]:
        """Find .dovi archive for a given file.

        Search order:
        1. --source path if specified
        2. Same directory as the input file
        """
        # Check --source first
        if self.config.backup_source:
            source = Path(self.config.backup_source)
            if source.exists():
                return source
            return None

        # Check same directory
        archive_path = filepath.parent / (filepath.stem + self.ARCHIVE_EXTENSION)
        if archive_path.exists():
            return archive_path

        return None

    def verify_backup(self, archive_path: Path) -> Tuple[bool, str]:
        """Validate archive structure and contents.

        Returns (is_valid, error_message).
        """
        if not archive_path.exists():
            return False, f"Archive not found: {archive_path}"

        try:
            with tarfile.open(archive_path, "r") as tar:
                names = tar.getnames()
                if self.EL_FILENAME not in names:
                    return False, f"Archive missing required file: {self.EL_FILENAME}"
        except tarfile.TarError as e:
            return False, f"Invalid archive format: {e}"
        except Exception as e:
            return False, f"Failed to read archive: {e}"

        return True, ""

    def create_backup(self, filepath: Path) -> Tuple[int, Optional[Path], str]:
        """Extract EL and create .dovi archive.

        Args:
            filepath: Path to the Profile 7 MKV file

        Returns:
            (status, archive_path, error_message)
            status: 0=success, 1=error
        """
        archive_path = self._get_archive_path(filepath)

        # Check if archive already exists
        if archive_path.exists():
            return 1, None, f"Backup already exists: {archive_path}"

        # Temp file for EL extraction
        el_temp = self._get_temp_path(f"{filepath.stem}_el.hevc", filepath.parent)

        try:
            # Extract EL via ffmpeg | dovi_tool demux pipeline
            # ffmpeg extracts HEVC stream, dovi_tool extracts just the EL
            ffmpeg_cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", str(filepath),
                "-map", "0:v:0",
                "-c:v", "copy",
                "-bsf:v", "hevc_mp4toannexb",
                "-f", "hevc", "-"
            ]

            dovi_cmd = ["dovi_tool", "demux", "--el-only", "-e", str(el_temp), "-"]

            if self.media.debug_mode:
                self.media.log(f"Backup: {' '.join(ffmpeg_cmd)} | {' '.join(dovi_cmd)}")

            ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            self.app.active_procs.append(ffmpeg_proc)

            dovi_proc = subprocess.Popen(
                dovi_cmd,
                stdin=ffmpeg_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            self.app.active_procs.append(dovi_proc)

            # Allow ffmpeg to receive SIGPIPE
            ffmpeg_proc.stdout.close()

            _, dovi_stderr = dovi_proc.communicate()
            _, ffmpeg_stderr = ffmpeg_proc.communicate()

            self.app.active_procs.clear()

            # Check for user abort
            if ffmpeg_proc.returncode == -2 or dovi_proc.returncode == 130 or self.app.abort_requested:
                self.app._cleanup()
                return 130, None, "Aborted"

            if ffmpeg_proc.returncode != 0:
                stderr_text = ffmpeg_stderr.decode() if ffmpeg_stderr else ""
                self.app._cleanup()
                return 1, None, f"FFmpeg failed: {stderr_text}"

            if dovi_proc.returncode != 0:
                stderr_text = dovi_stderr.decode() if dovi_stderr else ""
                self.app._cleanup()
                return 1, None, f"dovi_tool demux failed: {stderr_text}"

            if not el_temp.exists() or el_temp.stat().st_size == 0:
                self.app._cleanup()
                return 1, None, "EL extraction produced empty output"

            # Package into uncompressed TAR
            try:
                with tarfile.open(archive_path, "w") as tar:
                    tar.add(el_temp, arcname=self.EL_FILENAME)
            except Exception as e:
                self.app._cleanup()
                return 1, None, f"Failed to create archive: {e}"

            self.app._cleanup()
            return 0, archive_path, ""

        except Exception as e:
            self.app._cleanup()
            return 1, None, f"Backup failed: {e}"

    def _has_dolby_vision(self, filepath: Path) -> bool:
        """Check if file has Dolby Vision metadata (P8.1) vs plain HDR10."""
        info = self.media.get_video_info(filepath)
        # P8.1 files have DoVi metadata, HDR10 does not
        return "Dolby Vision" in info.mi_info_string or "dvhe" in info.mi_info_string.lower()

    def restore_backup(self, filepath: Path) -> Tuple[int, Optional[Path], str]:
        """Restore Profile 7 from P8.1/HDR10 + .dovi archive.

        Args:
            filepath: Path to the P8.1 or HDR10 MKV file to restore

        Returns:
            (status, restored_path, error_message)
            status: 0=success, 1=error
        """
        # Locate backup
        archive_path = self.locate_backup(filepath)
        if not archive_path:
            search_loc = filepath.parent / (filepath.stem + self.ARCHIVE_EXTENSION)
            return 1, None, f"No backup found. Expected: {search_loc}"

        # Verify backup integrity
        is_valid, error = self.verify_backup(archive_path)
        if not is_valid:
            return 1, None, error

        # Determine output path
        restored_name = filepath.stem + self.RESTORED_SUFFIX + ".mkv"
        if self.config.output_dir:
            restored_path = self.config.output_dir / restored_name
        else:
            restored_path = filepath.parent / restored_name

        if restored_path.exists():
            return 1, None, f"Restored file already exists: {restored_path}"

        # Temp files (registered with app.temp_files via _get_temp_path for Ctrl-C cleanup)
        source_dir = filepath.parent
        bl_temp = self._get_temp_path(f"{filepath.stem}_bl.hevc", source_dir)
        bl_clean = self._get_temp_path(f"{filepath.stem}_bl_clean.hevc", source_dir)
        el_temp = self._get_temp_path(f"{filepath.stem}_el.hevc", source_dir)
        restored_hevc = self._get_temp_path(f"{filepath.stem}_restored.hevc", source_dir)

        try:
            # Step 1: Extract BL from current file
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(filepath),
                "-map", "0:v:0",
                "-c:v", "copy",
                "-bsf:v", "hevc_mp4toannexb",
                "-f", "hevc", str(bl_temp)
            ]

            if self.media.debug_mode:
                self.media.log(f"Restore BL extract: {' '.join(ffmpeg_cmd)}")

            proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.app.active_procs.append(proc)
            _, stderr = proc.communicate()
            if proc in self.app.active_procs:
                self.app.active_procs.remove(proc)

            if self.app.abort_requested:
                self.app._cleanup()
                return 130, None, "Aborted"

            if proc.returncode != 0:
                self.app._cleanup()
                return 1, None, f"Failed to extract base layer: {stderr.decode()}"

            # Step 2: Detect if P8.1 and sanitize BL
            has_dovi = self._has_dolby_vision(filepath)

            if has_dovi:
                # P8.1 has injected RPU that must be stripped
                dovi_remove_cmd = ["dovi_tool", "remove", str(bl_temp), "-o", str(bl_clean)]

                if self.media.debug_mode:
                    self.media.log(f"Restore RPU strip: {' '.join(dovi_remove_cmd)}")

                proc = subprocess.Popen(dovi_remove_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.app.active_procs.append(proc)
                _, stderr = proc.communicate()
                if proc in self.app.active_procs:
                    self.app.active_procs.remove(proc)

                if self.app.abort_requested:
                    self.app._cleanup()
                    return 130, None, "Aborted"

                if proc.returncode != 0:
                    self.app._cleanup()
                    return 1, None, f"Failed to strip RPU from base layer: {stderr.decode()}"
                # Delete original BL now that we have the cleaned version - saves ~48GB disk space
                try:
                    bl_temp.unlink()
                    if bl_temp in self.app.temp_files:
                        self.app.temp_files.remove(bl_temp)
                    if self.media.debug_mode:
                        self.media.log(f"Deleted intermediate file early: {bl_temp.name}")
                except Exception:
                    pass  # Non-fatal if deletion fails
                bl_for_mux = bl_clean
            else:
                # HDR10 - use BL directly
                bl_for_mux = bl_temp

            # Step 3: Unpack archive to get EL
            try:
                with tarfile.open(archive_path, "r") as tar:
                    # Extract el.hevc to temp location
                    member = tar.getmember(self.EL_FILENAME)
                    with tar.extractfile(member) as src:
                        with open(el_temp, "wb") as dst:
                            shutil.copyfileobj(src, dst, length=1 << 20)
            except Exception as e:
                self.app._cleanup()
                return 1, None, f"Failed to extract EL from archive: {e}"

            # Step 4: Verify frame counts match
            bl_frames = self.media.get_frame_count(filepath)
            # For EL, we trust it was created from the same source
            # A full verification would require parsing HEVC NAL units

            # Step 5: Rebuild FEL with dovi_tool mux
            mux_cmd = [
                "dovi_tool", "mux",
                "--bl", str(bl_for_mux),
                "--el", str(el_temp),
                "-o", str(restored_hevc)
            ]

            if self.media.debug_mode:
                self.media.log(f"Restore FEL mux: {' '.join(mux_cmd)}")

            proc = subprocess.Popen(mux_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.app.active_procs.append(proc)
            _, stderr = proc.communicate()
            if proc in self.app.active_procs:
                self.app.active_procs.remove(proc)

            if self.app.abort_requested:
                self.app._cleanup()
                return 130, None, "Aborted"

            if proc.returncode != 0:
                self.app._cleanup()
                return 1, None, f"Failed to rebuild FEL: {stderr.decode()}"

            # Step 6: Final mux with mkvmerge (video from restored, everything else from original)
            mkvmerge_cmd = [
                "mkvmerge", "-o", str(restored_path),
                str(restored_hevc),
                "--no-video", str(filepath)
            ]

            if self.media.debug_mode:
                self.media.log(f"Restore final mux: {' '.join(mkvmerge_cmd)}")

            proc = subprocess.Popen(mkvmerge_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.app.active_procs.append(proc)
            _, stderr = proc.communicate()
            if proc in self.app.active_procs:
                self.app.active_procs.remove(proc)

            if self.app.abort_requested:
                self.app._cleanup()
                return 130, None, "Aborted"

            if proc.returncode not in (0, 1):  # mkvmerge returns 1 for warnings
                self.app._cleanup()
                return 1, None, f"Failed to create restored MKV: {stderr.decode()}"

            # Verify restored file
            if not restored_path.exists() or restored_path.stat().st_size == 0:
                self.app._cleanup()
                return 1, None, "Restored file is empty or missing"

            # Verify frame count of restored file
            restored_frames = self.media.get_frame_count(restored_path)
            if bl_frames is not None and restored_frames is not None and abs(bl_frames - restored_frames) > 1:
                # Frame count mismatch - warn but don't fail
                print(f"\n{YELLOW}⚠ Warning: Frame count mismatch (original: {bl_frames}, restored: {restored_frames}){RESET}")
                print(f"  The restored file may have issues. Verify playback before deleting the original.")
                if self.media.debug_mode:
                    self.media.log(f"Frame count mismatch: original={bl_frames}, restored={restored_frames}")

            self.app._cleanup()
            return 0, restored_path, ""

        except Exception as e:
            self.app._cleanup()
            return 1, None, f"Restore failed: {e}"


# =============================================================================
# MAIN APPLICATION CLASS
# =============================================================================

class DoviConvertApp:
    """Main application controller."""
    
    def __init__(self, config: Config):
        self.config = config
        
        # Check for Native Windows (Not Supported)
        if os.name == 'nt':
            print(f"{RED}Error: Native Windows is not officially supported.{RESET}")
            print(f"       Please use {BOLD}WSL2 (Windows Subsystem for Linux){RESET} or {BOLD}Docker{RESET}.")
            sys.exit(1)
        
        self.media = MediaToolWrapper(debug_mode=config.debug_mode)
        self.batch_running = False
        self.abort_requested = False
        self.current_command: Optional[str] = None

        # Current file state
        self.video_info: Optional[VideoInfo] = None
        self.scan_result: Optional[ScanResult] = None
        self.dovi_status = ""
        self.action = ""
        
        # Temp files to cleanup
        self.temp_files: List[Path] = []

        # Active subprocesses for clean abort
        self.active_procs: List[subprocess.Popen] = []

        # Active spinner for clean abort
        self.active_spinner: Optional[Spinner] = None

        # Last failure reason (for compact batch mode error tracking)
        self.last_fail_reason: str = ""

        # Track backup archives created during session (for abort message)
        self.backup_archives_created: List[Path] = []

        self._backup_archive_path: Optional[Path] = None

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
    
    def _handle_signal(self, signum: int, frame) -> None:
        """Handle interrupt signals."""
        self.abort_requested = True
        # Stop active spinner first to prevent output after our messages
        if self.active_spinner:
            self.active_spinner.stop()
            self.active_spinner = None
        # Terminate active subprocesses to unblock communicate()
        for proc in self.active_procs:
            try:
                proc.terminate()
            except Exception:
                pass
        for proc in self.active_procs:
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.active_procs.clear()
        self._cleanup()
        sys.stdout.write("\033[?25h")  # Ensure cursor visible
        # In batch mode, return to let the batch loop print its summary
        if self.batch_running:
            return

        # Context-appropriate interrupt messages
        cmd = self.current_command
        if cmd == "convert":
            print(f"\n{MAGENTA}[!] Process Interrupted by User.{RESET}")
            print(f"{MAGENTA}[!] Cleaning up temporary files... Done.{RESET}")
            print(f"{GREEN}[✓] Original Source file is safe and untouched.{RESET}")
            if self.backup_archives_created:
                if len(self.backup_archives_created) == 1:
                    print(f"{CYAN}[i] Backup preserved: {self.backup_archives_created[0].name}{RESET}")
                else:
                    print(f"{CYAN}[i] Backup(s) preserved: {len(self.backup_archives_created)} .dovi archive(s){RESET}")
        elif cmd == "backup":
            print(f"\nBackup cancelled.")
        elif cmd == "restore":
            print(f"\nRestore cancelled. Original file unchanged.")
        elif cmd == "scan":
            print(f"\nScan cancelled.")
        elif cmd == "inspect":
            print(f"\nInspect cancelled.")
        elif cmd == "cleanup":
            print(f"\nCleanup cancelled.")
        else:
            print(f"\nCancelled.")
        sys.exit(130)
    
    def _cleanup(self) -> None:
        """Clean up temporary files."""
        for tf in self.temp_files:
            try:
                if tf.exists():
                    tf.unlink()
            except Exception:
                pass
        self.temp_files.clear()

    def check_fel_complexity(self, filepath: Path) -> ScanResult:
        """Analyze RPU to detect Complex FEL."""
        result = ScanResult()
        
        # 1. Determine probe points
        duration_ms = self.media.get_duration_ms(filepath)
        
        if duration_ms < 10000:
            timestamps = [0]
        else:
            dur_sec = duration_ms // 1000
            # Probe at 10 points (5% to 95%)
            timestamps = [
                int(dur_sec * 0.05), int(dur_sec * 0.15), int(dur_sec * 0.25),
                int(dur_sec * 0.35), int(dur_sec * 0.45), int(dur_sec * 0.55),
                int(dur_sec * 0.65), int(dur_sec * 0.75), int(dur_sec * 0.85),
                int(dur_sec * 0.95)
            ]
        
        # 2. Get base layer peak
        bl_peak, _ = self.media.get_bl_peak(filepath)
        threshold = bl_peak + 50
        
        if self.config.debug_mode:
            self.media.log(f"[Scan Debug] Base Layer Peak: {bl_peak} nits (Threshold: {threshold})")
        
        complex_signal = False
        probe_count = 0
        
        
        for t in timestamps:
            if self.abort_requested:
                break
            
            # Create temp files
            temp_hevc = filepath.parent / f"probe_{t}_{int(time.time())}_{os.getpid()}.hevc"
            temp_rpu = temp_hevc.with_suffix(".rpu")
            temp_json = temp_hevc.with_suffix(".json")
            
            self.temp_files.extend([temp_hevc, temp_rpu, temp_json])
            
            # Extract 1 second of HEVC
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-v", "error" if not self.config.debug_mode else "info",
                "-analyzeduration", "100M", "-probesize", "100M",
                "-ss", str(t), "-i", str(filepath),
                "-map", "0:v:0", "-c:v", "copy", "-an", "-sn", "-dn",
                "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-t", "1",
                str(temp_hevc)
            ]
            
            try:
                result_proc = subprocess.run(
                    ffmpeg_cmd,
                    capture_output=True,
                    stdin=subprocess.DEVNULL
                )

            except Exception as e:
                if self.config.debug_mode:
                    self.media.log(f"probe ffmpeg exception @ {t}s: {e}")
                continue

            
            if not temp_hevc.exists() or temp_hevc.stat().st_size == 0:
                self._cleanup_probe_files([temp_hevc, temp_rpu, temp_json])
                continue
            
            # Extract RPU
            try:
                subprocess.run(
                    ["dovi_tool", "extract-rpu", str(temp_hevc), "-o", str(temp_rpu)],
                    capture_output=True
                )
            except Exception as e:
                if self.config.debug_mode:
                    self.media.log(f"probe dovi_tool extract-rpu exception: {e}")
            
            temp_hevc.unlink(missing_ok=True)
            
            if not temp_rpu.exists() or temp_rpu.stat().st_size == 0:
                self._cleanup_probe_files([temp_rpu, temp_json])
                continue
            
            # Export to JSON (use cwd + relative paths to avoid comma parsing issues)
            try:
                subprocess.run(
                    ["dovi_tool", "export", "-i", temp_rpu.name, "-d", f"all={temp_json.name}"],
                    capture_output=True,
                    cwd=filepath.parent
                )
            except Exception as e:
                if self.config.debug_mode:
                    self.media.log(f"probe dovi_tool export exception: {e}")
            
            temp_rpu.unlink(missing_ok=True)
            
            if not temp_json.exists() or temp_json.stat().st_size == 0:
                temp_json.unlink(missing_ok=True)
                continue
            
            probe_count += 1
            
            # Check for MEL
            try:
                json_content = temp_json.read_text()
                if '"el_type":"MEL"' in json_content:
                    result.verdict = "SAFE"
                    result.reason = "Minimal Enhancement Layer (MEL) Detected"
                    temp_json.unlink(missing_ok=True)
                    return result
                
                # Extract L1 max using simple parsing (no jq needed in Python)
                l1_max = self._extract_l1_max(json_content)
                
                if self.config.debug_mode:
                    self.media.log(f"[Scan Debug] Probe @ {t}s : L1 Raw={l1_max}")
                
                if l1_max is not None:
                    l1_nits = pq_to_nits(l1_max)
                    
                    if self.config.debug_mode:
                        self.media.log(f"[Scan Debug] Probe @ {t}s : L1={l1_max} -> {l1_nits} nits vs Threshold={threshold}")
                    
                    if l1_nits > threshold:
                        complex_signal = True
                        result.reason = f"Active Reconstruction (L1: {l1_nits} nits > BL: {bl_peak} nits @ {t}s)"
                        temp_json.unlink(missing_ok=True)
                        break
                        
            except Exception as e:
                if self.config.debug_mode:
                    self.media.log(f"[Scan Debug] Probe @ {t}s : Error parsing JSON: {e}")
            
            temp_json.unlink(missing_ok=True)
        
        if probe_count == 0:
            result.reason = "Extraction failed (No probes succeeded)"
            result.verdict = "COMPLEX"  # Default to Complex if we can't read it
            return result
        
        # Require at least 50% of probes to succeed for reliable verdict
        # (Only applies when we haven't already detected Complex - early break is fine)
        min_required = max(1, len(timestamps) // 2)
        if not complex_signal and probe_count < min_required:
            if self.config.debug_mode:
                self.media.log(f"Probe threshold not met: {probe_count}/{len(timestamps)} succeeded, {min_required} required")
            result.reason = f"Insufficient data ({probe_count}/{len(timestamps)} probes succeeded)"
            result.verdict = "COMPLEX"  # Default to Complex if data is unreliable
            return result
        
        if complex_signal:
            result.verdict = "COMPLEX"
        else:
            result.verdict = "SAFE"
            result.reason = "Static / Simple FEL (Safe to Convert)"
        
        return result
    
    def _extract_l1_max(self, json_content: str) -> Optional[int]:
        """Extract max L1 value from RPU JSON."""
        try:
            data = json.loads(json_content)
            max_vals = []
            
            find_l1_values(data, max_vals)
            return max(max_vals) if max_vals else None
        except Exception:
            return None

    def _extract_l1_stream(self, json_path: Path) -> List[int]:
        """
        Stream parser for RPU JSON. 
        Extracts 'max_pq' values line-by-line using low memory.
        """
        max_vals = []
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if "max_pq" in line:
                        # Expected format: "max_pq": 1234, or similar
                        # We use simple splitting to be robust against whitespace
                        try:
                            parts = line.split(":")
                            if len(parts) >= 2:
                                # Get the part after the colon, strip comma and whitespace
                                val_str = parts[1].strip().rstrip(",")
                                max_vals.append(int(val_str))
                        except (ValueError, IndexError):
                            continue
        except Exception:
            return []
        
        return max_vals

    
    def _cleanup_probe_files(self, files: List[Path]) -> None:
        """Clean up specific probe files."""
        for f in files:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass
    
    def determine_action(self, filepath: Path) -> Tuple[str, str]:
        """Determine action based on video info and FEL analysis."""
        info = self.video_info
        if info is None:
            return (f"{RED}Error{RESET}", "ERROR")
        
        mi = info.mi_info_string
        
        if mi == "FILE_NOT_FOUND":
            return (f"{RED}File not found{RESET}", "ERROR")
        
        if mi == "NO_TRACK":
            return (f"{RED}No Video Track{RESET}", "SKIP")
        
        if mi == "MKVMERGE_FAIL":
            if info.mkvmerge_error:
                err_detail = info.mkvmerge_error.split('\n')[0][:60]
                return (f"{RED}mkvmerge: {err_detail}{RESET}", "ERROR")
            else:
                return (f"{RED}mkvmerge: failed (no details, use -debug){RESET}", "ERROR")

        if mi == "MEDIAINFO_FAIL":
            return (f"{RED}Error: MediaInfo failed (Check file inputs or installation){RESET}", "ERROR")

        if mi == "MEDIAINFO_WSL_LIMIT":
            return (f"{RED}Error: WSL limit reached. File path > 255 chars on Windows drive.{RESET}", "ERROR")
        
        # Decision matrix
        if "dvhe.07" in mi or "Profile 7" in mi:
            # Profile 7 detected - run FEL analysis
            self.scan_result = self.check_fel_complexity(filepath)
            
            if self.scan_result.verdict == "COMPLEX":
                if "Insufficient data" in self.scan_result.reason or "Extraction failed" in self.scan_result.reason:
                    status = f"{RED}DV Profile 7 (Scan Failed){RESET}"
                    action = f"{RED}SKIP (error){RESET}"
                elif self.config.force_mode:
                    status = f"{RED}DV Profile 7 FEL (Complex){RESET}"
                    action = f"{RED}CONVERT (FORCED){RESET}"
                else:
                    status = f"{RED}DV Profile 7 FEL (Complex){RESET}"
                    action = f"{RED}SKIP{RESET}"
            elif self.scan_result.verdict == "SAFE":
                if "MEL" in self.scan_result.reason:
                    status = f"{GREEN}DV Profile 7 MEL (Safe){RESET}"
                    action = f"{GREEN}CONVERT{RESET}"
                else:
                    status = f"{BLUE}DV Profile 7 FEL (Simple){RESET}"
                    action = f"{BLUE}CONVERT*{RESET}"
            else:
                status = f"{MAGENTA}DV Profile 7 (Check Failed){RESET}"
                action = f"{MAGENTA}MANUAL CHECK{RESET}"
            
            return (status, action)
        
        elif "dvhe.08" in mi or "Profile 8" in mi:
            return (f"{DEFAULT}DV Profile 8.1{RESET}", "IGNORE")
        elif "dvhe.05" in mi or "Profile 5" in mi:
            return (f"{MAGENTA}DV Profile 5 (Stream){RESET}", "IGNORE")
        elif "Dolby Vision" in mi:
            return (f"{MAGENTA}DV Unknown Profile{RESET}", "IGNORE")
        else:
            # Granular HDR detection
            if "2094" in mi:
                return (f"{DEFAULT}HDR10+{RESET}", "IGNORE")
            elif "HLG" in mi or "Hybrid Log Gamma" in mi:
                return (f"{DEFAULT}HLG{RESET}", "IGNORE")
            elif "2086" in mi or "HDR10" in mi:
                return (f"{DEFAULT}HDR10{RESET}", "IGNORE")
            else:
                return (f"{DEFAULT}SDR{RESET}", "IGNORE")
    
    def analyze_file(self, filepath: Path) -> None:
        """Full analysis: get video info and determine action."""
        self.video_info = self.media.get_video_info(filepath)
        self.dovi_status, self.action = self.determine_action(filepath)

    
    def print_metrics(self, final_file: Path, frame_count: int, start_time: float, orig_size: int) -> None:
        """Print conversion metrics."""
        duration = int(time.time() - start_time)
        minutes = duration // 60
        seconds = duration % 60
        
        final_size = get_file_size(final_file)
        size_diff = orig_size - final_size
        if size_diff < 0:
            size_diff = 0
        
        orig_gb = human_size_gb(orig_size)
        final_gb = human_size_gb(final_size)
        
        # Dynamic unit for diff
        if size_diff >= 1073741824:
            diff_disp = f"{size_diff / 1024 / 1024 / 1024:.2f} GB"
        else:
            diff_disp = f"{size_diff / 1024 / 1024:.2f} MB"
        
        fps = frame_count // duration if duration > 0 else 0
        
        print("---------------------------------------------------")
        print(f"             {BOLD}CONVERSION METRICS{RESET}")
        print("---------------------------------------------------")
        print(f"Time Taken:    {minutes}m {seconds:02d}s")
        print(f"Orig Size:     {orig_gb}")
        print(f"Final Size:    {final_gb}")
        print(f"EL Discarded:  {diff_disp} (Space Saved)")
        print(f"Avg Speed:     {fps} fps")
        print("---------------------------------------------------")
    
    def convert_turbo(self, input_file: Path, output_file: Path) -> Tuple[int, str]:
        """Standard conversion using pipe mode. Returns (status, error_type)."""
        self._total_steps = 3
        spinner = Spinner("[1/3] Converting... ")
        self.active_spinner = spinner
        spinner.start()
        
        ffmpeg_stderr = b""
        dovi_stderr = b""
        
        try:
            # Create the pipe command
            ffmpeg_proc = subprocess.Popen(
                ["ffmpeg", "-y", "-v", "error", "-i", str(input_file),
                 "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            self.active_procs.append(ffmpeg_proc)

            # Choose dovi_tool command based on mode
            if self.config.hdr10_mode:
                dovi_cmd = ["dovi_tool", "remove", "-", "-o", str(output_file)]
            else:
                dovi_cmd = ["dovi_tool", "-m", "2", "convert", "--discard", "-", "-o", str(output_file)]

            dovi_proc = subprocess.Popen(
                dovi_cmd,
                stdin=ffmpeg_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            self.active_procs.append(dovi_proc)

            # Allow ffmpeg_proc to receive SIGPIPE
            ffmpeg_proc.stdout.close()

            _, dovi_stderr = dovi_proc.communicate()
            _, ffmpeg_stderr = ffmpeg_proc.communicate()

            ffmpeg_status = ffmpeg_proc.returncode
            dovi_status = dovi_proc.returncode

        except Exception as e:
            self.active_procs.clear()
            spinner.stop()
            self.active_spinner = None
            return (1, "UNKNOWN")

        self.active_procs.clear()

        spinner.stop()
        self.active_spinner = None
        
        # Check BOTH return codes - critical for catching hidden pipe errors
        if ffmpeg_status != 0:
            # ffmpeg failed - the data sent to dovi_tool may be incomplete/corrupt
            output_file.unlink(missing_ok=True)
            if self.config.debug_mode:
                self.media.log(f"ffmpeg failed with code {ffmpeg_status}: {ffmpeg_stderr.decode()}")
            
            stderr_text = ffmpeg_stderr.decode() if ffmpeg_stderr else ""
            if any(err in stderr_text for err in ["No space left on device", "Permission denied", "Read-only file system"]):
                print(stderr_text)
                return (1, "CRITICAL")
            
            # Treat any ffmpeg failure as a stream error
            return (1, "STREAM_ERROR")
        
        if dovi_status == 0:
            print(f"\r\033[K[1/3] Converting... Done.")
            return (0, "")
        
        # Check for user abort
        if dovi_status == 130 or self.abort_requested:
            output_file.unlink(missing_ok=True)
            return (130, "")
        
        output_file.unlink(missing_ok=True)
        
        # Error classification from dovi_tool
        stderr_text = dovi_stderr.decode() if dovi_stderr else ""
        
        if self.config.debug_mode:
            self.media.log(f"dovi_tool error (code {dovi_status}): {stderr_text or '(empty)'}")
        
        if any(err in stderr_text for err in ["No space left on device", "Permission denied", "Read-only file system"]):
            print(stderr_text)
            return (1, "CRITICAL")
        
        if any(err in stderr_text for err in ["Invalid data", "Invalid NAL unit", "conversion failed", "Error splitting"]):
            return (1, "STREAM_ERROR")
        
        print(stderr_text)
        return (1, "UNKNOWN")
    
    def convert_legacy(self, input_file: Path, output_file: Path) -> int:
        """Safe mode conversion using disk extraction."""
        self._total_steps = 4
        raw_temp = input_file.with_suffix(".raw.hevc")
        self.temp_files.append(raw_temp)

        if self.video_info is None or self.video_info.track_id is None:
            return 1

        # Extraction step
        spinner = Spinner("[1/4] Extracting... ")
        self.active_spinner = spinner
        spinner.start()

        ret, _, _ = self.media.run_logged(
            ["mkvextract", str(input_file), "tracks", f"{self.video_info.track_id}:{raw_temp}"]
        )
        spinner.stop()
        self.active_spinner = None

        if ret == 130 or self.abort_requested:
            return 130
        if ret != 0:
            return 1

        print(f"\r\033[K[1/4] Extracting... Done.")

        # Conversion step
        spinner = Spinner("[2/4] Converting... ")
        self.active_spinner = spinner
        spinner.start()

        # Choose dovi_tool command based on mode
        if self.config.hdr10_mode:
            dovi_cmd = ["dovi_tool", "remove", str(raw_temp), "-o", str(output_file)]
        else:
            dovi_cmd = ["dovi_tool", "-m", "2", "convert", "--discard", str(raw_temp), "-o", str(output_file)]

        ret, _, _ = self.media.run_logged(dovi_cmd)
        spinner.stop()
        self.active_spinner = None
        
        raw_temp.unlink(missing_ok=True)
        
        if ret == 0:
            print(f"\r\033[K[2/4] Converting... Done.")

        if ret == 130:
            return 130
        return ret

    def _convert_validate(self, filepath: Path, mode: str) -> Optional[int]:
        """Validate if file is ready for conversion. Returns return_code if should stop, None if ok."""
        if not filepath.exists():
            print(f"File not found: {filepath}")
            return 1
        
        self.analyze_file(filepath)
        
        # Safety check: Complex FEL
        if self.scan_result and self.scan_result.verdict == "COMPLEX":
            if self.config.force_mode:
                print(f"{RED}Complex FEL detected. Force Mode enabled. Proceeding...{RESET}")
            else:
                if mode == "auto":
                    return 2
                print(f"{RED}Error: Complex FEL detected (not safe to convert. Use -force to override).{RESET}")
                return 1
        
        # Simple FEL Advisory (skip in batch mode - already handled in pre-flight)
        if "FEL (Simple)" in self.dovi_status and mode == "manual":
            if self.config.include_simple:
                print(f"{BLUE}[i] Simple-FEL: Explicitly included (--include-simple).{RESET}")
            elif self.config.auto_yes:
                # Safety first: --yes without --include-simple skips Simple FEL
                print(f"{MAGENTA}[!] Simple FEL detected. Skipping (--yes without --include-simple).{RESET}")
                return 1
            else:
                print(f"{MAGENTA}[!] WARNING: This is a 'Simple FEL' file.{RESET}")
                print("    Scan found no active brightness expansion.")
                print("    Use -inspect for a full RPU analysis if in doubt.")
                try:
                    reply = input("Proceed with conversion? (y/N) ").strip().lower()
                except EOFError:
                    reply = "n"
                if reply != "y":
                    print("Conversion cancelled.")
                    return 1
        
        # Check if not Profile 7
        if self.action in ("IGNORE", "SKIP") or "CONVERT" not in self.action:
            if mode == "auto":
                return 2
            print(f"{RED}Error: Input file is not a Dolby Vision Profile 7 file.{RESET}")
            return 1
            
        return None

    def _convert_setup_paths(self, filepath: Path, batch_source_dir: Optional[Path] = None) -> Optional[Tuple[Path, Path, Path, Path]]:
        """Setup paths. Returns (conv_hevc, temp_mkv, backup_mkv, final_output_path) or None on error."""
        base_name = filepath.stem
        
        # Choose suffix based on conversion mode
        suffix = "hdr10" if self.config.hdr10_mode else "p81"
        
        # Use temp_dir for intermediate files if specified, otherwise use source directory
        # Route conv_hevc to temp_dir if specified (Design B: only HEVC goes to temp)
        if self.config.temp_dir:
            conv_hevc = self.config.temp_dir / f"{base_name}.{suffix}.hevc"
        else:
            conv_hevc = filepath.with_name(f"{base_name}.{suffix}.hevc")
        
        # temp_mkv uses .tmp extension to prevent automation retrigger
        # Always stays in source directory for efficient I/O
        temp_mkv = filepath.with_name(f"{base_name}.{suffix}.tmp")
        backup_mkv = filepath.with_suffix(".mkv.bak.dovi_convert")
        
        # Calculate final output path
        if self.config.output_dir:
            if batch_source_dir:
                # Batch mode: preserve basename and subdirectory structure
                batch_basename = batch_source_dir.name
                rel_path = filepath.relative_to(batch_source_dir)
                final_output_path = self.config.output_dir / batch_basename / rel_path
            else:
                # Convert mode: place directly in output directory
                final_output_path = self.config.output_dir / f"{base_name}.mkv"
            
            # Ensure output subdirectories exist
            final_output_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            # No -o flag: output in source directory (standard behavior)
            final_output_path = filepath
        
        self.temp_files.extend([conv_hevc, temp_mkv])
        
        if backup_mkv.exists():
            print(f"{RED}Skipping: Backup file already exists.{RESET}")
            return None
            
        return conv_hevc, temp_mkv, backup_mkv, final_output_path

    def _convert_perform_conversion(self, filepath: Path, conv_hevc: Path) -> int:
        """Run the actual conversion. Returns 0=success, 1=fail, 130=abort."""
        if self.config.safe_mode:
            if self.convert_legacy(filepath, conv_hevc) != 0:
                if self.abort_requested:
                    return 130
                self.last_fail_reason = "Safe mode extraction failed"
                print(f"{RED}Safe Mode Failed.{RESET}")
                return 1
            return 0
        else:
            turbo_res, fail_reason = self.convert_turbo(filepath, conv_hevc)

            if turbo_res == 0:
                return 0
            elif turbo_res == 130 or self.abort_requested:
                return 130
            else:
                print(f"{RED}Standard Mode Failed.{RESET}")

                if fail_reason == "CRITICAL":
                    self.last_fail_reason = "Disk full or permission denied"
                    print(f"{RED}CRITICAL ERROR: Disk Full or Permission Denied.{RESET}")
                    return 1
                elif fail_reason == "STREAM_ERROR":
                    self.last_fail_reason = "Stream/timestamp error (likely seamless branching)"
                    print(f"{MAGENTA}Reason: Stream/Timestamp Error (Likely Seamless Branching).{RESET}")
                else:
                    self.last_fail_reason = "Unknown conversion error"

                if self.batch_running:
                    print(f"{MAGENTA}Batch Mode: Skipping file. Retry manually with -safe.{RESET}")
                    return 1
                else:
                    print(f"{MAGENTA}Suggestion: This file may require Safe Mode (Disk Extraction).{RESET}")
                    try:
                        reply = input("Retry with Safe Mode? (Y/n) ").strip().lower()
                    except EOFError:
                        reply = "n"
                    if reply == "n":
                        return 1

                    print("[Retry] ", end="")
                    if self.convert_legacy(filepath, conv_hevc) != 0:
                        if self.abort_requested:
                            return 130
                        self.last_fail_reason = "Safe mode fallback also failed"
                        print(f"{RED}Safe Mode also failed.{RESET}")
                        return 1
                    return 0

    def _convert_mux(self, filepath: Path, conv_hevc: Path, temp_mkv: Path, fps_orig: str, total_steps: int = 3) -> int:
        """Mux the new file. Returns 0=success, 1=fail, 130=abort."""
        mux_args = ["-o", str(temp_mkv)]
        
        if self.video_info and self.video_info.delay != 0:
            mux_args.extend(["--sync", f"0:{self.video_info.delay}"])
        
        mux_args.extend(["--default-duration", f"0:{fps_orig}fps"])
        mux_args.extend(["--language", f"0:{self.video_info.language if self.video_info else 'und'}"])
        
        if self.video_info and self.video_info.name:
            mux_args.extend(["--track-name", f"0:{self.video_info.name}"])
        
        mux_args.append(str(conv_hevc))
        mux_args.extend(["--no-video", str(filepath)])
        
        mux_step = total_steps - 1
        spinner = Spinner(f"[{mux_step}/{total_steps}] Muxing (Cloning Metadata + {fps_orig}fps)... ")
        self.active_spinner = spinner
        spinner.start()

        ret, _, _ = self.media.run_logged(["mkvmerge"] + mux_args)
        spinner.stop()
        self.active_spinner = None

        if ret == 130 or self.abort_requested:
            return 130
        if ret != 0:
            self.last_fail_reason = "Mux failed"
            print(f"{RED}Mux Failed.{RESET}")
            return 1

        print(f"\r\033[K[{mux_step}/{total_steps}] Muxing (Cloning Metadata + {fps_orig}fps)... Done.")
        return 0

    def _convert_finalize(self, filepath: Path, temp_mkv: Path, backup_mkv: Path, conv_hevc: Path, start_time: float, orig_size: int, final_output_path: Path, total_steps: int = 3) -> int:
        """Verify, swap and print metrics. Returns 0=success, 1=fail."""
        # Step 4: Verification
        print(f"[{total_steps}/{total_steps}] Verifying... ", end="", flush=True)

        frames_orig = self.media.get_frame_count(filepath)
        frames_new = self.media.get_frame_count(temp_mkv)

        if frames_orig is None or frames_new is None:
            print(f"\r\033[K[{total_steps}/{total_steps}] Verifying... {YELLOW}Skipped{RESET} (see warning above)")
        elif frames_orig != frames_new:
            # DVY-33: Smart Verification Fallback
            # MediaInfo metadata in the source might be wrong (e.g. Avatar.mkv).
            # We run a slow but accurate ffprobe stream count on the ORIGINAL file to double-check.
            print(f"\r\033[K[{total_steps}/{total_steps}] Verifying... {MAGENTA}Metadata Mismatch ({frames_orig} vs {frames_new}). Checking Stream...{RESET}")

            ff_orig = self.media.get_frame_count_ffprobe(filepath)

            if ff_orig == frames_new:
                 print(f"\r\033[K[{total_steps}/{total_steps}] Verifying... {GREEN}Success!{RESET} (Source Metadata was incorrect)")
                 if self.config.debug_mode:
                     self.media.log(f"Frame verification mismatch resolved: MI_Orig={frames_orig}, FF_Orig={ff_orig}, New={frames_new}")
            else:
                self.last_fail_reason = f"Frame mismatch (expected {ff_orig}, got {frames_new})"
                print(f"\r\033[K[{total_steps}/{total_steps}] Verifying... {RED}FAIL: Frame mismatch!{RESET} (Stream Verified: {ff_orig} vs {frames_new})")
                return 1
        else:
            print(f"\r\033[K[{total_steps}/{total_steps}] Verifying... {GREEN}Success!{RESET}")
        
        # Print metrics
        self.print_metrics(temp_mkv, frames_new or 0, start_time, orig_size)
        
        # Step 5: Create backup and move output
        try:
            filepath.rename(backup_mkv)
        except OSError as e:
            print(f"{RED}Error: Could not rename original file to backup: {e}{RESET}")
            return 1

        self.last_output_path = final_output_path
        if final_output_path == filepath:
            # No -o flag: rename .tmp to .mkv in same directory
            try:
                temp_mkv.rename(filepath)
            except OSError as e:
                print(f"{RED}Error: Could not place converted file: {e}{RESET}")
                try:
                    backup_mkv.rename(filepath)
                    print(f"Original restored successfully.")
                except OSError:
                    print(f"Could not restore original — it is saved as: {BLUE}{backup_mkv}{RESET}")
                return 1
        else:
            # With -o: move to destination and rename .tmp to .mkv
            spinner = Spinner("Moving to output directory... ")
            self.active_spinner = spinner
            spinner.start()
            try:
                shutil.move(str(temp_mkv), str(final_output_path))
            except OSError as e:
                spinner.stop()
                self.active_spinner = None
                print(f"\r\033[K{RED}Error: Could not move converted file to output directory: {e}{RESET}")
                try:
                    backup_mkv.rename(filepath)
                    print(f"Original restored successfully.")
                except OSError:
                    print(f"Could not restore original — it is saved as: {BLUE}{backup_mkv}{RESET}")
                return 1
            spinner.stop()
            self.active_spinner = None
            print(f"\r\033[KMoving to output directory... Done.")
            print(f"Output saved to: {BLUE}{final_output_path}{RESET}")
        
        if self.config.delete_backup:
            backup_mkv.unlink()
            print(f"{MAGENTA}Original Source deleted (--delete active).{RESET}")
        else:
            print(f"Original Source saved as: {BLUE}{backup_mkv}{RESET}")

        # Show EL backup location if --backup was used
        if self._backup_archive_path:
            print(f"Enhancement Layer backed up as: {CYAN}{self._backup_archive_path}{RESET}")
            self._backup_archive_path = None  # Reset for next conversion

        conv_hevc.unlink(missing_ok=True)
        return 0

    
    def cmd_convert(self, filepath: Path, mode: str = "manual", batch_source_dir: Optional[Path] = None) -> int:
        """Convert a single file. Returns 0=success, 1=error, 2=skip, 130=abort."""

        # 1. Validation
        res = self._convert_validate(filepath, mode)
        if res is not None:
            return res

        # Only print Processing line in manual mode (batch mode already announces the file)
        if mode == "manual":
            print(f"{BOLD}Processing:{RESET} {filepath}")

        # 1b. Create EL backup if --backup flag is set
        self._backup_archive_path = None  # Reset for this conversion
        if self.config.create_backup:
            backup_mgr = BackupManager(self)
            archive_path = backup_mgr._get_archive_path(filepath)

            if archive_path.exists():
                self._backup_archive_path = archive_path  # Track for final output
                self.backup_archives_created.append(archive_path)  # Track for abort message
                if mode == "manual" or self.config.verbose_mode:
                    print(f"  {BLUE}Backup already exists:{RESET} {archive_path.name}")
            else:
                if mode == "manual" or self.config.verbose_mode:
                    spinner = Spinner("Creating EL backup... ")
                    spinner.start()

                status, archive_path, error = backup_mgr.create_backup(filepath)

                if mode == "manual" or self.config.verbose_mode:
                    spinner.stop()

                if status == 130:
                    return 130  # User abort

                if status != 0:
                    if mode == "manual" or self.config.verbose_mode:
                        print(f"\r\033[K{RED}Backup failed:{RESET} {error}")
                    return 1

                self._backup_archive_path = archive_path  # Track for final output
                self.backup_archives_created.append(archive_path)  # Track for abort message

                if mode == "manual" or self.config.verbose_mode:
                    archive_size = get_file_size(archive_path)
                    print(f"\r\033[KCreating EL backup... Done. ({human_size_gb(archive_size)})")
        
        # 2. Setup (Get FPS and paths)
        fps_orig = self.media.get_fps(filepath)
        if not fps_orig:
            print(f"{RED}Error: Could not detect Frame Rate.{RESET}")
            return 1
            
        paths = self._convert_setup_paths(filepath, batch_source_dir)
        if not paths:
            return 1
        conv_hevc, temp_mkv, backup_mkv, final_output_path = paths
        
        # Initialize metrics
        start_time = time.time()
        orig_size = get_file_size(filepath)
        
        # 3. Conversion
        try:
            res = self._convert_perform_conversion(filepath, conv_hevc)
            if res != 0:
                return res
                
            # 4. Muxing
            total_steps = getattr(self, '_total_steps', 3)
            res = self._convert_mux(filepath, conv_hevc, temp_mkv, fps_orig, total_steps)
            if res != 0:
                return res

            # 5. Finalize
            return self._convert_finalize(filepath, temp_mkv, backup_mkv, conv_hevc, start_time, orig_size, final_output_path, total_steps)
            
        finally:
            # Critical Cleanup: Ensures temp files are deleted even if we return early (e.g. Frame Mismatch)
            self._cleanup()
    
    def cmd_check_single(self, filepath: Path) -> None:
        """Scan and report on a single file."""
        self.analyze_file(filepath)
        
        if self.video_info and self.video_info.mi_info_string == "FILE_NOT_FOUND":
            print(f"{RED}Error: File '{filepath}' not found.{RESET}")
            return
        
        delay_ms = (self.video_info.delay // 1000000) if self.video_info else 0
        name = filepath.name
        
        print("---------------------------------------------------")
        print(f"{BOLD}File:{RESET}   {name}")
        print(f"{BOLD}Status:{RESET} {self.dovi_status}")
        print(f"{BOLD}Action:{RESET} {self.action}")
        print("---------------------------------------------------")
        
        # Simple FEL Advisory
        if "FEL (Simple)" in self.dovi_status:
            print()
            self._print_simple_fel_advisory()
    
    def cmd_check_all(self, max_depth: int = 1, files: List[Path] = None) -> None:
        """Scan all MKV files in directory or provided list."""
        if files:
            location = f"{len(files)} file(s)"
        elif max_depth > 1:
            location = f"recursively ({max_depth} levels deep)"
        else:
            location = "in current directory"
        
        print(f"{BLUE}Running Scanning {location}...{RESET}")
        
        # Use provided files or find them
        mkv_files = files if files else self._find_mkv_files(max_depth)
        
        # DVY-35: Early return if no files found
        if not mkv_files:
            print("No MKV files found.")
            return
        
        filename_width = 50
        format_width = 36

        # Print table header
        print(f"{'Filename':<{filename_width}} {'Format':<{format_width}} Action")
        print("-" * 96)
        
        simple_count = 0
        candidate_count = 0
        filtered_count = 0
        candidates_only = self.config.candidates_only
        
        # Only show directory headers if multiple directories
        unique_dirs = len(set(f.parent for f in mkv_files))
        show_headers = unique_dirs > 1
        current_directory = None
        
        for mkv_file in mkv_files:
            self.analyze_file(mkv_file)
            
            # DVY-46: Filter for candidates only
            is_candidate = "CONVERT" in self.action
            if candidates_only and not is_candidate:
                filtered_count += 1
                continue
            
            if is_candidate:
                candidate_count += 1
            
            # Directory header for grouping (only if multiple directories)
            if show_headers and mkv_file.parent != current_directory:
                if current_directory is not None:
                    print()  # Blank line between directories
                print(f"{BOLD}DIRECTORY: {mkv_file.parent}{RESET}")
                current_directory = mkv_file.parent
            
            name = mkv_file.name
            if len(name) > filename_width:
                name = name[: filename_width - 3] + "..."
            
            if "FEL (Simple)" in self.dovi_status:
                simple_count += 1
            
            status_visible_len = len(ANSI_ESCAPE_RE.sub("", self.dovi_status))
            status_padding = max(0, format_width - status_visible_len)
            print(f"{name:<{filename_width}} {self.dovi_status}{' ' * status_padding} {self.action}")

        # DVY-46: Summary for candidates-only mode
        if candidates_only:
            if candidate_count == 0:
                print("\nNo conversion candidates found.")
            else:
                print(f"\nShowing {candidate_count} candidate(s). {filtered_count} file(s) filtered.")
        
        # Conditional Advisory
        if simple_count > 0:
            print()
            self._print_simple_fel_advisory()
    
    def _print_simple_fel_advisory(self) -> None:
        """Print the Simple FEL advisory block."""
        print("=" * 96)
        print(f"{RED}*{RESET}{BOLD}ADVISORY: UNDERSTANDING 'SIMPLE' (BLUE) VERDICTS{RESET}")
        print("-" * 96)
        print(f"{BOLD}What is 'Simple FEL'?{RESET}")
        print("It means the scan detected no active brightness expansion over the Base Layer. This")
        print("suggests the file is likely safe to convert. But:")
        print()
        print(f"{BOLD}How accurate is the scan?{RESET}")
        print("The script takes 10 samples at different timestamps of the video to analyze peak brightness in")
        print("the FEL. While this is statistically accurate enough to determine whether the FEL expands luminance")
        print("over the Base Layer, it can't guarantee a definitive result. If accurate preservation is paramount")
        print("for a specific file, please verify it with -inspect before converting.")
        print("=" * 96)
    
    def _find_mkv_files(self, max_depth: int, paths: List[Path] = None) -> List[Path]:
        """
        Find MKV files up to max_depth.
        If paths provided, search those. Otherwise search current directory.
        """
        files = []
        
        # Default to current directory if no paths specified
        search_dirs = paths if paths else [Path.cwd()]
        
        for search_path in search_dirs:
            if search_path.is_file():
                # Direct file - include if it's an MKV
                if search_path.suffix.lower() == ".mkv" and not search_path.name.startswith("._"):
                    files.append(search_path)
            elif search_path.is_dir():
                # Directory - glob for MKV files
                if max_depth == 1:
                    for f in search_path.glob("*.mkv"):
                        if not f.name.startswith("._"):
                            files.append(f)
                else:
                    for depth in range(max_depth + 1):
                        pattern = "/".join(["*"] * depth) + "/*.mkv" if depth > 0 else "*.mkv"
                        for f in search_path.glob(pattern):
                            if not f.name.startswith("._") and "._" not in str(f):
                                files.append(f)
        
        return sorted(set(files))
    
    def collect_inputs(self, args: List[str], command: str, depth: int = 1) -> List[Path]:
        """
        Shared input handler - the 'bouncer'.
        Validates inputs based on command rules and returns list of files to process.
        
        Rules:
        - convert: Both files and directories allowed
        - scan: Both files and directories allowed
        """
        files: List[Path] = []
        directories: List[Path] = []
        
        for arg in args:
            path = Path(arg)
            if path.is_file():
                files.append(path)
            elif path.is_dir():
                directories.append(path)
            else:
                print(f"{RED}Error: '{arg}' not found.{RESET}")
                sys.exit(1)
        
        # If directories provided, find MKV files in them
        if directories:
            files.extend(self._find_mkv_files(depth, directories))
        
        # If no args at all, default to current directory (for scan only)
        if not args and command == "scan":
            files = self._find_mkv_files(depth)
        
        return sorted(set(files), key=lambda f: str(f))
    
    
    def _build_batch_source_map(self, source_dirs: List[Path], max_depth: int) -> Dict[Path, Path]:
        """
        Map files to their source directories and detect basename collisions.
        Only called when -o flag is used.
        
        Args:
            source_dirs: List of directories specified by user
            max_depth: Recursion depth for file discovery
            
        Returns:
            Dictionary mapping file_path -> source_directory
            
        Raises:
            SystemExit: If basename collision detected
        """
        # Pre-flight check: detect basename collisions
        basenames = [d.name for d in source_dirs]
        if len(basenames) != len(set(basenames)):
            # Find duplicates
            seen = {}
            duplicates = {}
            for d in source_dirs:
                basename = d.name
                if basename in seen:
                    if basename not in duplicates:
                        duplicates[basename] = [seen[basename]]
                    duplicates[basename].append(d)
                else:
                    seen[basename] = d
            
            # Format error message
            print(f"{RED}Error: Basename collision detected.{RESET}")
            for basename, paths in duplicates.items():
                print(f"\nMultiple source directories share the same name '{basename}':")
                for p in paths:
                    print(f"  - {p}")
                print(f"\nBoth would write to: {self.config.output_dir}/{basename}/")
            
            print(f"\n{BOLD}Solution:{RESET} Process directories separately:")
            for i, (basename, paths) in enumerate(duplicates.items()):
                for j, p in enumerate(paths):
                    suffix = f"_{j+1}" if len(paths) > 1 else ""
                    print(f"  dovi_convert -batch {p} -o {self.config.output_dir}/{basename}{suffix}/")
            
            sys.exit(1)
        
        # Build file-to-source mapping
        file_map: Dict[Path, Path] = {}
        for source_dir in source_dirs:
            files = self._find_mkv_files(max_depth, [source_dir])
            for file in files:
                file_map[file] = source_dir

        return file_map

    def _get_error_suggestion(self, reason: str) -> Optional[str]:
        """Get suggestion based on error reason."""
        reason_lower = reason.lower()
        if "stream" in reason_lower or "timestamp" in reason_lower or "seamless" in reason_lower:
            return f"Retry with {BOLD}--safe{RESET} flag"
        if "frame mismatch" in reason_lower:
            return f"Retry with {BOLD}--safe{RESET} flag"
        if "disk" in reason_lower or "permission" in reason_lower:
            return None  # User must fix manually
        return f"Run with {BOLD}--debug{RESET} flag and check dovi_convert_debug.log"

    def _print_compact_summary(self, stats: BatchStats) -> None:
        """Print compact end-of-batch summary."""
        print()
        print("=" * 96)
        if stats.aborted:
            print(f"{MAGENTA}{BOLD}BATCH ABORTED BY USER{RESET}")
        else:
            print(f"{BOLD}BATCH COMPLETE{RESET}")
        print("=" * 96)

        minutes = int(stats.elapsed) // 60
        seconds = int(stats.elapsed) % 60
        size_str = human_size_gb(stats.converted_size)

        print(f"Converted:     {GREEN}{len(stats.success_list)}{RESET} files ({size_str})")
        print(f"Failed:        {RED}{len(stats.fail_list)}{RESET} file{'s' if len(stats.fail_list) != 1 else ''}")
        if stats.aborted and stats.queue_count > 0:
            skipped = stats.queue_count - len(stats.success_list) - len(stats.fail_list)
            if skipped > 0:
                print(f"Skipped:       {MAGENTA}{skipped}{RESET} file{'s' if skipped != 1 else ''} (aborted)")
        if self.backup_archives_created:
            count = len(self.backup_archives_created)
            print(f"Backups:       {CYAN}{count}{RESET} .dovi archive{'s' if count != 1 else ''} preserved")
        print(f"Time elapsed:  {minutes}m {seconds:02d}s")
        if self.config.output_dir:
            print(f"Output:        {BLUE}{self.config.output_dir}{RESET}")

        if stats.fail_list:
            print()
            print("-" * 96)
            print(f"{RED}FAILED CONVERSIONS:{RESET}")
            print()
            for i, (filepath, reason) in enumerate(stats.fail_list, 1):
                print(f"{i}. {BOLD}{filepath.name}{RESET}")
                print(f"   Path:       {filepath.parent}/")
                print(f"   Error:      {reason}")
                suggestion = self._get_error_suggestion(reason)
                if suggestion:
                    print(f"   Suggestion: {suggestion}")
                print()

        print("=" * 96)
        print()

    def _print_verbose_summary(self, stats: BatchStats) -> None:
        """Print verbose end-of-batch summary."""
        print()
        print("=" * 51)
        if stats.aborted:
            print(f"{MAGENTA}{BOLD}BATCH ABORTED BY USER{RESET}")
        else:
            print(f"{BOLD}BATCH COMPLETE{RESET}")
        print("=" * 51)

        # Build converted line with breakdown
        if stats.success_simple_count > 0 or stats.success_forced_count > 0:
            total_converted = len(stats.success_list)
            parts = []
            if stats.success_simple_count > 0:
                mel_count = stats.success_simple_count - stats.success_simple_fel_count
                if stats.success_simple_fel_count > 0 and mel_count > 0:
                    parts.append(f"{stats.success_simple_fel_count} Simple FEL")
                    parts.append(f"{mel_count} MEL")
                elif stats.success_simple_fel_count > 0:
                    parts.append(f"{stats.success_simple_fel_count} Simple FEL")
                else:
                    parts.append(f"{stats.success_simple_count} MEL")
            if stats.success_forced_count > 0:
                parts.append(f"{stats.success_forced_count} Complex FEL")
            breakdown = f" ({', '.join(parts)})" if parts else ""
            print(f"Converted:     {GREEN}{total_converted}{RESET} files{breakdown}")
        else:
            print(f"Converted:     {GREEN}0{RESET} files")

        print(f"Failed:        {RED}{len(stats.fail_list)}{RESET} file{'s' if len(stats.fail_list) != 1 else ''}")

        if stats.aborted and stats.queue_count > 0:
            skipped = stats.queue_count - len(stats.success_list) - len(stats.fail_list)
            if skipped > 0:
                print(f"Skipped:       {MAGENTA}{skipped}{RESET} file{'s' if skipped != 1 else ''} (aborted)")

        if self.backup_archives_created:
            count = len(self.backup_archives_created)
            print(f"Backups:       {CYAN}{count}{RESET} .dovi archive{'s' if count != 1 else ''} preserved")

        if stats.fail_list:
            print()
            print("-" * 51)
            print(f"{RED}FAILED CONVERSIONS:{RESET}")
            print()
            for i, (filepath, reason) in enumerate(stats.fail_list, 1):
                print(f"{i}. {BOLD}{filepath.name}{RESET}")
                print(f"   Path:       {filepath.parent}/")
                print(f"   Error:      {reason}")
                suggestion = self._get_error_suggestion(reason)
                if suggestion:
                    print(f"   Suggestion: {suggestion}")
                print()

        print("=" * 51)
        print()

    def cmd_batch(self, max_depth: int = 1, files: List[Path] = None, source_dirs: List[Path] = None) -> None:
        """Batch processing of directory or provided file list."""

        conversion_queue: List[Path] = []
        file_kind: Dict[Path, str] = {}  # Track classification: "mel", "simple_fel", "forced"

        # Use BatchStats to track all statistics in one place
        stats = BatchStats()
        total_batch_size = 0
        
        if files:
            print(f"{BOLD}Processing {len(files)} file(s)...{RESET}")
        else:
            print(f"{BOLD}Scanning for Profile 7 files (Depth: {max_depth})...{RESET}")
        
        # Use provided files or find them
        mkv_files = files if files else self._find_mkv_files(max_depth)
        
        for mkv_file in mkv_files:
            self.analyze_file(mkv_file)
            
            is_simple_fel = "FEL (Simple)" in self.dovi_status
            
            if "CONVERT" in self.action:
                conversion_queue.append(mkv_file)
                
                if "FORCED" in self.action:
                    file_kind[mkv_file] = "forced"
                elif is_simple_fel:
                    file_kind[mkv_file] = "simple_fel"
                else:
                    file_kind[mkv_file] = "mel"
                
                total_batch_size += get_file_size(mkv_file)
                
            elif self.action == "IGNORE":
                stats.ignored_count += 1
            elif self.scan_result and self.scan_result.verdict == "COMPLEX":
                stats.complex_count += 1
            else:
                stats.skipped_count += 1

        if len(conversion_queue) == 0 and stats.complex_count == 0:
            print(f"No Profile 7 files found (Ignored: {stats.ignored_count}).")
            self.batch_running = False
            return
        
        # Count by classification
        simple_fel_files = [f for f in conversion_queue if file_kind.get(f) == "simple_fel"]
        simple_fel_count = len(simple_fel_files)
        simple_fel_excluded = False
        
        # Ask about Simple FEL files BEFORE showing summary (so summary is accurate)
        if simple_fel_count > 0 and not self.config.include_simple:
            if self.config.auto_yes:
                # --yes without --include-simple: skip Simple FEL files
                print(f"\n{MAGENTA}[!] {simple_fel_count} Simple-FEL file(s) detected.{RESET}")
                print(f"    To analyze, use {BOLD}inspect{RESET}. To include, add {BOLD}--include-simple{RESET}.")
                print(f"    Skipping {simple_fel_count} file(s). Proceeding with remaining files...")
                simple_fel_excluded = True
            else:
                # Interactive: ask user
                print(f"\n{MAGENTA}[!] Found {simple_fel_count} 'Simple FEL' file(s).{RESET}")
                print(f"    For details, run {BOLD}scan{RESET} first.")
                try:
                    reply = input("    Include Simple-FEL files in batch? (y/N) ").strip().lower()
                except EOFError:
                    reply = "n"
                
                if reply != "y":
                    simple_fel_excluded = True
            
            if simple_fel_excluded:
                conversion_queue = [f for f in conversion_queue if f not in simple_fel_files]
        
        # Compute counts from the final queue
        queue_count = len(conversion_queue)
        mel_count = sum(1 for f in conversion_queue if file_kind.get(f) == "mel")
        simple_fel_in_queue = sum(1 for f in conversion_queue if file_kind.get(f) == "simple_fel")
        forced_count = sum(1 for f in conversion_queue if file_kind.get(f) == "forced")
        
        # Early exit if nothing to convert
        if queue_count == 0:
            print(f"\n{MAGENTA}No files eligible for conversion.{RESET}")
            if stats.complex_count > 0 or stats.ignored_count > 0 or stats.skipped_count > 0:
                print(f"  Ignored: {stats.ignored_count} (Not P7), Complex FEL: {stats.complex_count} (Unsafe), Skipped: {stats.skipped_count} (Invalid)")
            if simple_fel_excluded:
                print(f"  Simple FEL: {simple_fel_count} (Excluded)")
            self.batch_running = False
            return
        
        # Recalculate total size after potential exclusions
        total_batch_size = sum(get_file_size(f) for f in conversion_queue)
        total_size_gb = human_size_gb(total_batch_size)
        
        print(f"\n{BOLD}Batch Overview:{RESET}")
        
        if mel_count > 0:
            print(f"  Convert:        {GREEN}{mel_count}{RESET}   (MEL - Safe)")
        if simple_fel_in_queue > 0:
            print(f"  Convert:        {BLUE}{simple_fel_in_queue}{RESET}   (Simple FEL - Likely Safe)")
        if simple_fel_excluded and simple_fel_count > 0:
            print(f"  Skip:           {MAGENTA}{simple_fel_count}{RESET}   (Simple FEL - Excluded)")
        if forced_count > 0:
            print(f"  Convert:        {MAGENTA}{forced_count}{RESET}   (Complex FEL - Forced)")
        if stats.complex_count > 0:
            print(f"  Skip:           {RED}{stats.complex_count}{RESET}   (Complex FEL)")
        print(f"  Queue Size:     {BLUE}{total_size_gb}{RESET} ({queue_count} file(s))")

        # Show note about ignored/invalid files if any
        if stats.ignored_count > 0 or stats.skipped_count > 0:
            parts = []
            if stats.ignored_count > 0:
                parts.append(f"{stats.ignored_count} ignored (not Profile 7)")
            if stats.skipped_count > 0:
                parts.append(f"{stats.skipped_count} invalid")
            print(f"  Note:           {', '.join(parts)}")

        # Proceed with conversion
        if self.config.auto_yes:
            print(f"\n{MAGENTA}Auto-Yes (-y) active. Starting conversion immediately...{RESET}")
            time.sleep(2)
        else:
            try:
                reply = input("\nShow file list? (y/N) ").strip().lower()
            except EOFError:
                reply = "n"
            
            if reply == "y":
                print(f"\n{BOLD}Conversion Queue:{RESET}")
                for f in conversion_queue:
                    print(f" - {f}")
            
            try:
                reply = input(f"\nProceed with conversion of {queue_count} file(s)? (Y/n) ").strip().lower()
            except EOFError:
                reply = "n"
            
            if reply not in ("y", ""):
                print("Batch cancelled.")
                self.batch_running = False
                return
        
        # Build file-to-source mapping if -o flag is used
        file_source_map: Dict[Path, Path] = {}
        if self.config.output_dir and source_dirs:
            file_source_map = self._build_batch_source_map(source_dirs, max_depth)

        # Store queue count for summary calculations
        stats.queue_count = queue_count

        # Determine output mode: compact (default) or verbose (--verbose)
        compact_mode = not self.config.verbose_mode

        if compact_mode:
            # Compact mode: table-based output
            print()
            print("=" * 96)
            self.batch_running = True
            print(f"{BOLD}BATCH PROCESSING STARTED{RESET}")
            print("=" * 96)
            print()
            print(f"Format: {GREEN}MEL{RESET} = Safe | {BLUE}sFEL{RESET} = Simple FEL | {MAGENTA}cFEL{RESET} = Complex FEL (Forced)")
            print()

            # Calculate layout dimensions
            digit_width = len(str(queue_count))
            prefix_width = digit_width * 2 + 4  # [N/N] + space
            filename_width = 96 - prefix_width - 8 - 15  # table - prefix - format - progress

            # Print header
            header_filename = f"{'Filename':<{filename_width + prefix_width}}"
            print(f"{header_filename}{'Format':<8}Progress")
            print("-" * 96)

            # Directory header tracking
            unique_dirs = len(set(f.parent for f in conversion_queue))
            show_dir_headers = unique_dirs > 1
            current_directory: Optional[Path] = None

            batch_start_time = time.time()

            for idx, filepath in enumerate(conversion_queue, 1):
                if self.abort_requested:
                    break

                # Directory header
                if show_dir_headers and filepath.parent != current_directory:
                    if current_directory is not None:
                        print()
                    print(f"{BOLD}DIRECTORY: {filepath.parent}{RESET}")
                    current_directory = filepath.parent

                # Format columns
                prefix = f"[{idx:>{digit_width}}/{queue_count}]"
                name = truncate_middle(filepath.name, filename_width - 4)  # -4 for spacing before format column
                kind = file_kind.get(filepath, "mel")
                fmt_map = {"mel": f"{GREEN}MEL{RESET}", "simple_fel": f"{BLUE}sFEL{RESET}",
                           "forced": f"{MAGENTA}cFEL{RESET}"}
                fmt_str = fmt_map.get(kind, "???")

                # Start spinner
                spinner = CompactBatchSpinner(prefix, name, fmt_str, filename_width)
                spinner.start()

                # Re-analyze and convert with suppressed output
                self.analyze_file(filepath)
                self.last_fail_reason = ""
                batch_source = file_source_map.get(filepath, None)

                with suppress_output():
                    res = self.cmd_convert(filepath, "auto", batch_source)

                # Finish row
                if res == 130 or self.abort_requested:
                    spinner.stop(False)
                    break
                elif res == 0:
                    spinner.stop(True)
                    stats.success_list.append(filepath.name)
                    stats.converted_size += get_file_size(self.last_output_path)
                    if self.scan_result and self.scan_result.verdict == "COMPLEX":
                        stats.success_forced_count += 1
                    else:
                        stats.success_simple_count += 1
                        if "Simple" in self.dovi_status:
                            stats.success_simple_fel_count += 1
                elif res == 2:
                    spinner.stop(False)
                    stats.skipped_count += 1
                else:
                    spinner.stop(False)
                    stats.fail_list.append((filepath, self.last_fail_reason or "Unknown error"))

            self.batch_running = False

            # Print compact summary (always show, even on abort)
            stats.elapsed = time.time() - batch_start_time
            stats.aborted = self.abort_requested
            self._print_compact_summary(stats)

        else:
            # Verbose mode: original detailed output
            print()
            print("=" * 51)
            self.batch_running = True
            print("BATCH PROCESSING STARTED")
            print("=" * 51)

            for idx, filepath in enumerate(conversion_queue, 1):
                if self.abort_requested:
                    break

                print("Batch:")
                print(f"{BOLD}[{idx}/{queue_count}]{RESET} {filepath.name}")
                print("---------------------------------------------------")

                # Re-analyze for fresh state
                self.analyze_file(filepath)
                self.last_fail_reason = ""

                # Get source directory from map (None if -o not used or file not in map)
                batch_source = file_source_map.get(filepath, None)

                res = self.cmd_convert(filepath, "auto", batch_source)

                if res == 130 or self.abort_requested:
                    break
                elif res == 0:
                    stats.success_list.append(filepath.name)
                    if self.scan_result and self.scan_result.verdict == "COMPLEX":
                        stats.success_forced_count += 1
                    else:
                        stats.success_simple_count += 1
                        if "Simple" in self.dovi_status:
                            stats.success_simple_fel_count += 1
                elif res == 2:
                    stats.skipped_count += 1
                else:
                    stats.fail_list.append((filepath, self.last_fail_reason or "Unknown error"))

            self.batch_running = False

            stats.aborted = self.abort_requested
            self._print_verbose_summary(stats)

    def cmd_backup(self, filepath: Path) -> int:
        """Create EL backup for a Profile 7 file.

        Returns: 0=success, 1=error
        """
        if not filepath.exists():
            print(f"{RED}File not found:{RESET} {filepath}")
            return 1

        if not filepath.suffix.lower() == ".mkv":
            print(f"{RED}Not an MKV file:{RESET} {filepath}")
            return 1

        # Check if file is Profile 7 FEL
        print(f"Analyzing: {BOLD}{filepath.name}{RESET}")
        info = self.media.get_video_info(filepath)

        if "Dolby Vision" not in info.mi_info_string:
            print(f"{RED}Not a Dolby Vision file.{RESET}")
            return 1

        # Check for FEL (Profile 7)
        is_profile_7 = "dvhe.07" in info.mi_info_string.lower() or "profile 7" in info.mi_info_string.lower()
        if not is_profile_7:
            print(f"{RED}Not a Profile 7 FEL file.{RESET} Only Profile 7 files can be backed up.")
            return 1

        # Create backup manager and execute
        backup_mgr = BackupManager(self)

        print(f"Creating backup...")
        spinner = Spinner("Extracting Enhancement Layer... ")
        spinner.start()

        status, archive_path, error = backup_mgr.create_backup(filepath)

        spinner.stop()

        if status != 0:
            print(f"\r\033[K{RED}Backup failed:{RESET} {error}")
            return 1

        # Report success
        archive_size = get_file_size(archive_path)
        orig_size = get_file_size(filepath)
        ratio = (archive_size / orig_size * 100) if orig_size > 0 else 0

        print(f"\r\033[KExtracting Enhancement Layer... Done.")
        print()
        print(f"{GREEN}Backup created successfully.{RESET}")
        print(f"  Archive: {BOLD}{archive_path}{RESET}")
        print(f"  Size: {human_size_gb(archive_size)} ({ratio:.1f}% of original)")
        return 0

    def cmd_restore(self, filepath: Path) -> int:
        """Restore Profile 7 from backup.

        Returns: 0=success, 1=error
        """
        if not filepath.exists():
            print(f"{RED}File not found:{RESET} {filepath}")
            return 1

        if not filepath.suffix.lower() == ".mkv":
            print(f"{RED}Not an MKV file:{RESET} {filepath}")
            return 1

        # Create backup manager
        backup_mgr = BackupManager(self)

        # Check if backup exists
        archive_path = backup_mgr.locate_backup(filepath)
        if not archive_path:
            expected = filepath.parent / (filepath.stem + BackupManager.ARCHIVE_EXTENSION)
            print(f"{RED}No backup found.{RESET}")
            print(f"  Expected: {expected}")
            if self.config.backup_source:
                print(f"  --source: {self.config.backup_source} (not found)")
            else:
                print(f"  Hint: Use --source to specify an alternate backup location.")
            return 1

        # Verify backup
        is_valid, error = backup_mgr.verify_backup(archive_path)
        if not is_valid:
            print(f"{RED}Invalid backup:{RESET} {error}")
            return 1

        print(f"Restoring: {BOLD}{filepath.name}{RESET}")
        print(f"  Using backup: {archive_path}")

        spinner = Spinner("Restoring Profile 7... ")
        spinner.start()

        status, restored_path, error = backup_mgr.restore_backup(filepath)

        spinner.stop()

        if status != 0:
            print(f"\r\033[K{RED}Restore failed:{RESET} {error}")
            return 1

        # Verify restored file
        restored_size = get_file_size(restored_path)
        info = self.media.get_video_info(restored_path)

        print(f"\r\033[KRestoring Profile 7... Done.")
        print()
        print(f"{GREEN}Restore completed successfully.{RESET}")
        print(f"  Restored file: {BOLD}{restored_path}{RESET}")
        print(f"  Size: {human_size_gb(restored_size)}")

        # Show format from mediainfo
        if "dvhe.07" in info.mi_info_string.lower():
            print(f"  Format: {GREEN}Profile 7 ({info.mi_info_string}){RESET}")
        else:
            print(f"  Format: {MAGENTA}Could not verify Profile 7{RESET} (check with 'dovi_convert scan')")

        return 0

    def cmd_cleanup(self, recursive: bool = False) -> None:
        """Clean up backup files."""
        if recursive:
            print(f"{BOLD}Scanning for .bak.dovi_convert files (Recursive)...{RESET}")
        else:
            print(f"{BOLD}Scanning for .bak.dovi_convert files (Current Directory)...{RESET}")
        
        files: List[Path] = []
        total_size = 0
        cwd = Path.cwd()
        
        # Find backup files
        pattern = "**/*.mkv.bak.dovi_convert" if recursive else "*.mkv.bak.dovi_convert"
        
        for f in cwd.glob(pattern):
            parent_file = Path(str(f).replace(".bak.dovi_convert", ""))
            
            if parent_file.exists():
                files.append(f)
                total_size += get_file_size(f)
            else:
                print(f"{MAGENTA}Skipping Orphan Backup:{RESET} {f.name}")
        
        if not files:
            print("No valid backup files found.")
            return
        
        print(f"\n{BOLD}Files found:{RESET}")
        for f in files:
            print(f" - {f}")
        
        size_gb = human_size_gb(total_size)
        print(f"\nFound {BOLD}{len(files)} valid backups{RESET} utilizing {BOLD}{size_gb}{RESET}.")
        
        if self.config.auto_yes:
            print(f"{MAGENTA}Auto-Yes (-y) active. Deleting files...{RESET}")
            reply = "y"
        else:
            try:
                reply = input("Delete them? (y/N) ").strip().lower()
            except EOFError:
                reply = "n"
        
        if reply == "y":
            for f in files:
                f.unlink()
                print(f"Deleted: {f.name}")
        else:
            print("Cancelled.")
    
    def _inspect_mel_fast_pass(self, filepath: Path) -> bool:
        """Run a quick check for MEL to avoid full scan if possible. Returns True if MEL detected."""
        spinner = Spinner("Checking EL Structure (Pre-Flight)... ")
        spinner.start()
        
        mel_detected = False
        pf_hevc = filepath.parent / f"inspect_pf_{int(time.time())}_{os.getpid()}.hevc"
        pf_rpu = pf_hevc.with_suffix(".rpu")
        pf_json = pf_hevc.with_suffix(".json")
        
        try:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(filepath),
                 "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-t", "1",
                 str(pf_hevc)],
                capture_output=True,
                stdin=subprocess.DEVNULL
            )
            
            if pf_hevc.exists() and pf_hevc.stat().st_size > 0:
                subprocess.run(
                    ["dovi_tool", "extract-rpu", str(pf_hevc), "-o", str(pf_rpu)],
                    capture_output=True
                )
                
                if pf_rpu.exists() and pf_rpu.stat().st_size > 0:
                    # Use cwd + relative paths to avoid comma parsing issues
                    subprocess.run(
                        ["dovi_tool", "export", "-i", pf_rpu.name, "-d", f"all={pf_json.name}"],
                        capture_output=True,
                        cwd=filepath.parent
                    )
                    
                    if pf_json.exists() and pf_json.stat().st_size > 0:
                        content = pf_json.read_text()
                        if '"el_type":"MEL"' in content:
                            mel_detected = True
        except Exception as e:
            if self.config.debug_mode:
                self.media.log(f"MEL fast pass exception: {e}")
        
        pf_hevc.unlink(missing_ok=True)
        pf_rpu.unlink(missing_ok=True)
        pf_json.unlink(missing_ok=True)
        spinner.stop()
        
        if mel_detected:
            print(f"\r\033[KChecking EL Structure... Done (MEL Detected).")
            print("---------------------------------------------------")
            print(f"{BOLD}VERDICT:{RESET}    {GREEN}MEL (Minimal Enhancement Layer){RESET}")
            print("---------------------------------------------------")
            print(f"{BOLD}ADVISORY:{RESET}")
            print("File is identified as MEL (empty enhancement layer).")
            print("It contains no video data to discard.")
            print("Absolutely safe to convert.")
            print("=" * 51)
            print()
            return True
        
        print(f"\r\033[KChecking EL Structure... Done (FEL Detected - Proceeding).")
        return False

    def _inspect_extract_rpu_loop(self, filepath: Path, temp_rpu: Path) -> bool:
        """Handle RPU extraction with retry logic. Returns True on success."""
        use_safe_mode = self.config.safe_mode
        
        while True:
            if not use_safe_mode:
                spinner = Spinner("Extracting RPU... ")
                spinner.start()
                
                try:
                    ffmpeg_proc = subprocess.Popen(
                        ["ffmpeg", "-v", "error", "-i", str(filepath),
                         "-map", f"0:{self.video_info.track_id}",
                         "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
                    self.active_procs.append(ffmpeg_proc)

                    dovi_proc = subprocess.Popen(
                        ["dovi_tool", "extract-rpu", "-", "-o", str(temp_rpu)],
                        stdin=ffmpeg_proc.stdout,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
                    self.active_procs.append(dovi_proc)

                    ffmpeg_proc.stdout.close()
                    _, dovi_stderr = dovi_proc.communicate()
                    _, ffmpeg_stderr = ffmpeg_proc.communicate()

                    self.active_procs.clear()

                    ffmpeg_status = ffmpeg_proc.returncode
                    dovi_status = dovi_proc.returncode

                    if ffmpeg_status != 0:
                        status = 1
                        if self.config.debug_mode:
                            self.media.log(f"ffmpeg failed: {ffmpeg_stderr.decode() if ffmpeg_stderr else 'Unknown error'}")
                    else:
                        status = dovi_status
                except Exception:
                    self.active_procs.clear()
                    status = 1
                
                spinner.stop()
                
                if status == 0 and temp_rpu.exists() and temp_rpu.stat().st_size > 0:
                    print(f"\r\033[KExtracting RPU... Done.")
                    return True
                else:
                    print(f"\r\033[KExtracting RPU... {RED}Failed.{RESET}")
                    temp_rpu.unlink(missing_ok=True)
                    
                    if self.config.auto_yes:
                        print(f"{MAGENTA}Retrying with Safe Mode (Auto-Yes).{RESET}")
                        use_safe_mode = True
                        continue
                    else:
                        try:
                            reply = input("Retry using Safe Mode (Extraction to Disk)? [Y/n] ").strip().lower()
                        except EOFError:
                            reply = "n"
                        if reply in ("y", ""):
                            use_safe_mode = True
                            continue
                        else:
                            print(f"{RED}Aborted.{RESET}")
                            return False
            else:
                raw_temp = filepath.parent / f"inspect_temp_{int(time.time())}_{os.getpid()}.hevc"
                
                spinner = Spinner("Extracting Track (Safe Mode)... ")
                spinner.start()
                
                ret, _, _ = self.media.run_logged(
                    ["mkvextract", str(filepath), "tracks", f"{self.video_info.track_id}:{raw_temp}"]
                )
                spinner.stop()
                
                if ret != 0:
                    print(f"\n{RED}Extracting Track Failed.{RESET}")
                    raw_temp.unlink(missing_ok=True)
                    return False
                
                print(f"\r\033[KExtracting Track... Done.")
                
                spinner = Spinner("Extracting RPU... ")
                spinner.start()
                
                ret, _, _ = self.media.run_logged(
                    ["dovi_tool", "extract-rpu", str(raw_temp), "-o", str(temp_rpu)]
                )
                spinner.stop()
                raw_temp.unlink(missing_ok=True)
                
                if ret == 0 and temp_rpu.exists() and temp_rpu.stat().st_size > 0:
                    print(f"\r\033[KExtracting RPU... Done.")
                    return True
                else:
                    print(f"\n{RED}RPU Extraction Failed.{RESET}")
                    temp_rpu.unlink(missing_ok=True)
                    return False

    def _inspect_analyze_peak(self, temp_json: Path) -> Tuple[int, int]:
        """Analyze L1 metadata for peak brightness. Returns (peak_nits, frame_count)."""
        spinner = Spinner("Calculating Peak Brightness (99.9th)... ")
        spinner.start()
        
        try:
            # 1. Try Fast Stream Parser (Low Memory)
            max_vals = self._extract_l1_stream(temp_json)
            
            if not max_vals:
                # 2. Fallback to Deep JSON Parse
                if self.config.debug_mode:
                    print(f"\n{MAGENTA}[!] Fast stream scan failed. Falling back to deep JSON parse...{RESET}")
                
                json_content = temp_json.read_text()
                data = json.loads(json_content)
                
                max_vals = []
                find_l1_values(data, max_vals)
            
            max_vals.sort()
            frame_count = len(max_vals)
            if frame_count > 0:
                idx = int(frame_count * 0.999)
                robust_peak = max_vals[min(idx, len(max_vals) - 1)]
            else:
                robust_peak = 0
                
        except Exception:
            frame_count = 0
            robust_peak = 0
        
        spinner.stop()
        
        if robust_peak > 0:
            robust_peak = pq_to_nits(robust_peak)
            
        return robust_peak, frame_count

    def _inspect_print_report(self, filepath: Path, peak_nits: int, frame_count: int) -> None:
        """Print the final inspection report."""
        print(f"\r\033[KCalculating Peak Brightness... Done.")
        
        bl_peak, is_default = self.media.get_bl_peak(filepath)
        threshold = bl_peak + 50
        
        if frame_count == 0:
            verdict = f"{MAGENTA}NO L1 METADATA{RESET}"
            advisory = f"{RED}WARNING:{RESET} No valid L1 brightness metadata found in FEL.\nThis is unusual for non-MEL files. Proceed with caution."
            robust_peak_str = "N/A"
        elif peak_nits > threshold:
            verdict = f"{RED}COMPLEX FEL (Active Brightness Expansion){RESET}"
            diff = peak_nits - bl_peak
            advisory = f"{BOLD}ADVISORY:{RESET}\nFEL Peak ({peak_nits} nits) exceeds Base Layer ({bl_peak} nits) by {diff} nits.\nThis indicates active brightness expansion in the FEL.\nConversion will likely cause clipping or tone-mapping errors."
            robust_peak_str = str(peak_nits)
        else:
            verdict = f"{GREEN}SIMPLE / SAFE{RESET}"
            advisory = f"{BOLD}ADVISORY:{RESET}\nFEL Peak ({peak_nits} nits) is within safe range of Base Layer ({bl_peak} nits).\nSafe to convert."
            robust_peak_str = str(peak_nits)
        
        default_suffix = " (default)" if is_default else ""
        print(f"{'Base Layer Peak (MaxCLL):':<26} {bl_peak} nits{default_suffix}")
        print(f"{'L1 Analysis:':<26} {frame_count} frames analyzed")
        print(f"{'FEL Peak Brightness:':<26} {robust_peak_str} nits")
        
        if frame_count == 0:
            print(f"{'Brightness Expansion:':<26} N/A")
        elif peak_nits > threshold:
            print(f"{'Brightness Expansion:':<26} {RED}+{diff} nits (Active){RESET}")
        else:
            print(f"{'Brightness Expansion:':<26} {GREEN}None (Safe){RESET}")
        print("---------------------------------------------------")
        print(f"{BOLD}VERDICT:{RESET}    {verdict}")
        print("---------------------------------------------------")
        print(advisory)
        print("=" * 51)
        print()

    def cmd_inspect(self, filepath: Path) -> None:
        """Full frame-by-frame RPU inspection."""
        if not filepath.exists():
            print(f"{RED}Error: File '{filepath}' not found.{RESET}")
            return
        
        # Basic validation
        self.video_info = self.media.get_video_info(filepath)
        mi = self.video_info.mi_info_string
        
        if "dvhe.07" not in mi and "Profile 7" not in mi:
            print(f"{RED}Error: File is not Dolby Vision Profile 7.{RESET}")
            print(f"Detected: {self.dovi_status} (Info: {mi})")
            return
        
        print()
        print("=" * 51)
        print("FULL RPU STRUCTURE INSPECTION")
        print("=" * 51)
        print(f"File:       {BOLD}{filepath.name}{RESET}")
        print("Format:     DV Profile 7 (Scanning...)")
        print("---------------------------------------------------")
        
        # 1. MEL Fast Pass
        if self._inspect_mel_fast_pass(filepath):
            return
            
        # 2. Extract RPU
        ts = int(time.time())
        temp_rpu = filepath.parent / f"inspect_{ts}_{os.getpid()}.rpu"
        temp_json = filepath.parent / f"inspect_{ts}_{os.getpid()}.json"
        self.temp_files.extend([temp_rpu, temp_json])
        
        if not self._inspect_extract_rpu_loop(filepath, temp_rpu):
            return
            
        # 3. Export Metadata
        spinner = Spinner("Exporting Metadata... ")
        spinner.start()
        
        # Use cwd + relative paths to avoid comma parsing issues
        ret, _, _ = self.media.run_logged(
            ["dovi_tool", "export", "-i", temp_rpu.name, "-d", f"all={temp_json.name}"],
            cwd=filepath.parent
        )
        
        spinner.stop()
        temp_rpu.unlink(missing_ok=True)
        
        if ret != 0 or not temp_json.exists() or temp_json.stat().st_size == 0:
            print(f"\r\033[KExporting Metadata... {RED}Failed.{RESET}")
            temp_json.unlink(missing_ok=True)
            return
        
        print(f"\r\033[KExporting Metadata... Done.")
        
        # 4. Analyze
        peak_nits, frame_count = self._inspect_analyze_peak(temp_json)
        temp_json.unlink(missing_ok=True)
        
        # 5. Print Report
        self._inspect_print_report(filepath, peak_nits, frame_count)


# =============================================================================
# ARGUMENT PARSING
# =============================================================================

# Valid flags per command - derived from FLAGS registry
COMMAND_FLAGS = {
    cmd: {f.field for f in FLAGS if cmd in f.commands}
    for cmd in ["convert", "scan", "inspect", "cleanup", "update-check", "backup", "restore"]
}
COMMAND_FLAGS["help"] = set()  # help takes no flags

# Legacy command mapping for helpful error messages
LEGACY_COMMANDS = {
    "-convert": "convert",
    "-scan": "scan",
    "-check": "scan",
    "-batch": "convert",  # batch merged into convert
    "-inspect": "inspect",
    "-cleanup": "cleanup",
    "-update-check": "update-check",
    "-help": "help",
}


def parse_args(argv: List[str]) -> ParsedArgs:
    """Parse command-line arguments into structured result."""
    parsed = ParsedArgs()
    args = argv[1:]  # Skip script name

    if not args:
        return parsed  # No command

    # Handle --help anywhere in arguments
    if "--help" in args:
        HelpText.print_help()
        sys.exit(0)

    # Check for legacy command syntax
    if args[0] in LEGACY_COMMANDS:
        new_cmd = LEGACY_COMMANDS[args[0]]
        print(f"{RED}Error: '{args[0]}' syntax was removed in v8.0.0{RESET}")
        print(f"Use instead: dovi_convert {new_cmd} ...")
        print(f"Run 'dovi_convert help' for the new syntax.")
        sys.exit(1)

    # Extract command
    parsed.command = args[0]
    rest = args[1:]

    # Parse flags and arguments using FLAGS registry
    i = 0
    while i < len(rest):
        arg = rest[i]

        # Look up flag in registry
        flag = FLAGS_BY_LONG.get(arg) or FLAGS_BY_SHORT.get(arg)

        if flag:
            if flag.takes_value:
                # Check if next arg exists and isn't a flag
                has_value = (i + 1 < len(rest) and not rest[i + 1].startswith("-"))

                if has_value:
                    value = rest[i + 1]
                    # Handle type conversion based on field
                    if flag.field in ("temp_dir", "output_dir"):
                        setattr(parsed, flag.field, Path(value))
                    elif flag.field == "backup_source":
                        parsed.backup_source = value
                    elif flag.field == "recursive":
                        parsed.recursive = True
                        try:
                            parsed.recursive_depth = int(value)
                        except ValueError:
                            print(f"{RED}Error: --recursive requires an integer depth.{RESET}")
                            sys.exit(1)
                    i += 2
                elif flag.optional_value:
                    # Flag like -r without a number
                    if flag.field == "recursive":
                        parsed.recursive = True
                        parsed.recursive_depth = flag.default_value
                    i += 1
                else:
                    print(f"{RED}Error: {arg} requires an argument.{RESET}")
                    sys.exit(1)
            else:
                # Boolean flag
                setattr(parsed, flag.field, True)
                i += 1
            continue

        # Unknown flag
        if arg.startswith("-"):
            print(f"{RED}Error: Unknown flag '{arg}'{RESET}")
            print("Run 'dovi_convert help' for available options.")
            sys.exit(1)

        # Positional argument (file or directory)
        path = Path(arg)
        if path.is_dir():
            parsed.directories.append(path)
        else:
            # Treat as file (let command handle non-existent paths)
            parsed.files.append(path)

        i += 1

    return parsed


def dispatch_command(app: 'DoviConvertApp', parsed: ParsedArgs) -> int:
    """Route to appropriate command handler. Returns exit code."""
    
    # Validate flags are applicable to this command
    if parsed.command and parsed.command not in ("help", ""):
        allowed = COMMAND_FLAGS.get(parsed.command, set())
        
        # Map of flag names to their display names and values
        # Validate using FLAGS registry
        for flag in FLAGS:
            value = getattr(parsed, flag.field, None)
            # Special case: recursive stores bool in .recursive, depth in .recursive_depth
            if flag.field == "recursive":
                value = parsed.recursive

            if value and flag.field not in allowed:
                print(f"{RED}Error: {flag.long} is not applicable to '{parsed.command}' command.{RESET}")
                return 1
    
    # Transfer parsed flags to app config
    app.config.force_mode = parsed.force
    app.config.safe_mode = parsed.safe
    app.config.auto_yes = parsed.yes
    app.config.debug_mode = parsed.debug
    app.config.include_simple = parsed.include_simple
    app.config.delete_backup = parsed.delete_backup
    app.config.hdr10_mode = parsed.hdr10
    app.config.candidates_only = parsed.candidates_only
    app.config.verbose_mode = parsed.verbose
    app.config.create_backup = parsed.create_backup
    app.config.temp_dir = parsed.temp_dir
    app.config.output_dir = parsed.output_dir
    app.config.backup_source = Path(parsed.backup_source) if parsed.backup_source else None

    # Validate temp/output dirs if specified
    if parsed.temp_dir:
        if not parsed.temp_dir.exists():
            print(f"{RED}Error: Temp directory does not exist: {parsed.temp_dir}{RESET}")
            return 1
        if not parsed.temp_dir.is_dir():
            print(f"{RED}Error: Temp path is not a directory: {parsed.temp_dir}{RESET}")
            return 1
        # Writability check
        try:
            test_file = parsed.temp_dir / ".dovi_convert_write_test"
            test_file.touch()
            test_file.unlink()
        except Exception:
            print(f"{RED}Error: Temp directory is not writable: {parsed.temp_dir}{RESET}")
            return 1
    
    if parsed.output_dir:
        try:
            parsed.output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"{RED}Error: Could not create output directory: {parsed.output_dir}{RESET}")
            print(f"       {e}")
            return 1
        if not parsed.output_dir.is_dir():
            print(f"{RED}Error: Output path is not a directory: {parsed.output_dir}{RESET}")
            return 1
        # Writability check
        try:
            test_file = parsed.output_dir / ".dovi_convert_write_test"
            test_file.touch()
            test_file.unlink()
        except Exception:
            print(f"{RED}Error: Output directory is not writable: {parsed.output_dir}{RESET}")
            return 1
    
    # Dispatch to command handlers
    app.current_command = parsed.command

    if parsed.command == "":
        UpdateChecker.check_foreground()
        HelpText.print_usage()
        UpdateChecker.check_background()
        return 0

    elif parsed.command == "help":
        UpdateChecker.check_foreground()
        HelpText.print_help()
        return 0
    
    elif parsed.command == "scan":
        inputs = parsed.files + parsed.directories
        files = app.collect_inputs(
            [str(p) for p in inputs] if inputs else [],
            "scan",
            parsed.recursive_depth
        )
        if len(files) == 1:
            app.cmd_check_single(files[0])
        else:
            app.cmd_check_all(parsed.recursive_depth, files if inputs else None)
        return 0
    
    elif parsed.command == "convert":
        if not (parsed.files or parsed.directories):
            print("Usage: dovi_convert convert [file|dir] ...")
            return 1
        
        # Determine mode: directories trigger batch-style pre-scan
        inputs = parsed.files + parsed.directories
        batch_mode = len(parsed.directories) > 0
        
        # --hdr10 restriction in batch mode
        if batch_mode and parsed.hdr10:
            print(f"{RED}Error: --hdr10 is not available when processing directories.{RESET}")
            print("       HDR10 conversion should be done one file at a time.")
            return 1
        
        # Warn if --recursive used without directories
        if parsed.recursive and not parsed.directories:
            print(f"{MAGENTA}Note: --recursive ignored (no directories provided).{RESET}")
        
        # Block mixed inputs (files + directories) with -o to prevent output path collisions
        if batch_mode and app.config.output_dir and parsed.files:
            print(f"{RED}Error: Cannot use -o with mixed directories and files.{RESET}")
            print(f"  Reason: Files with the same name could overwrite each other in the output folder.")
            print(f"  Solution: Run directories and files separately:")
            print(f"    dovi convert {parsed.directories[0]} -o {app.config.output_dir}")
            print(f"    dovi convert {parsed.files[0]} -o {app.config.output_dir}")
            return 1
        
        files = app.collect_inputs(
            [str(p) for p in inputs],
            "convert",
            parsed.recursive_depth
        )
        if not files:
            print(f"{RED}Error: No valid MKV files found.{RESET}")
            return 1
        
        if batch_mode:
            # Batch-style: pre-scan with summary and confirmation
            source_dirs = parsed.directories if parsed.directories else None
            app.cmd_batch(parsed.recursive_depth, files, source_dirs)
            return 0
        elif len(files) == 1:
            return app.cmd_convert(files[0], "manual")
        else:
            # Multiple explicit files: inline processing
            success_count = 0
            fail_list = []
            for idx, filepath in enumerate(files, 1):
                print(f"\n{'=' * 51}")
                print(f"[{idx}/{len(files)}] {filepath.name}")
                print("=" * 51)
                result = app.cmd_convert(filepath, "manual")
                if result == 0:
                    success_count += 1
                elif result == 130:
                    break
                else:
                    fail_list.append(filepath.name)
            print(f"\n{'=' * 51}")
            print(f"Processed {success_count} of {len(files)} files.")
            if fail_list:
                print(f"Failed: {', '.join(fail_list)}")
            print("=" * 51)
            print()
            return 0 if not fail_list else 1
    
    elif parsed.command == "inspect":
        if not parsed.files:
            print("Usage: dovi_convert inspect [file]")
            return 1
        app.cmd_inspect(parsed.files[0])
        return 0
    
    elif parsed.command == "backup":
        if not parsed.files:
            print("Usage: dovi_convert backup <file>")
            return 1
        if len(parsed.files) > 1:
            print(f"{RED}Error: backup command only supports single files.{RESET}")
            return 1
        return app.cmd_backup(parsed.files[0])

    elif parsed.command == "restore":
        if not parsed.files:
            print("Usage: dovi_convert restore <file>")
            return 1
        if len(parsed.files) > 1:
            print(f"{RED}Error: restore command only supports single files.{RESET}")
            return 1
        return app.cmd_restore(parsed.files[0])

    elif parsed.command == "cleanup":
        app.cmd_cleanup(parsed.recursive)
        return 0

    elif parsed.command == "update-check":
        UpdateChecker.check_manual()
        return 0
    
    else:
        print(f"{RED}Unknown command: {parsed.command}{RESET}")
        print("Run 'dovi_convert help' for available commands.")
        return 1


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def main() -> None:
    """Main entry point."""
    # Pre-flight: Ghost directory check
    try:
        Path.cwd()
    except OSError:
        print(f"{RED}Error: Ghost directory detected.{RESET} Please 'cd .' or restart terminal.")
        sys.exit(1)
    
    # Pre-flight: Dependency check
    missing = DependencyManager.find_missing()
    if missing:
        print(f"{RED}Missing dependencies:{RESET} {' '.join(missing)}")
        print()
        try:
            reply = input("Would you like to install them automatically? (y/N) ").strip().lower()
        except EOFError:
            reply = "n"
        
        if reply == "y":
            if sys.platform == "darwin" and not shutil.which("brew"):
                print()
                print(f"{MAGENTA}Homebrew is not installed.{RESET}")
                print("It's the recommended way to install dependencies on macOS.")
                print()
                print("Install it from: https://brew.sh")
                print()
                print("Then run dovi_convert again.")
                sys.exit(1)
            DependencyManager.install_dependencies(missing)
        else:
            print()
            print("Please install the missing dependencies manually:")
            for dep in missing:
                if dep == "dovi_tool":
                    print(f"  - dovi_tool: https://github.com/quietvoid/dovi_tool")
                else:
                    print(f"  - {dep}")
            
            if sys.platform == "darwin" and not shutil.which("brew"):
                print()
                print("Tip: Install Homebrew (https://brew.sh) - a universal package manager.")
                print("     Once installed, dovi_convert will use it to install dependencies automatically.")
            sys.exit(1)
    
    # Parse arguments
    parsed = parse_args(sys.argv)
    
    # Create app with config
    config = Config(debug_mode=parsed.debug)
    app = DoviConvertApp(config)
    
    # Dispatch command
    exit_code = dispatch_command(app, parsed)
    
    # Trigger background update check on clean exit
    UpdateChecker.check_background()
    
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
