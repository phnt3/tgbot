"""Telegram handlers package for PC Remote.

Contains all Telegram command and callback query handlers organized by category.

All messages are sent as plain text (no parse_mode) to eliminate the entire
class of Telegram Markdown entity-parse errors ("Can't find end of the entity").
Dynamic data (hostnames, paths, process names, alert messages) is still
sanitized through md_safe().
"""

from aiogram import types
from aiogram import Dispatcher
from aiogram.filters import Command
import asyncio
import os
import time

from . import init
from . import common


def md_safe(text) -> str:
    """Sanitize dynamic text so it is safe inside Telegram messages."""
    return str(text).replace("*", "").replace("_", "").replace("`", "").replace("[", "(").replace("]", ")")


# --- Confirmation state (Phase 4) ---
# Destructive actions (restart, shutdown, process kill) require an explicit
# second press within CONFIRM_WINDOW_SECONDS. Keys expire automatically so a
# forgotten confirmation can never be replayed later.
CONFIRM_WINDOW_SECONDS = 60
_PENDING_CONFIRMATIONS: dict = {}


def _request_confirmation(key) -> None:
    """Arm a one-shot confirmation for the given action key."""
    _PENDING_CONFIRMATIONS[key] = time.time() + CONFIRM_WINDOW_SECONDS


def _consume_confirmation(key) -> bool:
    """Consume a confirmation; returns True only if it is still valid."""
    expiry = _PENDING_CONFIRMATIONS.pop(key, None)
    return expiry is not None and time.time() <= expiry


def _clear_confirmations(*keys) -> None:
    """Drop pending confirmations for the given keys."""
    for key in keys:
        _PENDING_CONFIRMATIONS.pop(key, None)


def register_init_callbacks(dispatcher: Dispatcher) -> None:
    """Register inline keyboard callback handlers.

    Args:
        dispatcher: The Aiogram Dispatcher instance.
    """

    @dispatcher.callback_query()
    async def callback_query_handler(callback: types.CallbackQuery) -> None:
        """Handle callback queries from inline keyboard.

        Routes to appropriate handler based on callback data prefix.
        Validates callback data and handles expired/malformed callbacks safely.
        """
        # Authorization check: inline buttons must be protected as strictly as
        # commands. A leaked/forwarded message with buttons must not let a
        # foreign Telegram account launch apps or power off the PC.
        from security import is_user_authorized

        user_id = callback.from_user.id if callback.from_user else None
        if user_id is None or not is_user_authorized(user_id):
            await callback.answer("Not authorized", show_alert=True)
            return

        data = callback.data

        if not data:
            await callback.answer("Invalid callback data", show_alert=True)
            return

        # A button press means the user moved on: cancel armed one-shot text
        # prompts so the next message is not swallowed by a stale prompt.
        from handlers import clipboard_ui, features

        chat = getattr(callback.message, "chat", None)
        chat_id = int(getattr(chat, "id", 0) or 0)
        if chat_id:
            if data not in ("clip_write", "clip_open", "clip_cancel"):
                clipboard_ui.cancel_pending(chat_id)
            if data not in ("fm_search", "fm_search_cancel"):
                features.cancel_search_pending(chat_id)
            if data not in ("fm_del_yes", "fm_del_no"):
                features.cancel_file_pending(chat_id)

        # Per-application launch buttons use index-based callback data
        # (app_<idx>), keeping 64-byte limits and injection concerns away
        if data.startswith("app_") and data[4:].isdigit():
            await launch_app_index(callback.message, int(data[4:]))
        elif data.startswith("killp_") and data[6:].isdigit():
            await request_process_kill(callback, int(data[6:]))
        elif data.startswith("confirm_killp_") and data[14:].isdigit():
            await confirm_process_kill(callback, int(data[14:]))
        elif data.startswith("action_"):
            await handle_power_action(callback, data)
        elif data.startswith(("confirm_", "deny_")):
            await handle_power_confirmation(callback, data)
        elif data.startswith("chart_"):
            await show_chart(callback.message, data[len("chart_"):])
        elif data.startswith(("dsk_", "fm_", "med_", "net_", "sch_")):
            # Feature screens live in handlers/features.py
            from handlers import features

            await features.handle_callback(callback, data)
        elif data.startswith("clip_"):
            # Clipboard bridge lives in handlers/clipboard_ui.py
            await clipboard_ui.handle_callback(callback, data)
        elif data.startswith("set_"):
            # In-bot settings screens live in handlers/settings_ui.py
            from handlers import settings_ui

            await settings_ui.handle_callback(callback, data)
        elif data.startswith(("menu_", "profile_")):
            # Route to menu and profile handlers
            from handlers import features
            from handlers import settings_ui

            routing = {
                "menu_main": lambda msg: show_main_menu_handler(msg),
                "menu_system": lambda msg: show_system_monitoring(msg),
                "menu_apps": lambda msg: show_app_launcher(msg),
                "menu_gaming": lambda msg: show_gaming_profiles(msg),
                "menu_power": lambda msg: show_power_management(msg),
                "menu_screenshot": lambda msg: take_screenshot(msg),
                "menu_processes": lambda msg: show_process_info(msg),
                "menu_alerts": lambda msg: show_alerts_history(msg),
                "menu_settings": lambda msg: settings_ui.show_settings(msg),
                "menu_disk": lambda msg: features.show_disk_menu(msg),
                "menu_files": lambda msg: features.show_files(msg),
                "menu_media": lambda msg: features.show_media_menu(msg),
                "menu_network": lambda msg: features.show_network(msg),
                "menu_scheduler": lambda msg: features.show_scheduler(msg),
                "menu_clipboard": lambda msg: clipboard_ui.show_clipboard(msg),
                "profile_gaming": lambda msg: launch_profile_callback(msg, "GAMING"),
                "profile_vr": lambda msg: launch_profile_callback(msg, "VR"),
                "profile_work": lambda msg: launch_profile_callback(msg, "WORK"),
                "profile_normal": lambda msg: launch_profile_callback(msg, "NORMAL"),
            }

            handler = routing.get(data)
            if handler:
                await handler(callback.message)
            else:
                await callback.answer("Unknown menu option", show_alert=True)
        else:
            await callback.answer("Invalid callback data", show_alert=True)

        # Always answer the callback to stop the loading spinner
        try:
            await callback.answer()
        except Exception:
            pass


