"""Safe file browsing, download and upload helpers for PC Remote.

The file manager exposes the local filesystem to authorized Telegram users.
Every operation is defensive: directory listings skip unreadable entries,
downloads are capped to the Telegram Bot API limit, and uploads are written
into a dedicated folder with sanitized names and no accidental overwrite.
"""

import fnmatch
import logging
import os
import re
import shutil
import stat as stat_module
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil

logger = logging.getLogger(__name__)

# Telegram Bot API allows bots to send files up to 50 MB; stay a bit below.
MAX_SEND_BYTES = 45 * 1024 * 1024

_BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def format_size(num_bytes: Optional[float]) -> str:
    """Format a byte count for display."""
    value = float(num_bytes or 0)
    if value < 1024:
        return f"{value:.0f} B"
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} TB"


def list_directory(path: Path) -> Dict[str, Any]:
    """List one directory, directories first then files (case-insensitive).

    Args:
        path: Directory to list.

    Returns:
        Dict with keys: path (Path), parent (Optional[Path]), entries (list of
        dicts with name, path, is_dir, size).

    Raises:
        NotADirectoryError: If path is not an existing directory.
    """
    directory = Path(path)
    if not directory.is_dir():
        raise NotADirectoryError(str(directory))

    entries: List[Dict[str, Any]] = []
    try:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    size = 0 if is_dir else entry.stat(
                        follow_symlinks=False
                    ).st_size
                except OSError:
                    continue
                entries.append(
                    {
                        "name": entry.name,
                        "path": Path(entry.path),
                        "is_dir": is_dir,
                        "size": size,
                    }
                )
    except OSError as e:
        logger.warning("Cannot list %s: %s", directory, e)
        raise NotADirectoryError(str(directory)) from e

    entries.sort(key=lambda item: (not item["is_dir"], item["name"].casefold()))
    parent = directory.parent if directory.parent != directory else None
    return {"path": directory, "parent": parent, "entries": entries}


def _is_reparse_point(entry: os.DirEntry) -> bool:
    """Return True for junctions/symlinks that could create directory loops."""
    try:
        attributes = getattr(
            entry.stat(follow_symlinks=False), "st_file_attributes", 0
        )
    except OSError:
        return False
    flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def search_files(
    root: Path,
    query: str,
    max_depth: int = 4,
    max_dirs: int = 2000,
    max_results: int = 30,
    time_budget: float = 8.0,
) -> Dict[str, Any]:
    """Search file and folder names below ``root`` with hard limits.

    Matching is case-insensitive substring matching; when ``query`` contains
    ``*`` or ``?`` it is treated as a shell-style wildcard pattern. Directory
    junctions and symlinks are never descended into, so a recursive search can
    not loop forever.

    Args:
        root: Directory to start from.
        query: File name fragment or wildcard pattern.
        max_depth: How many directory levels below ``root`` to visit.
        max_dirs: Maximum number of directories scanned.
        max_results: Maximum number of matches returned.
        time_budget: Soft wall-clock limit in seconds.

    Returns:
        Dict with keys: root (Path), query (str), results (list of dicts with
        name, path, is_dir, size), truncated (bool), scanned_dirs (int).

    Raises:
        NotADirectoryError: If ``root`` is not an existing directory.
        ValueError: If the query is empty or too long.
    """
    directory = Path(root)
    if not directory.is_dir():
        raise NotADirectoryError(str(directory))

    term = str(query or "").strip()
    if not term:
        raise ValueError("Search text is empty.")
    if len(term) > 120:
        raise ValueError("Search text is too long (120 characters max).")

    depth_limit = max(0, int(max_depth))
    dir_limit = max(1, int(max_dirs))
    result_limit = max(1, int(max_results))
    deadline = time.monotonic() + max(0.1, float(time_budget))

    folded = term.casefold()
    wildcard = "*" in term or "?" in term
    results: List[Dict[str, Any]] = []
    scanned = 0
    truncated = False
    queue: deque = deque([(directory, 0)])

    while queue:
        if scanned >= dir_limit or time.monotonic() >= deadline:
            truncated = True
            break

        current, depth = queue.popleft()
        scanned += 1
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    if time.monotonic() >= deadline:
                        truncated = True
                        break

                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue

                    name = entry.name
                    matches = (
                        fnmatch.fnmatch(name.casefold(), folded)
                        if wildcard
                        else folded in name.casefold()
                    )
                    if matches:
                        size = 0
                        if not is_dir:
                            try:
                                size = entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                size = 0
                        results.append(
                            {
                                "name": name,
                                "path": Path(entry.path),
                                "is_dir": is_dir,
                                "size": size,
                            }
                        )
                        if len(results) >= result_limit:
                            truncated = True
                            break

                    if (
                        is_dir
                        and depth < depth_limit
                        and not _is_reparse_point(entry)
                    ):
                        queue.append((Path(entry.path), depth + 1))
        except OSError as e:
            logger.debug("Cannot search %s: %s", current, e)
            continue

        if len(results) >= result_limit or truncated:
            break

    return {
        "root": directory,
        "query": term,
        "results": results,
        "truncated": truncated,
        "scanned_dirs": scanned,
    }


