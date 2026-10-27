"""Telegram screens for disk cleanup, files, media, network and scheduled tasks.

All messages are plain text (no parse_mode) and every dynamic value is passed
through ``md_safe()``. Destructive operations (temp cleanup, Recycle Bin) use
the one-shot confirmation pattern from ``handlers`` so a single accidental tap
can never delete anything.

Callback data prefixes owned by this module:
    dsk_  disk space and cleanup
    fm_   file browser
    med_  volume and media keys
    net_  network / VPN status
    sch_  task scheduler
"""

import asyncio
import io
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiogram import types

from . import _consume_confirmation, _request_confirmation, md_safe
from disk_cleanup import (
    clean_targets,
    get_disk_overview,
    get_recycle_bin_size,
    empty_recycle_bin,
    human_size,
    scan_cleanup_targets,
)
from file_manager import (
    MAX_SEND_BYTES,
    default_upload_dir,
    format_size,
    list_directory,
    save_upload,
)
from media_control import MEDIA_ACTIONS, press_key
from network_info import collect_network_status
from scheduler import TaskScheduler, describe_task

logger = logging.getLogger(__name__)

FILES_PER_PAGE = 8
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
CLEAN_CONFIRM_KEY = "disk_clean"
RECYCLE_CONFIRM_KEY = "recycle_bin"

_FILE_STATE: Dict[int, Dict[str, Any]] = {}
_scheduler: Optional[TaskScheduler] = None


def set_scheduler(scheduler: Optional[TaskScheduler]) -> None:
    """Register the running scheduler instance used by the UI."""
    global _scheduler
    _scheduler = scheduler


def get_scheduler() -> Optional[TaskScheduler]:
    """Return the scheduler instance, if the bot has started one."""
    return _scheduler


def _key(message: Any) -> int:
    """Return the chat id used for per-chat UI state."""
    chat = getattr(message, "chat", None)
    return int(getattr(chat, "id", 0) or 0)


def _button(text: str, data: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text=text, callback_data=data)


def _back_keyboard(extra: Optional[List[List[types.InlineKeyboardButton]]] = None
                   ) -> types.InlineKeyboardMarkup:
    rows = list(extra or [])
    rows.append([_button("<< Back", "menu_main")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


# --- Disk space and cleanup ------------------------------------------------


async def show_disk_menu(message: types.Message) -> None:
    """Show a disk usage overview with cleanup entry points."""
    try:
        disks = await asyncio.to_thread(get_disk_overview)
    except Exception as e:
        logger.exception("Disk overview failed")
        await message.answer(f"Failed to read disk usage: {e}")
        return

    lines = ["=== DISK SPACE ===", ""]
    if not disks:
        lines.append("No fixed disks detected.")
    for disk in disks:
        lines.append(
            f"{md_safe(disk['mountpoint'])} ({md_safe(disk['fstype'])})"
        )
        lines.append(
            f"  {human_size(disk['free_bytes'])} free of "
            f"{human_size(disk['total_bytes'])} "
            f"({disk['percent']:.1f}% used)"
        )
    lines += [
        "",
        "Analyze known temp/cache folders or empty the Recycle Bin.",
    ]

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("Analyze temp & cache", "dsk_analyze")],
            [_button("Clean temp & cache", "dsk_clean")],
            [_button("Recycle Bin", "dsk_bin")],
            [_button("<< Back", "menu_main")],
        ]
    )
    await message.answer(
        "\n".join(lines), reply_markup=keyboard, disable_web_page_preview=True
    )


async def analyze_disk(message: types.Message) -> None:
    """Measure temp/cache folders and show what can be freed."""
    await message.answer("Scanning temporary and cache folders...")
    try:
        targets = await asyncio.to_thread(scan_cleanup_targets)
    except Exception as e:
        logger.exception("Disk analysis failed")
        await message.answer(f"Scan failed: {e}")
        return

    lines = ["=== DISK ANALYSIS ===", ""]
    cleanable_total = 0
    partial = False
    for target in targets:
        if not target["exists"]:
            status = "not found"
        else:
            status = human_size(target["size_bytes"])
            if target["files"]:
                status += f" in {target['files']} file(s)"
            if target["partial"]:
                status += " (scan truncated)"
                partial = True
        tag = "" if target["cleanable"] else " [report only]"
        lines.append(f"{md_safe(target['name'])}: {status}{tag}")
        if target["cleanable"] and target["exists"]:
            cleanable_total += int(target["size_bytes"])

    lines += ["", f"Cleanable now: {human_size(cleanable_total)}"]
    if partial:
        lines.append("Some folders were too large to scan fully.")

    keyboard = _back_keyboard(
        [
            [_button("Clean temp & cache", "dsk_clean")],
            [_button("Analyze again", "dsk_analyze")],
        ]
    )
    await message.answer(
        "\n".join(lines), reply_markup=keyboard, disable_web_page_preview=True
    )