def register_common_callbacks(dispatcher: Dispatcher) -> None:
    """Register common callback handlers.

    Args:
        dispatcher: The Aiogram Dispatcher instance.
    """
    pass


# --- Handler functions ---


async def show_main_menu_handler(message: types.Message) -> None:
    """Show the main menu."""
    from handlers.common import show_main_menu
    await show_main_menu(message)


async def show_system_monitoring(message: types.Message) -> None:
    """Show detailed system monitoring screen (plain text, no Markdown)."""
    from monitor import get_all_metrics

    metrics = await asyncio.to_thread(get_all_metrics)

    lines = ["=== SYSTEM MONITORING ===", ""]

    # System info
    lines.append("-- System Information --")
    hostname = metrics.get("hostname", "N/A")
    uptime = metrics.get("uptime")
    internet = metrics.get("internet")

    if uptime is not None:
        days = int(uptime // 86400)
        hours = int((uptime % 86400) // 3600)
        minutes = int((uptime % 3600) // 60)
        if days > 0:
            lines.append(f"Uptime: {days}d {hours}h {minutes}m")
        else:
            lines.append(f"Uptime: {hours}h {minutes}m")
    else:
        lines.append("Uptime: N/A")

    lines.append(f"Internet: {'Connected' if internet else 'Disconnected'}")
    lines.append(f"Hostname: {md_safe(hostname)}")
    lines.append("")

    # CPU
    lines.append("-- CPU --")
    cpu = metrics.get("cpu", {})
    cpu_util = cpu.get("utilization", "N/A")
    if cpu_util != "N/A":
        lines.append(f"Utilization: {cpu_util}%")
    cpu_temp = cpu.get("temperature")
    if cpu_temp and cpu_temp != "N/A":
        lines.append(f"Temperature: {cpu_temp} C")
    freq = cpu.get("frequency")
    if freq and freq != "N/A":
        lines.append(f"Frequency: {int(freq)} MHz")
    lines.append(f"Cores: {cpu.get('physical_cores', 'N/A')} physical, {cpu.get('logical_cores', 'N/A')} logical")
    lines.append("")

    # RAM
    lines.append("-- RAM --")
    ram = metrics.get("ram", {})
    ram_util = ram.get("utilization_percent", "N/A")
    if ram_util != "N/A":
        total = (ram.get("total_mb", 0) or 0) / 1024
        used = (ram.get("used_mb", 0) or 0) / 1024
        lines.append(f"Used: {used:.1f}/{total:.1f} GB ({ram_util:.1f}%)")
    else:
        lines.append("RAM: N/A")
    lines.append("")

    # GPU
    lines.append("-- GPU --")
    gpu = metrics.get("gpu", {})
    gpu_parts = []
    if gpu.get("utilization") != "N/A" and gpu.get("utilization") is not None:
        gpu_parts.append(f"Util: {gpu['utilization']}%")
    if gpu.get("temperature") != "N/A" and gpu.get("temperature") is not None:
        gpu_parts.append(f"Temp: {gpu['temperature']} C")
    if gpu.get("vram_used_mb") != "N/A" and gpu.get("vram_total_mb") != "N/A" and gpu.get("vram_used_mb") is not None:
        gpu_parts.append(f"VRAM: {gpu['vram_used_mb']:.1f}/{gpu['vram_total_mb']:.1f} GB")
    if gpu.get("power") != "N/A" and gpu.get("power") is not None:
        gpu_parts.append(f"Power: {gpu['power']}W")
    lines.append(", ".join(gpu_parts) if gpu_parts else "N/A")
    lines.append("")

    # Disk
    lines.append("-- Disk --")
    disks = metrics.get("disk", {})
    if disks:
        for drive, info in disks.items():
            if isinstance(info, dict) and info.get("utilization_percent", "N/A") != "N/A":
                lines.append(f"{md_safe(drive)}: {info.get('utilization_percent')}%")
    else:
        lines.append("No disk data available")

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="<< Back", callback_data="menu_main")],
    ])

    # Plain text: no parse_mode at all so Telegram never parses entities.
    await message.answer("\n".join(lines), reply_markup=keyboard, disable_web_page_preview=True)