def read_for_send(path: Path) -> Tuple[bytes, str]:
    """Read a file for sending to Telegram.

    Returns:
        Tuple of (bytes, filename).

    Raises:
        FileNotFoundError: If the path is missing or not a file.
        ValueError: If the file exceeds the send limit.
    """
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(str(target))
    size = target.stat().st_size
    if size > MAX_SEND_BYTES:
        raise ValueError(
            f"File is {format_size(size)}; the limit is "
            f"{format_size(MAX_SEND_BYTES)}."
        )
    return target.read_bytes(), target.name


def sanitize_filename(name: str, fallback: str = "upload.bin") -> str:
    """Return a safe basename derived from an untrusted file name."""
    base = Path(str(name or "")).name
    base = _BAD_FILENAME_CHARS.sub("_", base).strip(" .")
    if not base:
        base = fallback
    # Windows reserved device names cannot be created as regular files.
    stem = base.split(".")[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"COM\d|LPT\d", stem):
        base = f"_{base}"
    return base[:150]


def default_upload_dir() -> Path:
    """Return the folder where Telegram uploads are stored."""
    from config import config

    return Path(config["DATA_DIR"]) / "uploads"


def save_upload(filename: str, data: bytes, dest_dir: Path) -> Path:
    """Write uploaded bytes into ``dest_dir`` without overwriting files.

    Returns:
        The path that was written.
    """
    directory = Path(dest_dir)
    directory.mkdir(parents=True, exist_ok=True)

    safe_name = sanitize_filename(filename)
    target = directory / safe_name
    if target.exists():
        stem, suffix = target.stem, target.suffix
        for index in range(1, 1000):
            candidate = directory / f"{stem}_{index}{suffix}"
            if not candidate.exists():
                target = candidate
                break
        else:
            target = directory / f"{stem}_{int(time.time())}{suffix}"

    target.write_bytes(data)
    return target


# --- Drives and file operations --------------------------------------------


def list_drives() -> List[Dict[str, Any]]:
    """Return mounted drives with free-space information.

    Only real, currently mounted partitions are included. Unreadable drives
    still appear with zeroed sizes so the browser never hides hardware.
    """
    drives: List[Dict[str, Any]] = []
    seen: set = set()
    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception as e:
        logger.error("Failed to list drives: %s", e)
        return drives

    for partition in partitions:
        mountpoint = str(partition.mountpoint or "")
        if not mountpoint:
            continue
        path = Path(mountpoint)
        try:
            if not path.exists():
                continue
        except OSError:
            continue
        key = os.path.normcase(os.path.abspath(mountpoint))
        if key in seen:
            continue
        seen.add(key)

        total = free = 0
        percent = 0.0
        try:
            usage = psutil.disk_usage(mountpoint)
            total = int(usage.total)
            free = int(usage.free)
            percent = float(usage.percent)
        except (OSError, ValueError):
            pass

        drives.append(
            {
                "name": str(partition.device or mountpoint),
                "path": path,
                "total": total,
                "free": free,
                "percent": percent,
                "fstype": str(partition.fstype or ""),
            }
        )

    drives.sort(key=lambda item: str(item["path"]).casefold())
    return drives


