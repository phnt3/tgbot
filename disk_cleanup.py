"""Disk space analysis and safe cleanup for PC Remote.

Scanning and deletion are best-effort and never follow symlinks. Cleanup only
removes the *contents* of well-known temporary/cache folders and never touches
user documents. Every deletion is reported with files removed and bytes freed.
"""

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil

logger = logging.getLogger(__name__)

DEFAULT_SCAN_BUDGET_SECONDS = 20.0

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def human_size(num_bytes: Optional[float]) -> str:
    """Format a byte count as a short human-readable string."""
    value = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{value:.0f} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def get_disk_overview() -> List[Dict[str, Any]]:
    """Return usage information for every mounted fixed disk."""
    result: List[Dict[str, Any]] = []
    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception as e:
        logger.error("Failed to enumerate disks: %s", e)
        return result

    for part in partitions:
        if not part.fstype or "cdrom" in (part.opts or "").lower():
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        result.append(
            {
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "percent": usage.percent,
            }
        )
    return result


def _env_path(name: str, *parts: str) -> Optional[Path]:
    """Build a path from an environment variable, or None when unset."""
    base = os.environ.get(name)
    if not base:
        return None
    return Path(base).joinpath(*parts)


def default_cleanup_targets() -> List[Dict[str, Any]]:
    """Return well-known temporary/cache locations with a cleanable flag.

    Windows Update downloads are reported but not deleted: clearing them while
    the update service is running can leave Windows Update in a bad state.
    """
    candidates: List[Tuple[str, Optional[Path], bool]] = [
        ("User temp files", _env_path("TEMP"), True),
        ("Windows temp files", _env_path("WINDIR", "Temp"), True),
        ("Crash dumps", _env_path("LOCALAPPDATA", "CrashDumps"), True),
        ("Chrome cache", _env_path("LOCALAPPDATA", "Google", "Chrome",
                                   "User Data", "Default", "Cache"), True),
        ("Edge cache", _env_path("LOCALAPPDATA", "Microsoft", "Edge",
                                 "User Data", "Default", "Cache"), True),
        ("Thumbnail cache", _env_path("LOCALAPPDATA", "Microsoft", "Windows",
                                      "Explorer"), True),
        ("Windows Update cache", _env_path("WINDIR", "SoftwareDistribution",
                                           "Download"), False),
    ]

    targets: List[Dict[str, Any]] = []
    seen = set()
    for name, path, cleanable in candidates:
        if path is None:
            continue
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        targets.append({"name": name, "path": path, "cleanable": cleanable})
    return targets


def _tree_size(path: Path, deadline: float) -> Tuple[int, int, bool]:
    """Recursively measure a directory (best-effort, time-budgeted).

    Returns:
        (bytes, files, partial) where partial is True when the deadline hit.
    """
    total = 0
    files = 0
    partial = False
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if time.monotonic() > deadline:
                    return total, files, True
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        sub_bytes, sub_files, sub_partial = _tree_size(
                            Path(entry.path), deadline
                        )
                        total += sub_bytes
                        files += sub_files
                        partial = partial or sub_partial
                    else:
                        total += entry.stat(follow_symlinks=False).st_size
                        files += 1
                except OSError:
                    continue
    except OSError:
        return total, files, partial
    return total, files, partial


def scan_cleanup_targets(
    targets: Optional[List[Dict[str, Any]]] = None,
    budget_seconds: float = DEFAULT_SCAN_BUDGET_SECONDS,
) -> List[Dict[str, Any]]:
    """Measure every cleanup target.

    Args:
        targets: Target list; defaults to ``default_cleanup_targets()``.
        budget_seconds: Total scan budget. When exceeded remaining targets are
            reported with size 0 and ``partial=True``.

    Returns:
        List of dicts sorted by size (largest first) with keys: name, path,
        cleanable, exists, size_bytes, files, partial.
    """
    deadline = time.monotonic() + max(0.1, float(budget_seconds))
    scanned: List[Dict[str, Any]] = []
    for target in targets if targets is not None else default_cleanup_targets():
        path = Path(target["path"])
        info = {
            "name": target.get("name", path.name),
            "path": path,
            "cleanable": bool(target.get("cleanable", True)),
            "exists": path.is_dir(),
            "size_bytes": 0,
            "files": 0,
            "partial": False,
        }
        if info["exists"]:
            if time.monotonic() >= deadline:
                info["partial"] = True
            else:
                size, files, partial = _tree_size(path, deadline)
                info.update(size_bytes=size, files=files, partial=partial)
        scanned.append(info)
    scanned.sort(key=lambda item: item["size_bytes"], reverse=True)
    return scanned


def _delete_tree(path: Path) -> Tuple[int, int, int]:
    """Delete the contents of ``path`` without following symlinks.

    Returns:
        (bytes_freed, files_removed, errors)
    """
    freed = 0
    files = 0
    errors = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_symlink() or not entry.is_dir(
                        follow_symlinks=False
                    ):
                        size = entry.stat(follow_symlinks=False).st_size
                        os.unlink(entry.path)
                        freed += size
                        files += 1
                    else:
                        sub_freed, sub_files, sub_errors = _delete_tree(
                            Path(entry.path)
                        )
                        freed += sub_freed
                        files += sub_files
                        errors += sub_errors
                        try:
                            os.rmdir(entry.path)
                        except OSError:
                            pass
                except OSError:
                    errors += 1
    except OSError:
        errors += 1
    return freed, files, errors


def clean_targets(
    targets: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Delete the contents of all cleanable targets.

    Returns:
        Dict with keys: freed_bytes, removed_files, errors, skipped (list of
        target names that are report-only or missing).
    """
    freed = 0
    removed = 0
    errors = 0
    skipped: List[str] = []
    cleaned: List[str] = []

    for target in targets if targets is not None else default_cleanup_targets():
        path = Path(target["path"])
        name = target.get("name", path.name)
        if not target.get("cleanable", True):
            skipped.append(f"{name} (report only)")
            continue
        if not path.is_dir():
            skipped.append(f"{name} (not found)")
            continue
        target_freed, target_files, target_errors = _delete_tree(path)
        freed += target_freed
        removed += target_files
        errors += target_errors
        cleaned.append(name)

    return {
        "freed_bytes": freed,
        "removed_files": removed,
        "errors": errors,
        "skipped": skipped,
        "cleaned": cleaned,
    }


def get_recycle_bin_size() -> int:
    """Return the total size of files in the Recycle Bin (0 when unknown)."""
    if os.name != "nt":
        return 0
    script = (
        "(Get-ChildItem -LiteralPath 'C:\\$Recycle.Bin' -Force -Recurse "
        "-File -ErrorAction SilentlyContinue | "
        "Measure-Object -Property Length -Sum).Sum"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             script],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_NO_WINDOW,
        )
        raw = (result.stdout or "").strip()
        return int(float(raw)) if raw else 0
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        logger.warning("Failed to measure Recycle Bin: %s", e)
        return 0


def empty_recycle_bin() -> Tuple[bool, str]:
    """Empty the Recycle Bin for all drives.

    Returns:
        Tuple of (success, message).
    """
    if os.name != "nt":
        return False, "Recycle Bin is only available on Windows."
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"],
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Failed to empty Recycle Bin: {e}"
    if result.returncode == 0:
        return True, "Recycle Bin emptied."
    return False, "Failed to empty Recycle Bin (some files may be in use)."