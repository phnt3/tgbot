"""Safe file browsing, download and upload helpers for PC Remote.

The file manager exposes the local filesystem to authorized Telegram users.
Every operation is defensive: directory listings skip unreadable entries,
downloads are capped to the Telegram Bot API limit, and uploads are written
into a dedicated folder with sanitized names and no accidental overwrite.
"""

import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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