async def request_clean(message: types.Message) -> None:
    """Ask for confirmation before deleting temp/cache contents."""
    _request_confirmation(CLEAN_CONFIRM_KEY)
    lines = [
        "=== CLEAN TEMP & CACHE ===",
        "",
        "This permanently deletes the CONTENTS of known temporary and cache",
        "folders. The folders themselves are kept. Windows Update cache is",
        "report-only and will not be touched.",
        "",
        "Press Confirm within 60 seconds to continue.",
    ]
    keyboard = _back_keyboard(
        [
            [_button("Confirm cleanup", "dsk_clean_yes")],
            [_button("Cancel", "dsk_clean_no")],
        ]
    )
    await message.answer("\n".join(lines), reply_markup=keyboard)


async def confirm_clean(message: types.Message) -> None:
    """Run the confirmed cleanup and report what was freed."""
    if not _consume_confirmation(CLEAN_CONFIRM_KEY):
        await message.answer(
            "Cleanup confirmation expired. Press Clean temp & cache again."
        )
        return

    await message.answer("Cleaning temporary folders...")
    try:
        result = await asyncio.to_thread(clean_targets)
    except Exception as e:
        logger.exception("Cleanup failed")
        await message.answer(f"Cleanup failed: {e}")
        return

    freed = int(result.get("freed_bytes", 0))
    lines = [
        "=== CLEANUP RESULT ===",
        "",
        f"Freed: {human_size(freed)}",
        f"Removed files: {int(result.get('removed_files', 0))}",
        f"Errors: {int(result.get('errors', 0))}",
    ]
    cleaned = result.get("cleaned") or []
    if cleaned:
        lines += ["", "Cleaned: " + ", ".join(md_safe(str(n)) for n in cleaned)]
    skipped = result.get("skipped") or []
    if skipped:
        lines += ["", "Skipped: " + ", ".join(md_safe(str(n)) for n in skipped)]

    keyboard = _back_keyboard(
        [[_button("Analyze again", "dsk_analyze")]]
    )
    await message.answer("\n".join(lines), reply_markup=keyboard)


# --- Recycle Bin -----------------------------------------------------------


async def show_recycle_bin(message: types.Message) -> None:
    """Show the Recycle Bin size with an explicit empty action."""
    try:
        size = await asyncio.to_thread(get_recycle_bin_size)
    except Exception as e:
        logger.exception("Recycle Bin size failed")
        await message.answer(f"Failed to read Recycle Bin: {e}")
        return

    lines = [
        "=== RECYCLE BIN ===",
        "",
        f"Current size: {human_size(size)}",
        "",
        "Emptying permanently deletes every item in the Recycle Bin",
        "on all drives.",
    ]
    keyboard = _back_keyboard(
        [
            [_button("Empty Recycle Bin", "dsk_bin_empty")],
            [_button("Refresh", "dsk_bin")],
        ]
    )
    await message.answer("\n".join(lines), reply_markup=keyboard)


async def request_empty_bin(message: types.Message) -> None:
    """Ask for confirmation before emptying the Recycle Bin."""
    _request_confirmation(RECYCLE_CONFIRM_KEY)
    lines = [
        "=== EMPTY RECYCLE BIN ===",
        "",
        "This permanently deletes all Recycle Bin items on all drives.",
        "It cannot be undone.",
        "",
        "Press Confirm within 60 seconds to continue.",
    ]
    keyboard = _back_keyboard(
        [
            [_button("Confirm empty", "dsk_bin_yes")],
            [_button("Cancel", "dsk_bin_no")],
        ]
    )
    await message.answer("\n".join(lines), reply_markup=keyboard)


async def confirm_empty_bin(message: types.Message) -> None:
    """Empty the Recycle Bin after a valid confirmation."""
    if not _consume_confirmation(RECYCLE_CONFIRM_KEY):
        await message.answer(
            "Confirmation expired. Press Empty Recycle Bin again."
        )
        return

    await message.answer("Emptying the Recycle Bin...")
    try:
        ok, text = await asyncio.to_thread(empty_recycle_bin)
    except Exception as e:
        logger.exception("Recycle Bin empty failed")
        await message.answer(f"Failed to empty Recycle Bin: {e}")
        return

    level = "DONE" if ok else "FAILED"
    await message.answer(
        f"[{level}]\n{md_safe(text)}",
        reply_markup=_back_keyboard([[_button("Recycle Bin", "dsk_bin")]]),
    )