async def show_app_launcher(message: types.Message) -> None:
    """Show application launcher screen with per-app launch buttons."""
    from application_manager import load_applications_config

    apps = load_applications_config()

    lines = ["=== APPLICATION LAUNCHER ===", ""]
    lines.append("Configured applications:")

    buttons = []
    if apps:
        row = []
        for idx, (name, path) in enumerate(apps.items()):
            status = "+" if os.path.exists(path) else "!"
            lines.append(f"[{status}] {md_safe(name)}")
            row.append(types.InlineKeyboardButton(
                text=f"{status} {name}", callback_data=f"app_{idx}"
            ))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        lines.append("")
        lines.append("+ = path exists, ! = path not found on disk")
    else:
        lines.append("No applications configured. Edit config/applications.json")

    buttons.append([types.InlineKeyboardButton(text="<< Back", callback_data="menu_main")])

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer("\n".join(lines), reply_markup=keyboard, disable_web_page_preview=True)


def _launch_app_from_allowlist(name: str):
    """Sync helper: launch one allowlisted app with DB logging (runs in a thread)."""
    from application_manager import launch_application
    from database import Database
    from pathlib import Path
    from config import config

    db = Database(Path(config["DATA_DIR"]) / "pc_remote.db")
    try:
        return launch_application(name, db=db)
    finally:
        db.close()


async def launch_app_index(message: types.Message, index: int) -> None:
    """Launch an application by its index in the loaded allowlist.

    Index-based callbacks are validated against the current config size, so
    stale or crafted indices are rejected instead of launching something else.
    """
    from application_manager import load_applications_config

    apps = load_applications_config()
    names = list(apps.keys())

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Launcher", callback_data="menu_apps")],
        [types.InlineKeyboardButton(text="<< Back", callback_data="menu_main")],
    ])

    if index < 0 or index >= len(names):
        await message.answer("Application index out of range. Open the launcher menu again.", reply_markup=keyboard)
        return

    name = names[index]

    # Launch in a worker thread: Popen/startfile plus the duplicate-detection
    # scan would otherwise block the event loop for ~0.5s
    ok, result_msg = await asyncio.to_thread(_launch_app_from_allowlist, name)

    mark = "Started" if ok else "Failed"
    await message.answer(f"{mark}: {md_safe(result_msg)}", reply_markup=keyboard)