def _norm_path(path: Any) -> str:
    """Return a normalized absolute path for comparisons."""
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def _is_within(child: Any, parent: Any) -> bool:
    """Return True when ``child`` is ``parent`` or lives below it."""
    child_path = _norm_path(child)
    parent_path = _norm_path(parent).rstrip("\\/")
    if not parent_path:
        return False
    return child_path == parent_path or child_path.startswith(
        parent_path + os.sep
    )


def _critical_roots() -> List[Path]:
    """System folders that must never be modified through the bot."""
    roots: List[Path] = []
    for name in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)",
                 "ProgramData"):
        raw = os.environ.get(name)
        if raw:
            roots.append(Path(raw))
    system_drive = os.environ.get("SystemDrive", "C:")
    roots.append(Path(f"{system_drive}\\Windows"))
    return roots


def _user_roots() -> List[Path]:
    """User profile roots that must never be deleted or replaced."""
    roots = [Path.home()]
    system_drive = os.environ.get("SystemDrive", "C:")
    roots.append(Path(f"{system_drive}\\Users"))
    return roots


def _modify_problem(target: Path) -> Optional[str]:
    """Return a human-readable reason when ``target`` is protected."""
    try:
        if target.parent == target:
            return "Drive roots cannot be modified."
    except OSError:
        pass

    for root in _critical_roots():
        if _is_within(target, root):
            return f"Refused: {target} is inside a protected system folder."

    normalized = _norm_path(target)
    for root in _user_roots():
        if normalized == _norm_path(root):
            return "Refused: user profile roots cannot be modified."
    return None