# --- File browser ----------------------------------------------------------


async def show_files(message: types.Message, path: Optional[Path] = None) -> None:
    """Open the file browser at ``path`` (home folder by default)."""
    await _load_directory(message, Path(path) if path else Path.home())


async def _load_directory(
    message: types.Message, path: Path, page: int = 0
) -> None:
    """List a directory, cache it per chat and render the current page."""
    try:
        listing = await asyncio.to_thread(list_directory, path)
    except NotADirectoryError:
        await message.answer(f"Cannot open folder: {md_safe(str(path))}")
        return
    except Exception as e:
        logger.exception("Directory listing failed")
        await message.answer(f"Failed to list folder: {e}")
        return

    _FILE_STATE[_key(message)] = {
        "path": listing["path"],
        "parent": listing["parent"],
        "entries": listing["entries"],
        "page": max(0, int(page)),
        "error": None,
    }
    await _render_files(message)


async def _render_files(message: types.Message) -> None:
    """Render the cached directory listing as one message with buttons."""
    state = _FILE_STATE.get(_key(message))
    if state is None:
        await message.answer("File browser expired. Open it again from /files.")
        return

    entries: List[Dict[str, Any]] = state["entries"]
    page_count = max(1, (len(entries) + FILES_PER_PAGE - 1) // FILES_PER_PAGE)
    page = min(max(0, int(state["page"])), page_count - 1)
    state["page"] = page
    start = page * FILES_PER_PAGE
    visible = entries[start:start + FILES_PER_PAGE]

    lines = [f"=== FILES: {state['path']} ===", ""]
    if not entries:
        lines.append("This folder is empty.")
    else:
        lines.append(
            f"Entries: {len(entries)} | Page {page + 1}/{page_count}"
        )
        if not visible:
            lines.append("No entries on this page.")

    rows: List[List[types.InlineKeyboardButton]] = []
    for offset, entry in enumerate(visible):
        index = start + offset
        name = str(entry["name"])
        if entry["is_dir"]:
            label = f"[DIR] {name}"
            data = f"fm_open_{index}"
        else:
            label = f"[FILE] {name} ({format_size(entry['size'])})"
            data = f"fm_get_{index}"
        if len(label) > 55:
            label = label[:52] + "..."
        rows.append([_button(label, data)])

    if page > 0:
        rows.append([_button("< Prev", f"fm_page_{page - 1}")])
    if page < page_count - 1:
        rows.append([_button("Next >", f"fm_page_{page + 1}")])

    nav = [
        _button("Home", "fm_home"),
        _button("Up", "fm_up"),
        _button("Refresh", "fm_refresh"),
    ]
    rows.append(nav)
    rows.append([_button("<< Back", "menu_main")])

    await message.answer(
        "\n".join(lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
        disable_web_page_preview=True,
    )


async def _send_file(message: types.Message, entry: Dict[str, Any]) -> None:
    """Send one file to Telegram, enforcing the Bot API size cap."""
    path = Path(entry["path"])
    size = int(entry["size"])
    if size > MAX_SEND_BYTES:
        await message.answer(
            f"Cannot send {md_safe(path.name)}: {format_size(size)} exceeds "
            f"the {format_size(MAX_SEND_BYTES)} limit."
        )
        return
    try:
        await message.answer_document(
            types.FSInputFile(str(path)),
            caption=md_safe(path.name),
        )
    except Exception as e:
        logger.exception("Failed to send file")
        await message.answer(f"Failed to send file: {e}")


def _parse_index(data: str, prefix: str) -> Optional[int]:
    """Parse the integer index from ``prefix<number>`` callback data."""
    raw = data[len(prefix):]
    if not raw.isdigit():
        return None
    return int(raw)


async def _handle_file_callback(callback: types.CallbackQuery, data: str) -> None:
    """Route fm_* callbacks using the per-chat cached listing."""
    message = callback.message
    if message is None:
        await callback.answer("Message is no longer available", show_alert=True)
        return

    if data == "fm_home":
        await show_files(message)
        return
    if data == "fm_noop":
        await callback.answer()
        return

    state = _FILE_STATE.get(_key(message))
    if state is None:
        await callback.answer("File browser expired", show_alert=True)
        await show_files(message)
        return

    if data == "fm_refresh":
        await _load_directory(message, Path(state["path"]), int(state["page"]))
        return

    if data == "fm_up":
        parent = state.get("parent")
        if parent is None:
            await callback.answer("Already at the top", show_alert=True)
            return
        await _load_directory(message, Path(parent))
        return

    if data.startswith("fm_page_"):
        page = _parse_index(data, "fm_page_")
        if page is None:
            await callback.answer("Invalid page", show_alert=True)
            return
        state["page"] = page
        await _render_files(message)
        return

    if data.startswith("fm_open_") or data.startswith("fm_get_"):
        want_file = data.startswith("fm_get_")
        index = _parse_index(data, "fm_get_" if want_file else "fm_open_")
        entries: List[Dict[str, Any]] = state["entries"]
        if index is None or not 0 <= index < len(entries):
            await callback.answer("Entry is gone; refreshing", show_alert=True)
            await _load_directory(message, Path(state["path"]), int(state["page"]))
            return

        entry = entries[index]
        if want_file:
            if entry["is_dir"]:
                await callback.answer("That is a folder", show_alert=True)
                return
            await _send_file(message, entry)
            return

        if not entry["is_dir"]:
            await callback.answer("That is a file", show_alert=True)
            return
        await _load_directory(message, Path(entry["path"]))
        return

    await callback.answer("Unknown file action", show_alert=True)


# --- Telegram uploads ------------------------------------------------------


async def handle_upload(message: types.Message) -> None:
    """Store a document or photo sent to the bot in the uploads folder."""
    from security import is_user_authorized

    user = getattr(message, "from_user", None)
    if user is None or not is_user_authorized(user.id):
        return

    document = getattr(message, "document", None)
    photos = getattr(message, "photo", None) or []
    if document is not None:
        file_id = document.file_id
        filename = document.file_name or "document.bin"
        size = int(document.file_size or 0)
    elif photos:
        largest = photos[-1]
        file_id = largest.file_id
        filename = f"photo_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
        size = int(largest.file_size or 0)
    else:
        return

    if size > MAX_UPLOAD_BYTES:
        await message.answer(
            f"File is {format_size(size)}; the upload limit is "
            f"{format_size(MAX_UPLOAD_BYTES)}."
        )
        return

    await message.answer("Receiving file...")
    buffer = io.BytesIO()
    try:
        await message.bot.download(file_id, destination=buffer)
    except Exception as e:
        logger.exception("Failed to download Telegram file")
        await message.answer(f"Failed to receive file: {e}")
        return

    payload = buffer.getvalue()
    if len(payload) > MAX_UPLOAD_BYTES:
        await message.answer("File is too large after download.")
        return

    try:
        saved = await asyncio.to_thread(
            save_upload, filename, payload, default_upload_dir()
        )
    except Exception as e:
        logger.exception("Failed to save upload")
        await message.answer(f"Failed to save file: {e}")
        return

    await message.answer(
        f"Saved {format_size(len(payload))} to:\n{md_safe(str(saved))}"
    )


# --- Volume and media ------------------------------------------------------


async def show_media_menu(message: types.Message) -> None:
    """Show volume and media transport controls."""
    lines = [
        "=== VOLUME & MEDIA ===",
        "",
        "Buttons act on the currently active media session.",
        "Volume steps are the standard Windows 2% increments.",
    ]
    rows = [
        [
            _button("Vol -", "med_vol_down"),
            _button("Mute", "med_mute"),
            _button("Vol +", "med_vol_up"),
        ],
        [
            _button("Prev", "med_prev"),
            _button("Play/Pause", "med_play_pause"),
            _button("Next", "med_next"),
        ],
        [_button("Stop", "med_stop")],
        [_button("<< Back", "menu_main")],
    ]
    await message.answer(
        "\n".join(lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _handle_media_callback(
    callback: types.CallbackQuery, data: str
) -> None:
    """Execute one whitelisted media key."""
    action = data[len("med_"):]
    if action not in MEDIA_ACTIONS:
        await callback.answer("Unknown media action", show_alert=True)
        return
    try:
        ok, text = await asyncio.to_thread(press_key, action)
    except Exception as e:
        logger.exception("Media action failed")
        ok, text = False, f"Media action failed: {e}"
    await callback.answer(text, show_alert=not ok)


# --- Network and VPN -------------------------------------------------------


async def show_network(message: types.Message) -> None:
    """Render active interfaces, VPN state, Wi-Fi details and public IP."""
    try:
        status = await asyncio.to_thread(collect_network_status)
    except Exception as e:
        logger.exception("Network status failed")
        await message.answer(f"Failed to read network status: {e}")
        return

    lines = ["=== NETWORK / VPN ===", ""]
    lines.append(f"Host: {md_safe(str(status.get('hostname', 'unknown')))}")

    vpn = status.get("vpn_active") or []
    if vpn:
        names = ", ".join(
            f"{md_safe(str(item['name']))} ({md_safe(', '.join(item['ipv4']))})"
            for item in vpn
        )
        lines.append(f"VPN: ACTIVE - {names}")
    else:
        lines.append("VPN: not detected")

    active = status.get("active") or []
    lines.append("")
    if active:
        lines.append("Active interfaces:")
        for item in active:
            lines.append(
                f"  {md_safe(str(item['name']))}: "
                f"{md_safe(', '.join(item['ipv4']))}"
                + (f" ({item['speed_mbps']} Mbps)" if item["speed_mbps"] else "")
            )
    else:
        lines.append("No active network interfaces.")

    wifi = status.get("wifi")
    if wifi:
        lines += [
            "",
            f"Wi-Fi: {md_safe(str(wifi['ssid']))} "
            f"({md_safe(str(wifi['signal']))} signal)",
            f"Auth: {md_safe(str(wifi['authentication']))}",
        ]

    public_ip = status.get("public_ip")
    lines += ["", f"Public IP: {md_safe(public_ip) if public_ip else 'unavailable'}"]

    keyboard = _back_keyboard([[_button("Refresh", "net_refresh")]])
    await message.answer(
        "\n".join(lines), reply_markup=keyboard, disable_web_page_preview=True
    )


async def _handle_network_callback(
    callback: types.CallbackQuery, data: str
) -> None:
    """Refresh the network status screen."""
    if data != "net_refresh":
        await callback.answer("Unknown network action", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Message is no longer available", show_alert=True)
        return
    await show_network(callback.message)


# --- Task scheduler --------------------------------------------------------

_QUICK_TASKS: Dict[str, Dict[str, Any]] = {
    "shut_15": {"action": "shutdown", "delay": 900, "label": "Shutdown"},
    "shut_30": {"action": "shutdown", "delay": 1800, "label": "Shutdown"},
    "shut_60": {"action": "shutdown", "delay": 3600, "label": "Shutdown"},
    "restart_30": {"action": "restart", "delay": 1800, "label": "Restart"},
    "sleep_30": {"action": "sleep", "delay": 1800, "label": "Sleep"},
    "lock_15": {"action": "lock", "delay": 900, "label": "Lock workstation"},
    "prof_GAMING_30": {
        "action": "profile",
        "delay": 1800,
        "label": "Launch GAMING",
        "params": {"profile": "GAMING"},
    },
    "prof_WORK_30": {
        "action": "profile",
        "delay": 1800,
        "label": "Launch WORK",
        "params": {"profile": "WORK"},
    },
}


def _format_remaining(run_at: float) -> str:
    """Return a short 'in 12m' style countdown for a timestamp."""
    remaining = int(float(run_at) - time.time())
    if remaining <= 0:
        return "due now"
    if remaining >= 3600:
        return f"in {remaining // 3600}h {(remaining % 3600) // 60}m"
    if remaining >= 60:
        return f"in {remaining // 60}m"
    return f"in {remaining}s"


async def show_scheduler(message: types.Message) -> None:
    """Show pending scheduled tasks and quick-schedule buttons."""
    scheduler = _scheduler
    lines = ["=== SCHEDULED TASKS ===", ""]
    rows: List[List[types.InlineKeyboardButton]] = []

    if scheduler is None:
        lines.append("Scheduler is not running.")
    else:
        tasks = scheduler.list_tasks()
        if not tasks:
            lines.append("No pending tasks.")
        else:
            lines.append(f"Pending: {len(tasks)}")
            for task in tasks[:10]:
                lines.append(
                    f"#{task['id']} {describe_task(task)} "
                    f"({_format_remaining(task['run_at'])})"
                )
                rows.append(
                    [
                        _button(
                            f"Cancel #{task['id']}",
                            f"sch_cancel_{task['id']}",
                        )
                    ]
                )
            if len(tasks) > 10:
                lines.append(f"...and {len(tasks) - 10} more")

        lines += ["", "Quick schedules:"]
        rows += [
            [
                _button("Shutdown +15m", "sch_add_shut_15"),
                _button("+30m", "sch_add_shut_30"),
                _button("+1h", "sch_add_shut_60"),
            ],
            [
                _button("Restart +30m", "sch_add_restart_30"),
                _button("Sleep +30m", "sch_add_sleep_30"),
            ],
            [_button("Lock +15m", "sch_add_lock_15")],
            [
                _button("GAMING +30m", "sch_add_prof_GAMING_30"),
                _button("WORK +30m", "sch_add_prof_WORK_30"),
            ],
        ]
        if tasks:
            rows.append([_button("Cancel all", "sch_cancel_all")])
        rows.append([_button("Refresh", "sch_refresh")])

    rows.append([_button("<< Back", "menu_main")])
    await message.answer(
        "\n".join(lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _handle_scheduler_callback(
    callback: types.CallbackQuery, data: str
) -> None:
    """Create, cancel or refresh scheduled tasks."""
    message = callback.message
    if message is None:
        await callback.answer("Message is no longer available", show_alert=True)
        return

    scheduler = _scheduler
    if scheduler is None:
        await callback.answer("Scheduler is not running", show_alert=True)
        return

    if data.startswith("sch_add_"):
        code = data[len("sch_add_"):]
        spec = _QUICK_TASKS.get(code)
        if spec is None:
            await callback.answer("Unknown quick schedule", show_alert=True)
            return
        try:
            task = scheduler.add_delay(
                spec["action"],
                spec["delay"],
                label=spec["label"],
                params=spec.get("params"),
            )
        except ValueError as e:
            await callback.answer(f"Cannot schedule: {e}", show_alert=True)
            return
        await callback.answer(f"Scheduled task #{task['id']}")
        await show_scheduler(message)
        return

    if data == "sch_refresh":
        await callback.answer()
        await show_scheduler(message)
        return

    if data == "sch_cancel_all":
        removed = scheduler.cancel_all()
        await callback.answer(f"Cancelled {removed} task(s)")
        await show_scheduler(message)
        return

    if data.startswith("sch_cancel_"):
        task_id = _parse_index(data, "sch_cancel_")
        if task_id is None:
            await callback.answer("Invalid task id", show_alert=True)
            return
        removed = scheduler.cancel(task_id)
        await callback.answer(
            "Task cancelled" if removed else "Task not found",
            show_alert=not removed,
        )
        await show_scheduler(message)
        return

    await callback.answer("Unknown scheduler action", show_alert=True)


# --- Callback dispatcher ---------------------------------------------------


async def _handle_disk_callback(
    callback: types.CallbackQuery, data: str
) -> None:
    """Route dsk_* callbacks for disk overview and cleanup."""
    message = callback.message
    if message is None:
        await callback.answer("Message is no longer available", show_alert=True)
        return

    if data in {"dsk_refresh", "dsk_menu"}:
        await show_disk_menu(message)
        return
    if data == "dsk_analyze":
        await analyze_disk(message)
        return
    if data == "dsk_clean":
        await request_clean(message)
        return
    if data == "dsk_clean_yes":
        await confirm_clean(message)
        return
    if data == "dsk_clean_no":
        _consume_confirmation(CLEAN_CONFIRM_KEY)
        await callback.answer("Cleanup cancelled")
        await show_disk_menu(message)
        return
    if data == "dsk_bin":
        await show_recycle_bin(message)
        return
    if data == "dsk_bin_empty":
        await request_empty_bin(message)
        return
    if data == "dsk_bin_yes":
        await confirm_empty_bin(message)
        return
    if data == "dsk_bin_no":
        _consume_confirmation(RECYCLE_CONFIRM_KEY)
        await callback.answer("Cancelled")
        await show_recycle_bin(message)
        return

    await callback.answer("Unknown disk action", show_alert=True)


async def handle_callback(
    callback: types.CallbackQuery, data: str
) -> None:
    """Entry point for every feature callback (dsk_, fm_, med_, net_, sch_)."""
    if data.startswith("dsk_"):
        await _handle_disk_callback(callback, data)
    elif data.startswith("fm_"):
        await _handle_file_callback(callback, data)
    elif data.startswith("med_"):
        await _handle_media_callback(callback, data)
    elif data.startswith("net_"):
        await _handle_network_callback(callback, data)
    elif data.startswith("sch_"):
        await _handle_scheduler_callback(callback, data)
    else:
        await callback.answer("Unknown action", show_alert=True)