async def launch_profile_callback(message: types.Message, profile_key: str) -> None:
    """Launch a gaming profile and report per-app results (plain text)."""
    from gaming_profiles import launch_profile

    # NORMAL is read-only: show current system status, change nothing
    if profile_key.strip().upper() == "NORMAL":
        await show_system_monitoring(message)
        return

    # Profile launch runs in a worker thread (subprocess + duplicate checks)
    started, failed = await asyncio.to_thread(launch_profile, profile_key)

    lines = [f"=== PROFILE {md_safe(profile_key.upper())} ===", ""]

    if started:
        lines.append("Started / already running:")
        for entry in started:
            tag = " (optional)" if entry.get("optional") else ""
            lines.append(f"  + {md_safe(entry['name'])}{tag}")

    if failed:
        lines.append("")
        lines.append("Failed:")
        for entry in failed:
            tag = " (optional)" if entry.get("optional") else ""
            lines.append(f"  - {md_safe(entry['name'])}{tag}: {md_safe(entry['message'])}")

    if not started and not failed:
        lines.append("No applications configured for this profile.")
        lines.append("Edit config/profiles.json and config/applications.json")

    lines.append("")
    lines.append("System settings were not modified.")

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Profiles", callback_data="menu_gaming")],
        [types.InlineKeyboardButton(text="<< Back", callback_data="menu_main")],
    ])

    await message.answer("\n".join(lines), reply_markup=keyboard, disable_web_page_preview=True)


async def show_gaming_profiles(message: types.Message) -> None:
    """Show gaming profiles screen with real app lists from profiles.json."""
    from gaming_profiles import load_profiles_config

    profiles = load_profiles_config()

    lines = ["=== GAMING MODE ===", ""]
    lines.append("Available profiles:")
    lines.append("")

    if profiles:
        for name in ("GAMING", "VR", "WORK", "NORMAL"):
            spec = next((s for n, s in profiles.items() if n.upper() == name), None)
            if spec is None:
                continue
            apps_list = spec["apps"] + spec["optional"]
            lines.append(f"{name}: {md_safe(', '.join(apps_list)) if apps_list else 'no apps (status only)'}")
    else:
        lines.append("GAMING / VR / WORK / NORMAL")
        lines.append("(profiles.json missing - defaults in use)")

    lines.append("")
    lines.append("Select a profile:")
    lines.append("NORMAL does not launch anything - it shows system status.")

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="GAMING", callback_data="profile_gaming")],
        [types.InlineKeyboardButton(text="VR", callback_data="profile_vr")],
        [types.InlineKeyboardButton(text="WORK", callback_data="profile_work")],
        [types.InlineKeyboardButton(text="NORMAL", callback_data="profile_normal")],
        [types.InlineKeyboardButton(text="<< Back", callback_data="menu_main")],
    ])

    await message.answer("\n".join(lines), reply_markup=keyboard, disable_web_page_preview=True)


async def show_power_management(message: types.Message) -> None:
    """Show power management screen."""
    from power_manager import get_power_status

    status = get_power_status()

    lines = ["=== POWER MANAGEMENT ===", ""]
    lines.append("Lock workstation: available")
    lines.append("Sleep: available")
    lines.append(f"Hibernate: {'available' if status.get('can_hibernate') else 'unavailable'}")
    lines.append(f"Display off: {'available' if status.get('can_display_off') else 'unavailable'}")
    lines.append(f"Shutdown: {'pending' if status.get('has_pending_shutdown') else 'available'}")
    lines.append("Restart: available")
    lines.append("Cancel shutdown: available")
    lines.append("")
    lines.append("Hibernate saves the session to disk; nothing is lost.")
    lines.append("Display off only blanks the monitor, the PC keeps running.")
    lines.append("Restart and Shutdown require a second Confirm press.")
    lines.append("After confirmation Windows waits 30 seconds,")
    lines.append("and Cancel Shutdown aborts it during that window.")

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Lock", callback_data="action_lock")],
        [types.InlineKeyboardButton(text="Sleep", callback_data="action_sleep")],
        [types.InlineKeyboardButton(text="Hibernate", callback_data="action_hibernate")],
        [types.InlineKeyboardButton(text="Display Off", callback_data="action_display_off")],
        [types.InlineKeyboardButton(text="Restart", callback_data="action_restart")],
        [types.InlineKeyboardButton(text="Shutdown", callback_data="action_shutdown")],
        [types.InlineKeyboardButton(text="Cancel Shutdown", callback_data="action_cancel")],
        [types.InlineKeyboardButton(text="<< Back", callback_data="menu_main")],
    ])

    await message.answer("\n".join(lines), reply_markup=keyboard, disable_web_page_preview=True)


def _power_keyboard() -> types.InlineKeyboardMarkup:
    """Small keyboard returned after a power action result."""
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Power menu", callback_data="menu_power")],
        [types.InlineKeyboardButton(text="<< Back", callback_data="menu_main")],
    ])