def _is_reparse_path(path: Any) -> bool:
    """Return True when ``path`` itself is a junction or symlink."""
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except (OSError, AttributeError):
        return False
    flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def validate_name(name: str) -> str:
    """Validate one Windows file/folder name and return it stripped.

    Raises:
        ValueError: When the name is empty, too long, contains path
            separators or Windows-forbidden characters, or is reserved.
    """
    value = str(name or "").strip()
    if not value:
        raise ValueError("Name is empty.")
    if len(value) > 150:
        raise ValueError("Name is too long (150 characters max).")
    if value in (".", ".."):
        raise ValueError("Invalid name.")
    if _BAD_FILENAME_CHARS.search(value):
        raise ValueError('Name contains invalid characters: < > : " / \\ | ? *')
    if value.endswith(".") or value.endswith(" "):
        raise ValueError("Windows names cannot end with a space or a dot.")
    stem = value.split(".")[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(
        r"COM\d|LPT\d", stem
    ):
        raise ValueError("That name is reserved by Windows.")
    return value


def make_directory(parent: Path, name: str) -> Path:
    """Create one folder inside ``parent`` without overwriting anything."""
    directory = Path(parent)
    if not directory.is_dir():
        raise NotADirectoryError(str(directory))
    safe_name = validate_name(name)
    target = directory / safe_name
    problem = _modify_problem(target)
    if problem:
        raise PermissionError(problem)
    if target.exists():
        raise FileExistsError(f"{safe_name} already exists.")
    target.mkdir(parents=False)
    return target


def rename_path(path: Path, new_name: str) -> Path:
    """Rename one file or folder in place without overwriting."""
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(str(source))
    problem = _modify_problem(source)
    if problem:
        raise PermissionError(problem)
    safe_name = validate_name(new_name)
    target = source.with_name(safe_name)
    if target.exists() and _norm_path(target) != _norm_path(source):
        raise FileExistsError(f"{safe_name} already exists.")
    os.rename(source, target)
    return target


def _copy_tree(source: Path, target: Path) -> Dict[str, int]:
    """Recursively copy a folder, never following junctions or symlinks."""
    stats = {"files": 0, "bytes": 0, "skipped": 0, "errors": 0}

    def copy_dir(src: Path, dst: Path) -> None:
        dst.mkdir(parents=False, exist_ok=False)
        try:
            with os.scandir(src) as iterator:
                entries = list(iterator)
        except OSError as e:
            logger.warning("Cannot read %s: %s", src, e)
            stats["errors"] += 1
            return

        for entry in entries:
            try:
                if _is_reparse_point(entry):
                    stats["skipped"] += 1
                    continue
                destination = dst / entry.name
                if entry.is_dir(follow_symlinks=False):
                    copy_dir(Path(entry.path), destination)
                else:
                    shutil.copy2(entry.path, destination)
                    stats["files"] += 1
                    stats["bytes"] += entry.stat(
                        follow_symlinks=False
                    ).st_size
            except OSError as e:
                logger.warning("Cannot copy %s: %s", entry.path, e)
                stats["errors"] += 1

    if _is_reparse_path(source):
        raise PermissionError("Refusing to copy a junction or symlink.")
    copy_dir(source, target)
    return stats


def copy_path(source: Path, dest_dir: Path) -> Path:
    """Copy a file or folder into ``dest_dir`` without overwriting."""
    src = Path(source)
    destination_dir = Path(dest_dir)
    if not src.exists():
        raise FileNotFoundError(str(src))
    if not destination_dir.is_dir():
        raise NotADirectoryError(str(destination_dir))
    target = destination_dir / src.name
    if src.is_dir() and _is_within(destination_dir, src):
        raise ValueError("Cannot copy a folder into itself.")
    problem = _modify_problem(target)
    if problem:
        raise PermissionError(problem)
    if target.exists():
        raise FileExistsError(f"{target.name} already exists.")
    if src.is_dir():
        _copy_tree(src, target)
    else:
        if _is_reparse_path(src):
            raise PermissionError("Refusing to copy a junction or symlink.")
        shutil.copy2(src, target)
    return target


def _same_drive(source: Any, target: Any) -> bool:
    """Return True when both paths live on the same volume."""
    return os.path.normcase(Path(source).drive) == os.path.normcase(
        Path(target).drive
    )


def move_path(source: Path, dest_dir: Path) -> Path:
    """Move a file or folder into ``dest_dir`` without overwriting."""
    src = Path(source)
    destination_dir = Path(dest_dir)
    if not src.exists():
        raise FileNotFoundError(str(src))
    if not destination_dir.is_dir():
        raise NotADirectoryError(str(destination_dir))
    problem = _modify_problem(src)
    if problem:
        raise PermissionError(problem)
    target = destination_dir / src.name
    if src.is_dir() and _is_within(destination_dir, src):
        raise ValueError("Cannot move a folder into itself.")
    problem = _modify_problem(target)
    if problem:
        raise PermissionError(problem)
    if target.exists():
        raise FileExistsError(f"{target.name} already exists.")
    if _is_reparse_path(src):
        raise PermissionError("Refusing to move a junction or symlink.")

    if _same_drive(src, target):
        os.rename(src, target)
    else:
        if src.is_dir():
            _copy_tree(src, target)
        else:
            shutil.copy2(src, target)
        remove_stats = delete_path(src)
        if remove_stats["errors"]:
            raise OSError("Copied, but some original files could not be removed.")
    return target


def delete_path(path: Path) -> Dict[str, int]:
    """Delete a file or folder tree without following junctions/symlinks."""
    target = Path(path)
    if not target.exists() and not _is_reparse_path(target):
        raise FileNotFoundError(str(target))
    problem = _modify_problem(target)
    if problem:
        raise PermissionError(problem)

    stats = {"deleted": 0, "bytes": 0, "errors": 0, "skipped": 0}

    def remove(current: Path) -> None:
        if _is_reparse_path(current):
            try:
                if current.is_symlink():
                    current.unlink()
                else:
                    os.rmdir(current)
                stats["deleted"] += 1
            except OSError as e:
                logger.warning("Cannot remove link %s: %s", current, e)
                stats["errors"] += 1
            return

        try:
            is_dir = current.is_dir()
        except OSError:
            stats["errors"] += 1
            return

        if is_dir:
            try:
                with os.scandir(current) as iterator:
                    children = [Path(entry.path) for entry in iterator]
            except OSError as e:
                logger.warning("Cannot read %s: %s", current, e)
                stats["errors"] += 1
                return
            for child in children:
                remove(child)
            try:
                current.rmdir()
                stats["deleted"] += 1
            except OSError as e:
                logger.warning("Cannot remove %s: %s", current, e)
                stats["errors"] += 1
        else:
            try:
                size = current.stat(follow_symlinks=False).st_size
                current.unlink()
                stats["deleted"] += 1
                stats["bytes"] += size
            except OSError as e:
                logger.warning("Cannot remove %s: %s", current, e)
                stats["errors"] += 1

    remove(target)
    return stats