"""Windows clipboard bridge for PC Remote.

Reads and writes Unicode text through the Win32 clipboard API using ctypes.
The clipboard is a shared, single-owner resource, so every operation:

- retries ``OpenClipboard`` briefly while another process holds it,
- always closes the clipboard again,
- caps text length in both directions.

The native helpers (``_open_clipboard``, ``_close_clipboard``, ``_get_text``,
``_set_text``) are module-level functions on purpose: tests monkeypatch them
and never touch the real clipboard.
"""

import ctypes
import logging
import os
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# Refuse absurd payloads: a clipboard bridge is not a file transfer tool, and
# a giant block would freeze the bot while Telegram renders it.
MAX_TEXT_CHARS = 200_000

OPEN_ATTEMPTS = 10
OPEN_RETRY_DELAY = 0.05


def clipboard_available() -> bool:
    """Return True when the native Windows clipboard can be used."""
    return os.name == "nt"


def _libs() -> Tuple[object, object]:
    """Return configured user32/kernel32 handles.

    Raises:
        RuntimeError: On non-Windows platforms.
    """
    if os.name != "nt":
        raise RuntimeError("The clipboard is only available on Windows.")
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = ctypes.c_bool
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p

    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    return user32, kernel32


def _open_clipboard() -> bool:
    """Try to open the clipboard, retrying while it is locked elsewhere."""
    user32, _ = _libs()
    for attempt in range(OPEN_ATTEMPTS):
        if user32.OpenClipboard(None):
            return True
        if attempt + 1 < OPEN_ATTEMPTS:
            time.sleep(OPEN_RETRY_DELAY)
    return False


def _close_clipboard() -> None:
    """Close the clipboard, ignoring failures during teardown."""
    try:
        _, _ = _libs()
        ctypes.windll.user32.CloseClipboard()
    except Exception:  # pragma: no cover - teardown must never raise
        logger.debug("Failed to close clipboard", exc_info=True)


def _get_text() -> Optional[str]:
    """Return clipboard text, or None when the clipboard has no text."""
    user32, kernel32 = _libs()
    handle = user32.GetClipboardData(CF_UNICODETEXT)
    if not handle:
        return None
    pointer = kernel32.GlobalLock(ctypes.c_void_p(handle))
    if not pointer:
        return None
    try:
        return ctypes.wstring_at(pointer)
    finally:
        kernel32.GlobalUnlock(ctypes.c_void_p(handle))


def _set_text(text: str) -> bool:
    """Replace clipboard contents with ``text``."""
    user32, kernel32 = _libs()
    if not user32.EmptyClipboard():
        return False

    size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        return False

    pointer = kernel32.GlobalLock(ctypes.c_void_p(handle))
    if not pointer:
        kernel32.GlobalFree(ctypes.c_void_p(handle))
        return False

    try:
        buffer = ctypes.create_unicode_buffer(text)
        ctypes.memmove(pointer, buffer, size)
    finally:
        kernel32.GlobalUnlock(ctypes.c_void_p(handle))

    if not user32.SetClipboardData(CF_UNICODETEXT, ctypes.c_void_p(handle)):
        kernel32.GlobalFree(ctypes.c_void_p(handle))
        return False
    # On success the system owns the handle; freeing it would corrupt memory.
    return True


def read_clipboard_text() -> Tuple[bool, str, bool]:
    """Read clipboard text.

    Returns:
        ``(ok, text, truncated)``. On failure ``text`` is a human-readable
        error message and ``truncated`` is False. An empty clipboard returns
        ``(True, "", False)``.
    """
    if not clipboard_available():
        return False, "The clipboard is only available on Windows.", False

    try:
        if not _open_clipboard():
            return False, "The clipboard is busy; try again in a moment.", False
        try:
            text = _get_text()
        finally:
            _close_clipboard()
    except Exception as e:
        logger.exception("Clipboard read failed")
        return False, f"Clipboard read failed: {e}", False

    content = text or ""
    truncated = len(content) > MAX_TEXT_CHARS
    return True, content[:MAX_TEXT_CHARS], truncated


def write_clipboard_text(text: str) -> Tuple[bool, str]:
    """Replace the clipboard with ``text``.

    Returns:
        Tuple of (success, message).
    """
    if not clipboard_available():
        return False, "The clipboard is only available on Windows."

    content = str(text if text is not None else "")
    if not content.strip():
        return False, "Refused: the clipboard text is empty."
    if len(content) > MAX_TEXT_CHARS:
        return False, (
            f"Refused: {len(content)} characters exceeds the "
            f"{MAX_TEXT_CHARS} character clipboard limit."
        )

    try:
        if not _open_clipboard():
            return False, "The clipboard is busy; try again in a moment."
        try:
            ok = _set_text(content)
        finally:
            _close_clipboard()
    except Exception as e:
        logger.exception("Clipboard write failed")
        return False, f"Clipboard write failed: {e}"

    if not ok:
        return False, "Failed to write to the clipboard."
    return True, f"Clipboard updated ({len(content)} characters)."


def clear_clipboard() -> Tuple[bool, str]:
    """Empty the clipboard."""
    if not clipboard_available():
        return False, "The clipboard is only available on Windows."

    try:
        if not _open_clipboard():
            return False, "The clipboard is busy; try again in a moment."
        try:
            ok = bool(ctypes.windll.user32.EmptyClipboard())
        finally:
            _close_clipboard()
    except Exception as e:
        logger.exception("Clipboard clear failed")
        return False, f"Clipboard clear failed: {e}"

    if not ok:
        return False, "Failed to clear the clipboard."
    return True, "Clipboard cleared."