async def handle_power_action(callback: types.CallbackQuery, data: str) -> None:
    """Handle power buttons.

    Lock, Sleep and Cancel run immediately. Restart and Shutdown only arm a
    one-shot confirmation: nothing happens until the user presses Confirm
    within 60 seconds.
    """
    from power_manager import (
        cancel_shutdown,
        display_off,
        hibernate_system,
        lock_workstation,
        sleep_system,
    )

    message = callback.message

    if data in (
        "action_lock",
        "action_sleep",
        "action_hibernate",
        "action_display_off",
        "action_cancel",
    ):
        if data == "action_lock":
            operation, label = lock_workstation, "Lock"
        elif data == "action_sleep":
            operation, label = sleep_system, "Sleep"
        elif data == "action_hibernate":
            operation, label = hibernate_system, "Hibernate"
        elif data == "action_display_off":
            operation, label = display_off, "Display off"
        else:
            operation, label = cancel_shutdown, "Cancel shutdown"
            _clear_confirmations("restart", "shutdown")

        # subprocess calls block; run them off the event loop
        ok, result_message = await asyncio.to_thread(operation)
        mark = "OK" if ok else "FAILED"
        await message.answer(
            f"{label}: {mark}\n{md_safe(result_message)}",
            reply_markup=_power_keyboard(),
        )
        return

    if data in ("action_restart", "action_shutdown"):
        action = "restart" if data == "action_restart" else "shutdown"
        _request_confirmation(action)

        lines = [f"=== CONFIRM {action.upper()} ===", ""]
        if action == "shutdown":
            lines.append("This will power off the PC.")
        else:
            lines.append("This will reboot the PC.")
        lines.append("After confirmation Windows waits 30 seconds.")
        lines.append("Cancel Shutdown aborts it during that window.")
        lines.append("")
        lines.append("Press Confirm within 60 seconds to proceed.")

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="Confirm", callback_data=f"confirm_{action}")],
            [types.InlineKeyboardButton(text="Cancel", callback_data=f"deny_{action}")],
            [types.InlineKeyboardButton(text="<< Back", callback_data="menu_power")],
        ])
        await message.answer("\n".join(lines), reply_markup=keyboard)
        return

    await callback.answer("Unknown power action", show_alert=True)


async def handle_power_confirmation(callback: types.CallbackQuery, data: str) -> None:
    """Handle Confirm/Cancel presses for restart and shutdown."""
    message = callback.message

    if data.startswith("deny_"):
        if data.startswith("deny_killp_") and data[11:].isdigit():
            _clear_confirmations(f"kill:{int(data[11:])}")
            await message.answer(
                "Process termination cancelled. Nothing was executed.",
                reply_markup=_processes_keyboard(),
            )
            return

        action = data[len("deny_"):]
        _clear_confirmations(action)
        await message.answer(
            f"{action.upper()} cancelled. Nothing was executed.",
            reply_markup=_power_keyboard(),
        )
        return

    action = data[len("confirm_"):]
    if action not in ("restart", "shutdown"):
        await callback.answer("Invalid confirmation", show_alert=True)
        return

    # One-shot: the pending confirmation is consumed here, so double presses
    # or a stale button cannot fire the action twice
    if not _consume_confirmation(action):
        await message.answer(
            "Confirmation expired (60 seconds). Open Power Management and try again.",
            reply_markup=_power_keyboard(),
        )
        return

    from power_manager import execute_restart, execute_shutdown

    operation = execute_restart if action == "restart" else execute_shutdown
    ok, result_message = await asyncio.to_thread(operation, True)
    mark = "OK" if ok else "FAILED"
    await message.answer(
        f"{action.upper()}: {mark}\n{md_safe(result_message)}",
        reply_markup=_power_keyboard(),
    )


def _processes_keyboard() -> types.InlineKeyboardMarkup:
    """Small keyboard returned after a process action result."""
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Processes", callback_data="menu_processes")],
        [types.InlineKeyboardButton(text="<< Back", callback_data="menu_main")],
    ])


async def request_process_kill(callback: types.CallbackQuery, pid: int) -> None:
    """First Kill press: verify the target and ask for confirmation."""
    from process_manager import get_process_details, is_process_terminable

    message = callback.message
    keyboard = _processes_keyboard()

    details = await asyncio.to_thread(get_process_details, pid)
    if not details:
        await message.answer(f"Process PID {pid} is no longer running.", reply_markup=keyboard)
        return

    name = details.get("name") or "unknown"
    if not is_process_terminable(name):
        await message.answer(
            f"Refused: {md_safe(name)} is not on the termination allowlist.\n"
            "Only allowlisted, non-critical applications can be terminated.",
            reply_markup=keyboard,
        )
        return

    _request_confirmation(f"kill:{pid}")

    lines = [
        "=== CONFIRM TERMINATION ===",
        "",
        f"Process: {md_safe(name)}",
        f"PID: {pid}",
        f"Memory: {details.get('memory_mb', 0):.1f} MB",
        "",
        "Unsaved work in this application will be lost.",
        "Press Confirm within 60 seconds to terminate it.",
    ]

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Confirm", callback_data=f"confirm_killp_{pid}")],
        [types.InlineKeyboardButton(text="Cancel", callback_data=f"deny_killp_{pid}")],
        [types.InlineKeyboardButton(text="<< Back", callback_data="menu_processes")],
    ])
    await message.answer("\n".join(lines), reply_markup=keyboard)


async def confirm_process_kill(callback: types.CallbackQuery, pid: int) -> None:
    """Second press: re-verify the PID and terminate the allowlisted process."""
    from process_manager import (
        get_process_details,
        is_process_terminable,
        safe_terminate_process,
    )

    message = callback.message
    keyboard = _processes_keyboard()

    if not _consume_confirmation(f"kill:{pid}"):
        await message.answer(
            "Confirmation expired (60 seconds). Press Kill again.",
            reply_markup=keyboard,
        )
        return

    # Re-verify right before the kill: Windows recycles PIDs, so the PID from
    # the confirmation screen may now belong to a different process
    details = await asyncio.to_thread(get_process_details, pid)
    if not details:
        await message.answer(f"Process PID {pid} is no longer running.", reply_markup=keyboard)
        return

    name = details.get("name") or "unknown"
    if not is_process_terminable(name):
        await message.answer(
            f"Refused: {md_safe(name)} is not on the termination allowlist.",
            reply_markup=keyboard,
        )
        return

    ok, result_message = await asyncio.to_thread(
        safe_terminate_process, pid, "User requested via Telegram"
    )
    mark = "OK" if ok else "FAILED"
    await message.answer(
        f"Terminate {md_safe(name)} (PID {pid}): {mark}\n{md_safe(result_message)}",
        reply_markup=keyboard,
    )


async def take_screenshot(message: types.Message) -> None:
    """Take and send a screenshot (plain text messages only)."""
    from screenshot_manager import capture_desktop

    # Capture runs in a worker thread so the ~1s capture does not block the bot
    result = await asyncio.to_thread(capture_desktop)

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="<< Back", callback_data="menu_main")],
    ])

    if result.get("success") and result.get("screenshot_bytes"):
        # aiogram 3 requires uploads wrapped in BufferedInputFile, not raw BytesIO
        photo = types.BufferedInputFile(
            result["screenshot_bytes"], filename="screenshot.png"
        )
        try:
            await message.answer_photo(photo=photo, caption="Desktop screenshot")
            await message.answer("Screenshot sent.", reply_markup=keyboard)
            return
        except Exception as e:
            await message.answer(f"Failed to send screenshot: {md_safe(e)}", reply_markup=keyboard)
            return
    elif result.get("desktop_locked"):
        await message.answer("Desktop is locked. Screenshot may be limited or unavailable.", reply_markup=keyboard)
    else:
        await message.answer(md_safe(result.get("message", "Screenshot capture failed.")), reply_markup=keyboard)


async def show_process_info(message: types.Message) -> None:
    """Show process information with Kill buttons for allowlisted apps."""
    from process_manager import (
        get_processes_by_cpu,
        get_processes_by_ram,
        get_terminable_allowlist,
        is_process_terminable,
    )

    def _scan():
        return get_processes_by_cpu(limit=10), get_processes_by_ram(limit=10)

    # psutil process scans block; keep them off the event loop
    cpu_procs, ram_procs = await asyncio.to_thread(_scan)
    allowlist = get_terminable_allowlist()

    def _mark(name: str) -> str:
        return " [K]" if is_process_terminable(name, allowlist) else ""

    lines = ["=== RUNNING PROCESSES ===", ""]
    lines.append("Top by CPU:")
    if cpu_procs:
        for p in cpu_procs:
            lines.append(
                f"  {md_safe(p['name'])}{_mark(p['name'])} (PID {p['pid']}) - {p['cpu_percent']}%"
            )
    else:
        lines.append("  No data available")

    lines.append("")
    lines.append("Top by RAM:")
    if ram_procs:
        for p in ram_procs:
            lines.append(
                f"  {md_safe(p['name'])}{_mark(p['name'])} (PID {p['pid']}) - {p['ram_mb']:.1f} MB"
            )
    else:
        lines.append("  No data available")

    lines.append("")
    lines.append("[K] = can be terminated from here.")
    lines.append("Termination is only allowed for allowlisted non-critical apps.")
    lines.append("Every kill requires a second Confirm press.")

    # Kill buttons: dedupe by PID, allowlisted only, capped so the screen
    # stays readable and callback payloads stay small (killp_<pid>)
    kill_buttons = []
    seen_pids = set()
    for p in list(ram_procs) + list(cpu_procs):
        pid = p["pid"]
        if pid in seen_pids:
            continue
        seen_pids.add(pid)
        if not is_process_terminable(p["name"], allowlist):
            continue
        kill_buttons.append([
            types.InlineKeyboardButton(
                text=f"Kill {p['name']} ({pid})",
                callback_data=f"killp_{pid}",
            )
        ])
        if len(kill_buttons) >= 8:
            break

    kill_buttons.append([types.InlineKeyboardButton(text="<< Back", callback_data="menu_main")])
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=kill_buttons)

    await message.answer("\n".join(lines), reply_markup=keyboard, disable_web_page_preview=True)


CHART_METRICS = {
    "cpu": "CPU",
    "gpu": "GPU",
    "ram": "RAM",
    "disk": "Disk",
}
CHART_RANGES = {
    "5m": "last 5 minutes",
    "1h": "last hour",
    "24h": "last 24 hours",
}

# Per-chat drive lists for the disk chart picker. The list is rebuilt from the
# database and the live drive table every time the picker is opened.
_CHART_DRIVES: dict = {}


def _disk_drives() -> list:
    """Return known drive identifiers, merging history with live drives."""
    from config import config
    from database import Database
    from file_manager import list_drives

    drives: list = []
    seen: set = set()

    def add(name) -> None:
        text = str(name or "").strip()
        if not text:
            return
        key = os.path.normcase(text.rstrip("\\/"))
        if key in seen:
            return
        seen.add(key)
        drives.append(text)

    db_path = config["DATA_DIR"] / "pc_remote.db"
    try:
        db = Database(db_path)
        try:
            for device in db.get_disk_devices():
                add(device)
        finally:
            db.close()
    except Exception:
        pass

    try:
        for item in list_drives():
            add(item.get("name") or item.get("path"))
    except Exception:
        pass

    return drives


async def _show_disk_picker(
    message: types.Message, time_range: str = "5m"
) -> None:
    """Show a per-drive picker for disk charts."""
    try:
        drives = await asyncio.to_thread(_disk_drives)
    except Exception:
        drives = []
    _CHART_DRIVES[message.chat.id] = drives

    if not drives:
        await message.answer(
            "No drives found yet. Disk history appears after the monitor "
            "records its first sample.",
            reply_markup=_charts_keyboard(),
        )
        return

    lines = [
        "=== DISK CHART ===",
        "",
        "Choose a drive and time range.",
    ]
    if time_range in CHART_RANGES:
        lines.append(f"Suggested range: {CHART_RANGES[time_range]}.")

    rows: list = []
    for index, drive in enumerate(drives):
        label = drive.rstrip("\\/") or drive
        rows.append([
            types.InlineKeyboardButton(
                text=f"{label} {range_label}",
                callback_data=f"chart_diskd_{index}_{range_key}",
            )
            for range_key, range_label in CHART_RANGES.items()
        ])
    rows.append([
        types.InlineKeyboardButton(text="<< Charts", callback_data="chart_menu"),
    ])
    await message.answer(
        "\n".join(lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _send_chart(
    message: types.Message,
    metric: str,
    time_range: str,
    device_id: str = None,
) -> None:
    """Render and send one chart, including the disk drive name."""
    from charts import render_chart
    from config import config

    db_path = config["DATA_DIR"] / "pc_remote.db"

    try:
        await message.bot.send_chat_action(message.chat.id, "upload_photo")
    except Exception:
        pass

    # Chart rendering is CPU-bound and creates its own DB connection, so it
    # must stay off the event loop.
    image = await asyncio.to_thread(
        render_chart, db_path, metric, time_range, device_id
    )

    if not image:
        target = f" {device_id}" if device_id else ""
        await message.answer(
            f"No {CHART_METRICS[metric]}{target} history for the "
            f"{CHART_RANGES[time_range]} yet.\n"
            "History is recorded in the background; try again in a minute.",
            reply_markup=_charts_keyboard(),
        )
        return

    photo = types.BufferedInputFile(
        image, filename=f"{metric}_{time_range}.png"
    )
    target = f" {device_id}" if device_id else ""
    caption = (
        f"{CHART_METRICS[metric]}{target} chart - "
        f"{CHART_RANGES[time_range]}"
    )
    try:
        await message.answer_photo(
            photo=photo, caption=caption, reply_markup=_charts_keyboard()
        )
    except Exception as e:
        await message.answer(
            f"Failed to send chart: {md_safe(e)}",
            reply_markup=_charts_keyboard(),
        )


async def _show_disk_chart(message: types.Message, selection: str) -> None:
    """Handle ``diskd_<index>_<range>`` from the disk drive picker."""
    rest = selection[len("diskd_"):]
    index_text, _, time_range = rest.partition("_")
    drives = _CHART_DRIVES.get(message.chat.id) or []
    if (
        not index_text.isdigit()
        or time_range not in CHART_RANGES
        or int(index_text) >= len(drives)
    ):
        await message.answer(
            "Drive selection expired. Choose a drive again.",
            reply_markup=_charts_keyboard(),
        )
        return
    await _send_chart(
        message, "disk", time_range, device_id=drives[int(index_text)]
    )


def _charts_keyboard() -> types.InlineKeyboardMarkup:
    """Build the chart menu keyboard (metric rows x time ranges)."""
    rows = []
    for metric, label in CHART_METRICS.items():
        if metric == "disk":
            rows.append([
                types.InlineKeyboardButton(
                    text="Disk: choose drive",
                    callback_data="chart_disk",
                )
            ])
            continue
        rows.append([
            types.InlineKeyboardButton(
                text=f"{label} {range_label}",
                callback_data=f"chart_{metric}_{range_key}",
            )
            for range_key, range_label in CHART_RANGES.items()
        ])
    rows.append([
        types.InlineKeyboardButton(text="<< Main Menu", callback_data="menu_main"),
    ])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def show_chart_menu(message: types.Message) -> None:
    """Show the chart selection screen."""
    lines = [
        "=== CHARTS ===",
        "",
        "Select a metric and time range.",
        "Disk charts ask for a drive first, so C: and D: stay separate.",
        "Charts are generated from stored measurements; right after startup",
        "the history may still be empty.",
    ]
    await message.answer(
        "\n".join(lines),
        reply_markup=_charts_keyboard(),
        disable_web_page_preview=True,
    )


async def show_chart(message: types.Message, selection: str) -> None:
    """Render and send a chart for a callback selection like ``cpu_1h``."""
    if selection == "menu":
        await show_chart_menu(message)
        return

    if selection == "disk":
        await _show_disk_picker(message)
        return

    if selection.startswith("diskd_"):
        await _show_disk_chart(message, selection)
        return

    metric, _, time_range = selection.partition("_")
    if metric not in CHART_METRICS or time_range not in CHART_RANGES:
        await message.answer(
            "Unknown chart selection. Use the Charts menu.",
            reply_markup=_charts_keyboard(),
        )
        return

    if metric == "disk":
        await _show_disk_picker(message, time_range)
        return

    await _send_chart(message, metric, time_range)


async def show_alerts_history(message: types.Message) -> None:
    """Show alerts history."""
    from database import Database
    from config import config
    from datetime import datetime

    db = Database(config["DATA_DIR"] / "pc_remote.db")
    try:
        alerts = db.get_recent_alerts(limit=20, acknowledged_only=False)
    finally:
        db.close()

    lines = ["=== ALERTS HISTORY ===", ""]
    if alerts:
        for ts, level, category, message_text, acknowledged in alerts:
            time_str = (
                datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                if isinstance(ts, (int, float))
                else str(ts)
            )
            lines.append(f"{'[ACK]' if acknowledged else '[NEW]'} {time_str} {md_safe(category)}: {md_safe(message_text)}")
    else:
        lines.append("No alerts recorded")

    lines.append("")
    lines.append(f"Total shown: {len(alerts)}")

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="<< Back", callback_data="menu_main")],
    ])

    await message.answer("\n".join(lines), reply_markup=keyboard, disable_web_page_preview=True)


async def back_to_main(message: types.Message) -> None:
    """Return to main menu."""
    from handlers.common import show_main_menu
    await show_main_menu(message)