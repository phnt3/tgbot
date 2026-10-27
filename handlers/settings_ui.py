"""In-bot settings screens.

Everything the user can change lives in the bot database (see
``settings_store``); this module is only the Telegram interface.  Screens are
plain text with inline buttons, following the same rules as the rest of the
bot: no Markdown, dynamic strings passed through ``md_safe``.

Callback data always starts with ``set_`` and is handled by
``handle_callback``.  Free-form values (names, exact numbers, times) are
collected through one-shot text prompts kept in memory for a few minutes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import string
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiogram import types
from aiogram.types import BufferedInputFile

from . import md_safe
from alerts import (
    DEFAULT_RECOVERY_MARGINS,
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    load_alert_settings,
    save_alert_settings,
)
from application_manager import (
    add_application,
    load_applications_config,
    remove_application,
    save_applications_config,
)
from file_manager import list_directory
from gaming_profiles import load_profiles_config, save_profiles_config
from notifier import load_notification_settings, save_notification_settings
from security import is_user_authorized, validate_application_name

logger = logging.getLogger(__name__)

PAGE_SIZE = 8
TEXT_TTL_SECONDS = 300
IMPORT_TTL_SECONDS = 600
RESET_TTL_SECONDS = 60
PICKER_EXTENSIONS = (".exe", ".lnk")

# One-shot free-text prompts, keyed by chat id.
_PENDING_TEXT: Dict[int, Dict[str, Any]] = {}
# Armed "send me a JSON backup" state, keyed by chat id (expiry timestamp).
_PENDING_IMPORT: Dict[int, float] = {}
# Armed "reset settings" confirmation, keyed by chat id (expiry timestamp).
_PENDING_RESET: Dict[int, float] = {}
# File picker state, keyed by chat id.
_PICKER: Dict[int, Dict[str, Any]] = {}


# --- small helpers ---------------------------------------------------------


def _chat_id(source: Any) -> int:
    """Return the chat id for a Message/CallbackQuery-like object."""
    chat = getattr(source, "chat", None)
    if chat is not None and getattr(chat, "id", None) is not None:
        try:
            return int(chat.id)
        except (TypeError, ValueError):
            pass
    user = getattr(source, "from_user", None)
    if user is not None and getattr(user, "id", None) is not None:
        try:
            return int(user.id)
        except (TypeError, ValueError):
            pass
    return 0


def _button(text: str, data: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text=text, callback_data=data)


def _markup(rows: List[List[types.InlineKeyboardButton]]) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def _send(
    message: Any,
    lines: List[str],
    rows: List[List[types.InlineKeyboardButton]],
) -> None:
    """Render a screen, editing the current bot message when possible."""
    text = "\n".join(lines)
    markup = _markup(rows) if rows else None
    edit = getattr(message, "edit_text", None)
    if edit is not None:
        try:
            await edit(text, reply_markup=markup, disable_web_page_preview=True)
            return
        except Exception as e:  # user command message, not a bot message
            logger.debug("Could not edit settings message: %s", e)
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


def _clip(value: Any, limit: int = 48) -> str:
    """Shorten text for a button label."""
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "~"


def _fmt_num(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "never"
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}"


def _fmt_minutes(seconds: Any) -> str:
    try:
        number = float(seconds)
    except (TypeError, ValueError):
        return "never"
    if not math.isfinite(number):
        return "never"
    minutes = number / 60.0
    return f"{_fmt_num(minutes)} min"


def _fmt_duration(seconds: Any) -> str:
    try:
        number = int(float(seconds))
    except (TypeError, ValueError):
        return str(seconds)
    if number % 3600 == 0:
        return f"{number // 3600} h"
    if number % 60 == 0:
        return f"{number // 60} min"
    return f"{number} s"


def _fmt_hhmm(value: Any) -> str:
    text = str(value or "").strip()
    return text or "--:--"


_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
_HHMM_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")


def _parse_number(text: Any) -> float:
    match = _NUMBER_RE.search(str(text or ""))
    if not match:
        raise ValueError("Send a number, for example 85.")
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        raise ValueError("Send a number, for example 85.") from None


def _parse_minutes(text: Any) -> float:
    raw = str(text or "").strip().lower()
    if raw in {"never", "once", "off", "no"}:
        return math.inf
    value = _parse_number(raw)
    if value < 0 or value > 1440:
        raise ValueError("Send minutes between 0 and 1440, or \"never\".")
    return value


def _parse_duration(text: Any) -> int:
    match = re.fullmatch(
        r"\s*(\d+(?:[.,]\d+)?)\s*(s|sec|secs|m|min|mins|h|hour|hours)?\s*",
        str(text or "").lower(),
    )
    if not match:
        raise ValueError("Send seconds, for example 60, or 5 min.")
    amount = float(match.group(1).replace(",", "."))
    unit = match.group(2) or "s"
    if unit.startswith("h"):
        amount *= 3600
    elif unit.startswith("m"):
        amount *= 60
    seconds = int(round(amount))
    if seconds < MIN_INTERVAL_SECONDS or seconds > MAX_INTERVAL_SECONDS:
        raise ValueError(
            f"Interval must be between {MIN_INTERVAL_SECONDS} s and 24 h."
        )
    return seconds


def _parse_hhmm(text: Any) -> str:
    match = _HHMM_RE.fullmatch(str(text or "").strip())
    if not match:
        raise ValueError("Send a time as HH:MM, for example 23:00.")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("Hours must be 0-23 and minutes 0-59.")
    return f"{hour:02d}:{minute:02d}"


def _suffix_index(data: str, prefix: str) -> Optional[int]:
    """Parse ``<prefix><digits>`` and return the digits, else ``None``."""
    if not data.startswith(prefix):
        return None
    tail = data[len(prefix) :]
    if not tail.isdigit() or len(tail) > 6:
        return None
    return int(tail)


def _applications() -> List[Tuple[str, str]]:
    """Return the allowlist as a name/path list sorted by name."""
    apps = load_applications_config()
    return sorted(
        ((str(name), str(path)) for name, path in apps.items()),
        key=lambda item: item[0].casefold(),
    )


def _app_exists(path: str) -> bool:
    try:
        return os.path.exists(path)
    except OSError:
        return False


# --- notifications ---------------------------------------------------------


NOTIFICATION_FIELDS: Dict[str, Dict[str, Any]] = {
    "battery_warning": {
        "title": "BATTERY WARNING",
        "label": "Battery warning",
        "unit": "%",
        "step": 5,
        "low": 5,
        "high": 100,
        "integer": True,
        "help": "Send an alert when the battery drops to this level.",
    },
    "battery_critical": {
        "title": "BATTERY CRITICAL",
        "label": "Battery critical",
        "unit": "%",
        "step": 5,
        "low": 1,
        "high": 100,
        "integer": True,
        "help": "Send an urgent alert at this level. It cannot exceed the warning level.",
    },
    "app_cooldown_minutes": {
        "title": "APPLICATION COOLDOWN",
        "label": "App cooldown",
        "unit": "min",
        "step": 1,
        "low": 0,
        "high": 1440,
        "integer": False,
        "help": "Silence repeated launch alerts for the same application.",
    },
    "crash_cooldown_minutes": {
        "title": "CRASH COOLDOWN",
        "label": "Crash cooldown",
        "unit": "min",
        "step": 1,
        "low": 0,
        "high": 1440,
        "integer": False,
        "help": "Silence repeated crash alerts for the same process.",
    },
}

TOGGLE_FIELDS: Dict[str, Tuple[str, str]] = {
    "notify_login": ("Login", "Login / unlock"),
    "notify_battery": ("Battery", "Battery alerts"),
    "notify_apps": ("Apps", "Application launches"),
    "notify_crashes": ("Crashes", "Crashes"),
}


def _on_off(value: Any) -> str:
    return "ON" if value else "OFF"


def _notif_limits(field: str, data: Dict[str, Any]) -> Tuple[int, int]:
    spec = NOTIFICATION_FIELDS[field]
    low = int(spec["low"])
    high = int(spec["high"])
    if field == "battery_critical":
        high = int(data.get("battery_warning", high))
    return low, max(low, high)


def _notif_value(field: str, data: Dict[str, Any]) -> Any:
    return data.get(field, NOTIFICATION_FIELDS[field]["low"])


def _notif_value_text(field: str, value: Any) -> str:
    unit = NOTIFICATION_FIELDS[field]["unit"]
    return f"{_fmt_num(value)} {unit}".strip()


async def show_notifications(message: Any) -> None:
    """Notifications screen: event toggles, thresholds and quiet hours."""
    data = load_notification_settings()
    quiet = data.get("quiet_hours") or {}

    lines = [
        "=== NOTIFICATIONS ===",
        "Telegram messages about events on this PC.",
        "Tap a switch to turn that event type on or off.",
        "",
    ]
    for field, (_short, label) in TOGGLE_FIELDS.items():
        lines.append(f"{label}: {_on_off(data.get(field))}")

    lines.append("")
    lines.append(
        "Battery: warning "
        f"{_fmt_num(data.get('battery_warning'))}%, critical "
        f"{_fmt_num(data.get('battery_critical'))}%"
    )
    lines.append(
        "Repeat app alerts after: "
        f"{_fmt_minutes(data.get('app_cooldown_minutes', 0) * 60)}"
    )
    lines.append(
        "Repeat crash alerts after: "
        f"{_fmt_minutes(data.get('crash_cooldown_minutes', 0) * 60)}"
    )
    lines.append("")
    quiet_enabled = bool(quiet.get("enabled"))
    lines.append(f"Quiet hours: {_on_off(quiet_enabled)}")
    lines.append(
        f"Window: {_fmt_hhmm(quiet.get('start'))} - {_fmt_hhmm(quiet.get('end'))}"
    )
    if quiet_enabled and str(quiet.get("start")) == str(quiet.get("end")):
        lines.append("Warning: start and end are equal, so quiet hours are OFF.")
    if quiet_enabled:
        lines.append("During quiet hours only critical battery alerts are sent.")

    rows: List[List[types.InlineKeyboardButton]] = []
    toggles = list(TOGGLE_FIELDS.items())
    for index in range(0, len(toggles), 2):
        row = []
        for field, (short, _label) in toggles[index : index + 2]:
            row.append(
                _button(f"{short}: {_on_off(data.get(field))}", f"set_nt_{field[7:]}")
            )
        rows.append(row)

    rows.append([
        _button(
            "Battery warning: " + _notif_value_text("battery_warning", data.get("battery_warning")),
            "set_nb_battery_warning",
        ),
        _button(
            "Battery critical: " + _notif_value_text("battery_critical", data.get("battery_critical")),
            "set_nb_battery_critical",
        ),
    ])
    rows.append([
        _button(
            "App cooldown: " + _fmt_minutes(data.get("app_cooldown_minutes", 0) * 60),
            "set_nb_app_cooldown_minutes",
        ),
        _button(
            "Crash cooldown: " + _fmt_minutes(data.get("crash_cooldown_minutes", 0) * 60),
            "set_nb_crash_cooldown_minutes",
        ),
    ])
    rows.append([_button(f"Quiet hours: {_on_off(quiet_enabled)}", "set_nq_toggle")])
    rows.append([
        _button(f"Quiet start: {_fmt_hhmm(quiet.get('start'))}", "set_nq_start"),
        _button(f"Quiet end: {_fmt_hhmm(quiet.get('end'))}", "set_nq_end"),
    ])
    rows.append([_button("<< Back", "set_main")])

    await _send(message, lines, rows)


async def show_notification_field(message: Any, field: str) -> None:
    """Detail screen for one numeric notification setting."""
    if field not in NOTIFICATION_FIELDS:
        await show_notifications(message)
        return

    spec = NOTIFICATION_FIELDS[field]
    data = load_notification_settings()
    value = _notif_value(field, data)
    low, high = _notif_limits(field, data)
    unit = spec["unit"]

    lines = [
        f"=== {spec['title']} ===",
        str(spec["help"]),
        "",
        f"Current: {_notif_value_text(field, value)}",
        f"Allowed: {low}-{high} {unit}".strip(),
        "",
        "Use the buttons or send an exact value.",
    ]

    step = spec["step"]
    rows = [
        [
            _button(f"-{step} {unit}", f"set_nb_{field}_d"),
            _button(f"+{step} {unit}", f"set_nb_{field}_u"),
        ],
        [_button("Set exact value", f"set_nbx_{field}")],
        [_button("<< Back", "set_notif")],
    ]
    await _send(message, lines, rows)


def _store_notif_value(field: str, raw_value: float, exact: bool) -> float:
    """Clamp and save one numeric notification value."""
    spec = NOTIFICATION_FIELDS[field]
    data = load_notification_settings()
    low, high = _notif_limits(field, data)
    if not math.isfinite(raw_value):
        raise ValueError("Send a number between " f"{low} and {high}.")
    value: Any = int(round(raw_value)) if spec["integer"] else round(raw_value, 1)
    if exact and (value < low or value > high):
        unit = spec["unit"]
        raise ValueError(f"Allowed range: {low}-{high} {unit}.".strip())
    value = max(low, min(high, value))
    data[field] = value
    save_notification_settings(data)
    return value


def _toggle_notification(key: str) -> bool:
    data = load_notification_settings()
    data[key] = not bool(data.get(key))
    save_notification_settings(data)
    return bool(data[key])


def _toggle_quiet_hours() -> bool:
    data = load_notification_settings()
    quiet = dict(data.get("quiet_hours") or {})
    quiet["enabled"] = not bool(quiet.get("enabled"))
    data["quiet_hours"] = quiet
    save_notification_settings(data)
    return bool(quiet["enabled"])


def _set_quiet_time(which: str, value: str) -> None:
    data = load_notification_settings()
    quiet = dict(data.get("quiet_hours") or {})
    quiet[which] = value
    data["quiet_hours"] = quiet
    save_notification_settings(data)


# --- monitoring alerts -----------------------------------------------------


ALERT_FIELDS: Dict[str, Dict[str, Any]] = {
    "cpu_temperature": {
        "title": "CPU TEMPERATURE",
        "label": "CPU temp",
        "long": "CPU temperature",
        "unit": "C",
        "low": 30,
        "high": 110,
        "step": 5,
    },
    "gpu_temperature": {
        "title": "GPU TEMPERATURE",
        "label": "GPU temp",
        "long": "GPU temperature",
        "unit": "C",
        "low": 30,
        "high": 110,
        "step": 5,
    },
    "ram_utilization": {
        "title": "RAM UTILIZATION",
        "label": "RAM usage",
        "long": "RAM usage",
        "unit": "%",
        "low": 10,
        "high": 100,
        "step": 5,
    },
    "disk_utilization": {
        "title": "DISK UTILIZATION",
        "label": "Disk usage",
        "long": "disk usage",
        "unit": "%",
        "low": 10,
        "high": 100,
        "step": 5,
    },
}


def _alert_value_text(key: str, value: Any) -> str:
    unit = ALERT_FIELDS[key]["unit"]
    return f"{_fmt_num(value)} {unit}".strip()


async def show_alerts(message: Any) -> None:
    """Monitoring thresholds, repeat cooldowns and sampling interval."""
    data = load_alert_settings()
    thresholds = data.get("thresholds") or {}
    cooldowns = data.get("cooldowns") or {}
    interval = data.get("interval_seconds", 60)

    lines = [
        "=== MONITORING ALERTS ===",
        f"The background monitor samples this PC every {_fmt_duration(interval)}.",
        "",
        "Send an alert when a value goes above:",
    ]
    for key, spec in ALERT_FIELDS.items():
        lines.append(
            f"{spec['long'].capitalize()}: "
            f"{_alert_value_text(key, thresholds.get(key))}"
        )

    lines.append("")
    margin = _fmt_num(DEFAULT_RECOVERY_MARGINS.get("cpu_temperature", 2))
    lines.append(
        f"An alert clears once the value drops {margin} below its threshold."
    )
    lines.append("")
    lines.append("Repeat while a breach stays active:")
    for key, spec in ALERT_FIELDS.items():
        lines.append(
            f"{spec['long'].capitalize()}: "
            f"{_fmt_minutes(cooldowns.get(key))}"
        )

    rows: List[List[types.InlineKeyboardButton]] = []
    keys = list(ALERT_FIELDS)
    for index in range(0, len(keys), 2):
        row = []
        for key in keys[index : index + 2]:
            row.append(
                _button(
                    f"{ALERT_FIELDS[key]['label']}: "
                    f"{_alert_value_text(key, thresholds.get(key))}",
                    f"set_at_{key}",
                )
            )
        rows.append(row)
    rows.append([_button("Repeat cooldowns", "set_acooldowns")])
    rows.append([_button(f"Check interval: {_fmt_duration(interval)}", "set_ai")])
    rows.append([_button("<< Back", "set_main")])
    await _send(message, lines, rows)


async def show_alert_cooldowns(message: Any) -> None:
    """List repeat cooldowns for every monitored metric."""
    data = load_alert_settings()
    cooldowns = data.get("cooldowns") or {}

    lines = [
        "=== REPEAT COOLDOWNS ===",
        "While a value stays above its threshold, the alert is repeated",
        "after the cooldown below. \"Never repeat\" sends one alert per breach.",
        "",
    ]
    for key, spec in ALERT_FIELDS.items():
        lines.append(
            f"{spec['long'].capitalize()}: {_fmt_minutes(cooldowns.get(key))}"
        )

    rows = [
        [
            _button(
                f"{ALERT_FIELDS[key]['label']}: {_fmt_minutes(cooldowns.get(key))}",
                f"set_ac_{key}",
            )
        ]
        for key in ALERT_FIELDS
    ]
    rows.append([_button("<< Back", "set_alerts")])
    await _send(message, lines, rows)


async def show_alert_threshold(message: Any, key: str) -> None:
    """Detail screen for one monitoring threshold."""
    if key not in ALERT_FIELDS:
        await show_alerts(message)
        return

    spec = ALERT_FIELDS[key]
    data = load_alert_settings()
    value = (data.get("thresholds") or {}).get(key)
    unit = spec["unit"]

    lines = [
        f"=== {spec['title']} ===",
        f"Send an alert when {spec['long']} goes above the limit.",
        "The alert clears once the value drops below the limit.",
        "",
        f"Current: {_alert_value_text(key, value)}",
        f"Allowed: {spec['low']}-{spec['high']} {unit}".strip(),
        "",
        "Use the buttons or send an exact value.",
    ]
    rows = [
        [
            _button(f"-{spec['step']} {unit}", f"set_at_{key}_d"),
            _button(f"+{spec['step']} {unit}", f"set_at_{key}_u"),
        ],
        [_button("Set exact value", f"set_atx_{key}")],
        [_button("<< Back", "set_alerts")],
    ]
    await _send(message, lines, rows)


async def show_alert_cooldown(message: Any, key: str) -> None:
    """Detail screen for one repeat cooldown."""
    if key not in ALERT_FIELDS:
        await show_alerts(message)
        return

    spec = ALERT_FIELDS[key]
    data = load_alert_settings()
    value = (data.get("cooldowns") or {}).get(key)
    current = _fmt_minutes(value)
    never = not math.isfinite(float(value or 0))

    if never:
        repeat = "The alert is not repeated while the problem lasts."
    else:
        repeat = (
            f"While {spec['long']} stays above its threshold, the alert is\n"
            f"repeated every {current}."
        )

    lines = [
        f"=== {spec['title']} COOLDOWN ===",
        repeat,
        "",
        f"Current: {'never repeat' if never else current}",
        "",
        "Use the buttons, send minutes, or choose \"Never repeat\".",
    ]
    rows = [
        [
            _button("-1 min", f"set_ac_{key}_d"),
            _button("+1 min", f"set_ac_{key}_u"),
        ],
        [_button("Never repeat", f"set_ac_{key}_never")],
        [_button("Set exact value", f"set_acx_{key}")],
        [_button("<< Back", "set_acooldowns")],
    ]
    await _send(message, lines, rows)


def _store_alert_threshold(key: str, raw_value: float, exact: bool) -> float:
    spec = ALERT_FIELDS[key]
    if not math.isfinite(raw_value):
        raise ValueError(f"Send a number between {spec['low']} and {spec['high']}.")
    value = round(raw_value, 1)
    if exact and (value < spec["low"] or value > spec["high"]):
        raise ValueError(
            f"Allowed range: {spec['low']}-{spec['high']} {spec['unit']}."
        )
    value = max(float(spec["low"]), min(float(spec["high"]), value))
    data = load_alert_settings()
    data.setdefault("thresholds", {})[key] = value
    save_alert_settings(data)
    return value


def _store_alert_cooldown(key: str, raw_value: Optional[float], exact: bool) -> None:
    """Store a cooldown given in minutes; the settings layer uses seconds."""
    data = load_alert_settings()
    if raw_value is None or not math.isfinite(raw_value):
        # ``None`` is how the JSON layer represents "never repeat".
        data.setdefault("cooldowns", {})[key] = None
    else:
        minutes = round(float(raw_value), 1)
        if exact and (minutes < 0 or minutes > 1440):
            raise ValueError("Send minutes between 0 and 1440, or \"never\".")
        minutes = max(0.0, min(1440.0, minutes))
        data.setdefault("cooldowns", {})[key] = round(minutes * 60.0, 1)
    save_alert_settings(data)


def _store_alert_interval(seconds: int) -> int:
    value = int(max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, seconds)))
    data = load_alert_settings()
    data["interval_seconds"] = value
    save_alert_settings(data)
    return value


async def show_alert_interval(message: Any) -> None:
    """Sampling interval detail screen."""
    data = load_alert_settings()
    current = int(data.get("interval_seconds", 60))

    lines = [
        "=== CHECK INTERVAL ===",
        "How often the monitor samples this PC.",
        f"Higher values use less CPU; lower values react faster.",
        "",
        f"Current: {_fmt_duration(current)}",
        f"Allowed: {MIN_INTERVAL_SECONDS} s - 24 h",
    ]
    presets = (15, 30, 60, 300, 900)
    rows: List[List[types.InlineKeyboardButton]] = []
    for index in range(0, len(presets), 2):
        row = []
        for seconds in presets[index : index + 2]:
            label = _fmt_duration(seconds) + (" (current)" if seconds == current else "")
            row.append(_button(label, f"set_ai_{seconds}"))
        rows.append(row)
    rows.append([_button("Set exact value", "set_aix")])
    rows.append([_button("<< Back", "set_alerts")])
    await _send(message, lines, rows)


# --- applications ----------------------------------------------------------


async def show_applications(message: Any, page: int = 0) -> None:
    """Paginated list of the launch allowlist."""
    apps = _applications()
    pages = max(1, (len(apps) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    chunk = apps[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    missing = sum(1 for _name, path in apps if not _app_exists(path))

    lines = [
        "=== APPLICATIONS ===",
        "Programs the bot is allowed to launch.",
        "Tap a name to launch, rename or remove it.",
        "",
        f"{len(apps)} configured, {missing} with a missing path.",
        f"Page {page + 1}/{pages}",
        "",
    ]
    if not apps:
        lines.append("No applications configured yet. Tap Add application below.")
    else:
        for offset, (name, path) in enumerate(chunk):
            index = page * PAGE_SIZE + offset
            mark = "+" if _app_exists(path) else "!"
            lines.append(f"{mark} {index + 1}. {md_safe(name)}")

    rows: List[List[types.InlineKeyboardButton]] = []
    for offset, (name, path) in enumerate(chunk):
        index = page * PAGE_SIZE + offset
        mark = "+" if _app_exists(path) else "!"
        rows.append([_button(f"{mark} {_clip(name, 40)}", f"set_apd_{index}")])

    rows.append([_button("Add application", "set_add")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(_button("< Prev", f"set_apps_{page - 1}"))
        if page < pages - 1:
            nav.append(_button("Next >", f"set_apps_{page + 1}"))
        if nav:
            rows.append(nav)
    rows.append([_button("<< Back", "set_main")])
    await _send(message, lines, rows)


async def show_application_detail(message: Any, index: int) -> None:
    """One application entry with launch/edit actions."""
    apps = _applications()
    if index < 0 or index >= len(apps):
        await show_applications(message)
        return

    name, path = apps[index]
    exists = _app_exists(path)
    lines = [
        "=== APPLICATION ===",
        f"Name: {md_safe(name)}",
        f"Path: {md_safe(path)}",
        f"Status: {'found on disk' if exists else 'PATH NOT FOUND'}",
        "",
        "Launch uses the same safe allowlist as the main launcher.",
    ]
    rows = [
        [_button("Launch", f"set_ap_l_{index}")],
        [_button("Rename", f"set_ap_n_{index}")],
        [_button("Change executable", f"set_ap_p_{index}")],
        [_button("Remove", f"set_ap_r_{index}")],
        [_button("<< Back", f"set_apps_{index // PAGE_SIZE}")],
    ]
    await _send(message, lines, rows)


async def show_remove_application(message: Any, index: int) -> None:
    """Removal confirmation; the executable on disk is never touched."""
    apps = _applications()
    if index < 0 or index >= len(apps):
        await show_applications(message)
        return
    name, path = apps[index]
    lines = [
        "=== REMOVE APPLICATION ===",
        f"Remove \"{md_safe(name)}\" from the launch allowlist?",
        "",
        "The program and its files are NOT deleted.",
        "A gaming profile that still lists this name will report it as",
        "missing when the profile is launched.",
        "",
        f"Path: {md_safe(path)}",
    ]
    rows = [
        [_button("Yes, remove", f"set_ap_ry_{index}")],
        [_button("Cancel", f"set_apd_{index}")],
    ]
    await _send(message, lines, rows)


def _rename_application(old_name: str, new_name: str) -> str:
    """Rename an allowlist entry and keep profile references working."""
    new_name = str(new_name or "").strip()
    if not validate_application_name(new_name):
        raise ValueError(
            "Use letters, digits, spaces and . - _ + only, e.g. My Game."
        )
    apps = load_applications_config()
    if old_name not in apps:
        raise ValueError("That application is no longer configured.")
    if new_name == old_name:
        return old_name
    for existing in apps:
        if existing != old_name and existing.casefold() == new_name.casefold():
            raise ValueError(f"\"{existing}\" already exists; names must be unique.")

    renamed: Dict[str, str] = {}
    for name, path in apps.items():
        renamed[new_name if name == old_name else name] = path
    save_applications_config(renamed)

    profiles = load_profiles_config()
    changed = False
    for spec in profiles.values():
        if not isinstance(spec, dict):
            continue
        for bucket in ("apps", "optional"):
            keys = spec.get(bucket) or []
            updated = []
            for key in keys:
                if str(key).casefold() == old_name.casefold():
                    updated.append(new_name)
                    changed = True
                else:
                    updated.append(key)
            spec[bucket] = updated
    if changed:
        save_profiles_config(profiles)
    return new_name


# --- executable picker -----------------------------------------------------


def _default_application_name(path: str) -> str:
    stem = Path(str(path)).stem
    cleaned = re.sub(r"[^A-Za-z0-9 .\-_+]", " ", stem)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:48].strip() or "Application"


def _application_index(name: str) -> int:
    for index, (existing, _path) in enumerate(_applications()):
        if existing == name:
            return index
    return -1


def _start_picker(
    chat_id: int,
    mode: str,
    target: Optional[str] = None,
    directory: Optional[Path] = None,
) -> None:
    _PICKER[chat_id] = {
        "mode": mode,
        "target": target,
        "dir": str(directory) if directory else None,
        "page": 0,
        "selected": None,
        "name": None,
    }


def _drive_entries() -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for letter in string.ascii_uppercase:
        path = Path(f"{letter}:\\")
        try:
            if path.is_dir():
                entries.append(
                    {"name": f"{letter}:", "path": path, "is_dir": True, "size": 0}
                )
        except OSError:
            continue
    return entries


def _list_picker(state: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Return (entries, error).  No directory means the drive list."""
    directory = state.get("dir")
    if not directory:
        return _drive_entries(), None
    try:
        listing = list_directory(Path(directory))
    except (OSError, NotADirectoryError) as e:
        return [], str(e) or "cannot open this folder"

    entries: List[Dict[str, Any]] = []
    for entry in listing.get("entries", []):
        try:
            if entry.get("is_dir"):
                entries.append(entry)
            elif Path(entry.get("path")).suffix.lower() in PICKER_EXTENSIONS:
                entries.append(entry)
        except (TypeError, ValueError):
            continue
    return entries, None


def _resolve_picker_entry(chat_id: int, index: int) -> Optional[Dict[str, Any]]:
    state = _PICKER.get(chat_id)
    if not state:
        return None
    entries, _error = _list_picker(state)
    if 0 <= index < len(entries):
        return entries[index]
    return None


async def show_picker(message: Any, chat_id: int) -> None:
    """Folder browser limited to .exe and .lnk files."""
    state = _PICKER.get(chat_id)
    if not state:
        await show_applications(message)
        return

    entries, error = _list_picker(state)
    drive_mode = not state.get("dir")
    pages = max(1, (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(int(state.get("page", 0)), pages - 1))
    state["page"] = page
    chunk = entries[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    lines = ["=== CHOOSE EXECUTABLE ==="]
    if state.get("mode") == "new":
        lines.append("Adding a new application.")
    else:
        lines.append(f"New executable for \"{md_safe(state.get('target'))}\".")
    lines.append("Tap a folder to open it, or a .exe / .lnk file to select it.")
    lines.append("")
    if drive_mode:
        lines.append("This PC. Choose a drive:")
    else:
        lines.append(f"Folder: {md_safe(state.get('dir'))}")
        lines.append(f"Page {page + 1}/{pages}")
    if error:
        lines.append(f"Cannot open this folder: {md_safe(error)}")
    elif not entries and not drive_mode:
        lines.append("Nothing here.")

    rows: List[List[types.InlineKeyboardButton]] = []
    if not drive_mode:
        rows.append([_button(".. Up", "set_pk_u"), _button("Drives", "set_pk_root")])
    for offset, entry in enumerate(chunk):
        index = page * PAGE_SIZE + offset
        name = str(entry.get("name") or "?")
        if entry.get("is_dir"):
            rows.append([_button(_clip(name + "\\", 40), f"set_pk_d_{index}")])
        else:
            rows.append([_button(_clip(name, 40), f"set_pk_f_{index}")])
    if not drive_mode and pages > 1:
        nav = []
        if page > 0:
            nav.append(_button("< Prev", f"set_pk_{page - 1}"))
        if page < pages - 1:
            nav.append(_button("Next >", f"set_pk_{page + 1}"))
        if nav:
            rows.append(nav)
    rows.append([_button("Cancel", "set_pk_x")])
    await _send(message, lines, rows)


async def show_picker_confirm(message: Any, chat_id: int) -> None:
    """Confirm the selected executable before writing it."""
    state = _PICKER.get(chat_id)
    if not state or not state.get("selected"):
        await show_applications(message)
        return

    path = str(state["selected"])
    if state.get("mode") == "new":
        name = state.get("name") or _default_application_name(path)
        lines = [
            "=== ADD APPLICATION ===",
            f"File: {md_safe(path)}",
            f"Name: {md_safe(name)}",
            "",
            "The name is shown on the launcher. It may be changed before adding.",
        ]
        rows = [
            [_button("Add application", "set_pk_ok")],
            [_button("Choose another name", "set_pk_name")],
            [_button("Cancel", "set_pk_x")],
        ]
    else:
        target = str(state.get("target") or "")
        current = load_applications_config().get(target, "")
        lines = [
            "=== CHANGE EXECUTABLE ===",
            f"Application: {md_safe(target)}",
            f"Current: {md_safe(current)}",
            f"New: {md_safe(path)}",
            "",
            "The application keeps its name.",
        ]
        rows = [
            [_button("Save new path", "set_pk_ok")],
            [_button("Cancel", "set_pk_x")],
        ]
    await _send(message, lines, rows)


def _add_picked_application(chat_id: int, custom_name: Optional[str] = None) -> str:
    state = _PICKER.get(chat_id)
    if not state or not state.get("selected"):
        raise ValueError("Choose an executable first.")
    name = (
        custom_name
        or state.get("name")
        or _default_application_name(str(state["selected"]))
    )
    name = str(name).strip()
    add_application(name, str(state["selected"]))
    _PICKER.pop(chat_id, None)
    return name


def _save_picked_path(chat_id: int) -> str:
    state = _PICKER.get(chat_id)
    if not state or not state.get("selected"):
        raise ValueError("Choose an executable first.")
    target = str(state.get("target") or "")
    apps = load_applications_config()
    if target not in apps:
        raise ValueError("That application is no longer configured.")
    apps[target] = str(state["selected"])
    save_applications_config(apps)
    _PICKER.pop(chat_id, None)
    return target


async def remove_application_from_ui(message: Any, index: int) -> None:
    apps = _applications()
    if index < 0 or index >= len(apps):
        await show_applications(message)
        return
    name = apps[index][0]
    removed = remove_application(name)
    lines = [
        f"Removed \"{md_safe(name)}\" from the allowlist."
        if removed
        else "That application was already removed."
    ]
    await _send(message, lines, [[_button("Applications", "set_apps")]])


async def launch_application_from_ui(message: Any, index: int) -> None:
    """Launch one allowlisted application from the settings screen."""
    apps = _applications()
    if index < 0 or index >= len(apps):
        await show_applications(message)
        return
    name = apps[index][0]
    await message.answer(f"Launching {md_safe(name)}...")
    try:
        from . import _launch_app_from_allowlist

        ok, text = await asyncio.to_thread(_launch_app_from_allowlist, name)
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("Launch from settings failed")
        await message.answer(f"Launch failed: {md_safe(e)}")
        return
    if not ok:
        await message.answer(md_safe(text))


# --- gaming profiles -------------------------------------------------------


MEMBERSHIP_MARKS = {
    "required": "[R]",
    "optional": "[O]",
    "none": "[ ]",
}


def _profile_names() -> List[str]:
    """Editable profiles (NORMAL is a read-only status screen)."""
    profiles = load_profiles_config()
    names = [name for name in profiles if name.strip().upper() != "NORMAL"]
    return sorted(names, key=str.casefold)


def _profile_membership(spec: Dict[str, Any], key: str) -> str:
    if key in (spec.get("apps") or []):
        return "required"
    if key in (spec.get("optional") or []):
        return "optional"
    return "none"


def _profile_by_name(name: str) -> Optional[Dict[str, Any]]:
    profiles = load_profiles_config()
    for existing, spec in profiles.items():
        if existing == name:
            return spec
    return None


def _profile_warnings(spec: Dict[str, Any]) -> List[str]:
    """Allowlist keys referenced by a profile but no longer configured."""
    known = {name.casefold() for name, _path in _applications()}
    missing: List[str] = []
    for key in list(spec.get("apps") or []) + list(spec.get("optional") or []):
        if str(key).casefold() not in known and key not in missing:
            missing.append(str(key))
    return missing


async def show_profiles(message: Any) -> None:
    """List gaming profiles."""
    profiles = load_profiles_config()
    names = _profile_names()

    lines = [
        "=== GAMING PROFILES ===",
        "A profile launches a group of applications with one tap.",
        "NORMAL is the status screen and is not editable here.",
        "",
    ]
    if not names:
        lines.append("No profiles yet. Tap New profile below.")
    for name in names:
        spec = profiles.get(name) or {}
        required = len(spec.get("apps") or [])
        optional = len(spec.get("optional") or [])
        lines.append(f"- {md_safe(name)}: {required} required, {optional} optional")

    rows: List[List[types.InlineKeyboardButton]] = [
        [_button(_clip(name, 40), f"set_pfd_{index}")]
        for index, name in enumerate(names)
    ]
    rows.append([_button("New profile", "set_pf_new")])
    rows.append([_button("<< Back", "set_main")])
    await _send(message, lines, rows)


async def show_profile(message: Any, index: int) -> None:
    """One profile: launch, edit name/description, apps, delete."""
    names = _profile_names()
    if index < 0 or index >= len(names):
        await show_profiles(message)
        return

    name = names[index]
    profiles = load_profiles_config()
    spec = profiles.get(name) or {}
    required = [str(key) for key in (spec.get("apps") or [])]
    optional = [str(key) for key in (spec.get("optional") or [])]
    missing = _profile_warnings(spec)

    lines = [
        "=== PROFILE ===",
        f"Name: {md_safe(name)}",
        f"Description: {md_safe(spec.get('description') or '-')}",
        "",
        "Required: " + (", ".join(md_safe(k) for k in required) if required else "none"),
        "Optional: " + (", ".join(md_safe(k) for k in optional) if optional else "none"),
    ]
    if missing:
        lines.append("")
        lines.append(
            "Warning: not in the allowlist: "
            + ", ".join(md_safe(k) for k in missing)
        )
        lines.append("Those names will fail to launch until added again.")

    rows = [
        [_button("Launch profile", f"set_pfl_{index}")],
        [_button("Applications in profile", f"set_pfa_{index}_0")],
        [_button("Rename", f"set_pfn_{index}")],
        [_button("Edit description", f"set_pfe_{index}")],
        [_button("Delete profile", f"set_pfr_{index}")],
        [_button("<< Back", "set_pf")],
    ]
    await _send(message, lines, rows)


async def show_profile_apps(message: Any, index: int, page: int = 0) -> None:
    """Cycle allowlist apps through none -> required -> optional."""
    names = _profile_names()
    if index < 0 or index >= len(names):
        await show_profiles(message)
        return

    name = names[index]
    spec = _profile_by_name(name)
    if spec is None:
        await show_profiles(message)
        return

    apps = _applications()
    pages = max(1, (len(apps) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    chunk = apps[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    lines = [
        "=== PROFILE APPLICATIONS ===",
        f"Profile: {md_safe(name)}",
        "",
        "Tap an app to cycle its role:",
        "[ ] not used  ->  [R] required  ->  [O] optional  ->  [ ]",
        "",
        f"Page {page + 1}/{pages}",
    ]
    if not apps:
        lines.append("")
        lines.append("The allowlist is empty. Add an application first.")

    rows: List[List[types.InlineKeyboardButton]] = []
    for offset, (app_name, _path) in enumerate(chunk):
        app_index = page * PAGE_SIZE + offset
        mark = MEMBERSHIP_MARKS[_profile_membership(spec, app_name)]
        rows.append(
            [_button(f"{mark} {_clip(app_name, 34)}", f"set_pfc_{index}_{app_index}")]
        )
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(_button("< Prev", f"set_pfa_{index}_{page - 1}"))
        if page < pages - 1:
            nav.append(_button("Next >", f"set_pfa_{index}_{page + 1}"))
        if nav:
            rows.append(nav)
    rows.append([_button("<< Back", f"set_pfd_{index}")])
    await _send(message, lines, rows)


async def show_remove_profile(message: Any, index: int) -> None:
    names = _profile_names()
    if index < 0 or index >= len(names):
        await show_profiles(message)
        return
    name = names[index]
    lines = [
        "=== DELETE PROFILE ===",
        f'Delete profile "{md_safe(name)}"?',
        "",
        "Only the profile entry is removed. Applications and files are not deleted.",
    ]
    rows = [
        [_button("Yes, delete", f"set_pfry_{index}")],
        [_button("Cancel", f"set_pfd_{index}")],
    ]
    await _send(message, lines, rows)


def _new_profile(name: str) -> str:
    name = str(name or "").strip()
    if not validate_application_name(name):
        raise ValueError(
            "Use Latin letters, digits, spaces and . - _ + only, e.g. GAMING."
        )
    if name.upper() == "NORMAL":
        raise ValueError("NORMAL is reserved for the status screen.")
    profiles = load_profiles_config()
    for existing in profiles:
        if existing.casefold() == name.casefold():
            raise ValueError(f"\"{existing}\" already exists.")
    profiles[name] = {"description": "", "apps": [], "optional": []}
    save_profiles_config(profiles)
    return name


def _rename_profile(old_name: str, new_name: str) -> str:
    new_name = str(new_name or "").strip()
    if not validate_application_name(new_name):
        raise ValueError(
            "Use Latin letters, digits, spaces and . - _ + only, e.g. GAMING."
        )
    if new_name.upper() == "NORMAL":
        raise ValueError("NORMAL is reserved for the status screen.")
    profiles = load_profiles_config()
    if old_name not in profiles:
        raise ValueError("That profile no longer exists.")
    if new_name == old_name:
        return old_name
    for existing in profiles:
        if existing != old_name and existing.casefold() == new_name.casefold():
            raise ValueError(f"\"{existing}\" already exists; names must be unique.")
    rebuilt: Dict[str, Dict[str, Any]] = {}
    for name, spec in profiles.items():
        rebuilt[new_name if name == old_name else name] = spec
    save_profiles_config(rebuilt)
    return new_name


def _set_profile_description(name: str, text: str) -> None:
    profiles = load_profiles_config()
    spec = profiles.get(name)
    if spec is None:
        raise ValueError("That profile no longer exists.")
    spec["description"] = str(text or "").strip()[:200]
    save_profiles_config(profiles)


def _delete_profile(name: str) -> bool:
    profiles = load_profiles_config()
    if name not in profiles:
        return False
    profiles.pop(name)
    save_profiles_config(profiles)
    return True


def _cycle_profile_app(index: int, app_index: int) -> None:
    """Move one app: unused -> required -> optional -> unused."""
    names = _profile_names()
    apps = _applications()
    if index < 0 or index >= len(names) or app_index < 0 or app_index >= len(apps):
        raise ValueError("That entry is no longer available.")
    profile_name = names[index]
    app_name = apps[app_index][0]

    profiles = load_profiles_config()
    spec = profiles.get(profile_name)
    if spec is None:
        raise ValueError("That profile no longer exists.")
    required = [str(key) for key in (spec.get("apps") or [])]
    optional = [str(key) for key in (spec.get("optional") or [])]
    if app_name in required:
        required.remove(app_name)
        optional.append(app_name)
    elif app_name in optional:
        optional.remove(app_name)
    else:
        required.append(app_name)
    spec["apps"] = required
    spec["optional"] = optional
    save_profiles_config(profiles)


async def launch_profile_from_ui(message: Any, name: str) -> None:
    from gaming_profiles import launch_profile

    await message.answer(f"Launching profile {md_safe(name)}...")
    try:
        started, failed = await asyncio.to_thread(launch_profile, name)
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("Profile launch from settings failed")
        await message.answer(f"Profile launch failed: {md_safe(e)}")
        return

    lines = [f"Profile {md_safe(name)}:"]
    for entry in started:
        lines.append(f"+ {md_safe(entry.get('name'))}")
    for entry in failed:
        note = " (optional)" if entry.get("optional") else ""
        lines.append(
            f"! {md_safe(entry.get('name'))}{note}: {md_safe(entry.get('message'))}"
        )
    if not started and not failed:
        lines.append("Nothing to launch.")
    await _send(message, lines, [[_button("Profiles", "set_pf")]])


# --- backup, restore and main screen ---------------------------------------


def _notification_summary() -> Tuple[int, int]:
    data = load_notification_settings()
    enabled = sum(1 for key in TOGGLE_FIELDS if data.get(key))
    return enabled, len(TOGGLE_FIELDS)


def _allowed_user_count() -> int:
    raw = os.getenv("ALLOWED_USER_IDS", "")
    return len([part for part in raw.split(",") if part.strip()])


async def show_settings(message: Any) -> None:
    """Main settings screen."""
    enabled, total = _notification_summary()
    alerts = load_alert_settings()
    thresholds = alerts.get("thresholds") or {}
    interval = int(alerts.get("interval_seconds", 60))
    apps = load_applications_config()
    profiles = _profile_names()

    lines = [
        "=== SETTINGS ===",
        "Stored on this PC and applied immediately. No restart is needed.",
        "",
        f"Notifications: {enabled} of {total} event types on",
        f"Monitoring: {len(thresholds)} limits, checked every {_fmt_duration(interval)}",
        f"Applications: {len(apps)}",
        f"Profiles: {len(profiles)}",
        "",
        f"Access: {_allowed_user_count()} allowed user(s), set in .env.",
        "Changing access from the bot is not allowed for safety.",
    ]
    rows = [
        [_button("Notifications", "set_notif"), _button("Monitoring alerts", "set_alerts")],
        [_button("Applications", "set_apps"), _button("Gaming profiles", "set_pf")],
        [_button("Backup & restore", "set_bk")],
        [_button("<< Main menu", "menu_main")],
    ]
    await _send(message, lines, rows)


async def show_backup(message: Any) -> None:
    """Backup/restore screen."""
    lines = [
        "=== BACKUP & RESTORE ===",
        "All settings live in the bot database on this PC.",
        "",
        "Export sends a JSON file to this chat. It contains your program",
        "names and local paths, so keep it private.",
        "",
        "Import replaces the settings with a JSON file you send next.",
        "The file is fully checked before anything is changed.",
        "",
        "Reset restores the defaults from the config folder on this PC.",
    ]
    rows = [
        [_button("Export settings", "set_bk_export")],
        [_button("Import settings", "set_bk_import")],
        [_button("Reset to defaults", "set_bk_reset")],
        [_button("<< Back", "set_main")],
    ]
    await _send(message, lines, rows)


async def export_settings(message: Any) -> None:
    """Send the current settings as a JSON document."""
    from settings_store import export_payload

    try:
        payload = export_payload()
        blob = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("Settings export failed")
        await message.answer(f"Export failed: {md_safe(e)}")
        return

    document = BufferedInputFile(blob, filename="pc_remote_settings.json")
    await message.answer_document(
        document,
        caption="PC Remote settings backup. It contains local paths - keep it private.",
    )


async def arm_import(chat_id: int, message: Any) -> None:
    """Wait for the next uploaded JSON document to be imported."""
    _PENDING_IMPORT[chat_id] = time.time() + IMPORT_TTL_SECONDS
    lines = [
        "=== IMPORT SETTINGS ===",
        "Send the JSON backup now as a file.",
        "",
        "It replaces notifications, monitoring, applications and profiles",
        "with the file contents. Nothing changes until the file is valid.",
        "",
        f"This import is armed for {IMPORT_TTL_SECONDS // 60} minutes.",
    ]
    rows = [[_button("Cancel", "set_bk")], [_button("<< Settings", "set_main")]]
    await _send(message, lines, rows)


def import_armed(chat_id: int) -> bool:
    """True when the chat is waiting for a settings backup."""
    _prune_pending()
    return chat_id in _PENDING_IMPORT


def take_import_pending(chat_id: int) -> bool:
    """Consume the armed import state; True when it was still valid."""
    expiry = _PENDING_IMPORT.pop(chat_id, None)
    if expiry is None:
        return False
    return time.time() <= expiry


def import_settings(chat_id: int, raw: bytes) -> Dict[str, Any]:
    """Validate and apply an uploaded settings file.

    The caller should check :func:`import_armed` first so ordinary JSON
    uploads still go to the uploads folder.

    Raises:
        ValueError: when the import was not armed or the file is invalid.
    """
    from settings_store import import_payload

    if not take_import_pending(chat_id):
        raise ValueError("Import is not armed. Open Settings -> Backup first.")
    try:
        text = raw.decode("utf-8-sig")
        payload = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        raise ValueError("The file is not valid JSON.") from None
    return import_payload(payload)


async def show_reset_confirm(message: Any, chat_id: int) -> None:
    """Confirm restoring factory defaults."""
    _PENDING_RESET[chat_id] = time.time() + RESET_TTL_SECONDS
    lines = [
        "=== RESET SETTINGS ===",
        "Replace notifications, monitoring, applications and profiles with",
        "the defaults from this PC's config folder?",
        "",
        "Applications or profiles that exist only in the bot database will",
        "be lost. Uploaded files and the programs themselves are not touched.",
    ]
    rows = [
        [_button("Yes, reset", "set_bk_reset_yes")],
        [_button("Cancel", "set_bk")],
    ]
    await _send(message, lines, rows)


async def reset_settings(message: Any, chat_id: int) -> None:
    """Reset every section, then show the new summary."""
    from settings_store import reset_defaults

    expiry = _PENDING_RESET.pop(chat_id, None)
    if expiry is None or time.time() > expiry:
        await show_backup(message)
        return

    try:
        created = reset_defaults()
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("Settings reset failed")
        await message.answer(f"Reset failed: {md_safe(e)}")
        return

    lines = ["Settings were reset to defaults."]
    if created:
        lines.append("Re-created: " + ", ".join(sorted(created)) + ".")
    lines.append("")
    lines.append("If a config file is missing, that section is now empty.")
    await _send(message, lines, [[_button("Settings", "set_main")]])


def _prune_pending() -> None:
    """Drop expired one-shot states."""
    now = time.time()
    for store in (_PENDING_IMPORT, _PENDING_RESET):
        for key, expiry in list(store.items()):
            if expiry < now:
                store.pop(key, None)
    for key, entry in list(_PENDING_TEXT.items()):
        if float(entry.get("expiry", 0)) < now:
            _PENDING_TEXT.pop(key, None)


# --- text prompts ----------------------------------------------------------


def _arm_text_prompt(chat_id: int, kind: str, **payload: Any) -> None:
    """Remember the next free-text message for this chat."""
    entry: Dict[str, Any] = {"kind": kind, "expiry": time.time() + TEXT_TTL_SECONDS}
    entry.update(payload)
    _PENDING_TEXT[chat_id] = entry


async def _ask_value(
    message: Any,
    chat_id: int,
    kind: str,
    lines: List[str],
    back: str = "set_main",
    **payload: Any,
) -> None:
    """Show instructions and arm a one-shot text prompt."""
    _arm_text_prompt(chat_id, kind, **payload)
    body = list(lines)
    body.append("")
    body.append(
        "Send the value in one message. "
        f"This prompt expires in {TEXT_TTL_SECONDS // 60} min."
    )
    await _send(message, body, [[_button("Cancel", back)]])


async def _apply_text(
    message: Any,
    chat_id: int,
    entry: Dict[str, Any],
    text: str,
) -> None:
    """Validate and store one free-text answer, then show the next screen."""
    kind = entry.get("kind")

    if kind == "notif_field":
        field = str(entry.get("field") or "")
        if field not in NOTIFICATION_FIELDS:
            raise ValueError("That setting is no longer available.")
        value = (
            _parse_minutes(text)
            if field.endswith("_minutes")
            else _parse_number(text)
        )
        _store_notif_value(field, value, True)
        await show_notification_field(message, field)
        return

    if kind == "quiet_time":
        which = str(entry.get("which") or "")
        if which not in ("start", "end"):
            raise ValueError("That setting is no longer available.")
        _set_quiet_time(which, _parse_hhmm(text))
        await show_notifications(message)
        return

    if kind == "alert_threshold":
        key = str(entry.get("key") or "")
        if key not in ALERT_FIELDS:
            raise ValueError("That limit is no longer available.")
        _store_alert_threshold(key, _parse_number(text), True)
        await show_alert_threshold(message, key)
        return

    if kind == "alert_cooldown":
        key = str(entry.get("key") or "")
        if key not in ALERT_FIELDS:
            raise ValueError("That cooldown is no longer available.")
        _store_alert_cooldown(key, _parse_minutes(text), True)
        await show_alert_cooldown(message, key)
        return

    if kind == "alert_interval":
        _store_alert_interval(_parse_duration(text))
        await show_alert_interval(message)
        return

    if kind == "app_rename":
        old_name = str(entry.get("name") or "")
        new_name = _rename_application(old_name, text)
        index = _application_index(new_name)
        if index >= 0:
            await show_application_detail(message, index)
        else:
            await show_applications(message)
        return

    if kind == "picker_name":
        value = str(text or "").strip()
        if not validate_application_name(value):
            raise ValueError(
                "Use letters, digits, spaces and . - _ + only, e.g. My Game."
            )
        state = _PICKER.get(chat_id)
        if not state:
            raise ValueError("The file picker expired. Open Applications again.")
        state["name"] = value
        await show_picker_confirm(message, chat_id)
        return

    if kind == "profile_new":
        name = _new_profile(text)
        names = _profile_names()
        index = names.index(name) if name in names else -1
        if index >= 0:
            await show_profile(message, index)
        else:
            await show_profiles(message)
        return

    if kind == "profile_rename":
        old_name = str(entry.get("name") or "")
        new_name = _rename_profile(old_name, text)
        names = _profile_names()
        index = names.index(new_name) if new_name in names else -1
        if index >= 0:
            await show_profile(message, index)
        else:
            await show_profiles(message)
        return

    if kind == "profile_description":
        name = str(entry.get("name") or "")
        description = "" if text.casefold() in {"-", "none", "clear"} else text
        _set_profile_description(name, description)
        names = _profile_names()
        index = names.index(name) if name in names else -1
        if index >= 0:
            await show_profile(message, index)
        else:
            await show_profiles(message)
        return

    raise ValueError("That prompt is no longer valid. Open Settings again.")


async def handle_text(message: Any, state: Any = None) -> bool:
    """Consume a reply for the active text prompt.

    Returns ``True`` when the message belonged to a settings prompt, so the
    caller can tell an ordinary message from an answer.
    """
    user = getattr(message, "from_user", None)
    if user is not None and not is_user_authorized(user.id):
        return False

    chat_id = _chat_id(message)
    _prune_pending()
    entry = _PENDING_TEXT.pop(chat_id, None)
    if entry is None:
        return False

    text = str(getattr(message, "text", "") or "").strip()
    if not text:
        _PENDING_TEXT[chat_id] = entry
        return True

    try:
        await _apply_text(message, chat_id, entry, text)
    except ValueError as e:
        entry["expiry"] = time.time() + TEXT_TTL_SECONDS
        _PENDING_TEXT[chat_id] = entry
        await message.answer(f"Not changed: {md_safe(e)}")
    except Exception:
        logger.exception("Settings text input failed")
        entry["expiry"] = time.time() + TEXT_TTL_SECONDS
        _PENDING_TEXT[chat_id] = entry
        await message.answer("Not changed: unexpected error.")
    return True


# --- callback router -------------------------------------------------------

_PROFILE_APPS_RE = re.compile(r"^set_pfa_(\d+)_(\d+)$")
_PROFILE_CYCLE_RE = re.compile(r"^set_pfc_(\d+)_(\d+)$")


async def _route_callback(
    callback: Any,
    message: Any,
    chat_id: int,
    data: str,
) -> bool:
    """Run one settings action; ``False`` means unknown callback data."""
    # --- top level -----------------------------------------------------
    if data == "set_main":
        _PICKER.pop(chat_id, None)
        await show_settings(message)
        return True
    if data == "set_notif":
        await show_notifications(message)
        return True
    if data == "set_alerts":
        await show_alerts(message)
        return True
    if data == "set_apps":
        await show_applications(message)
        return True
    if data == "set_pf":
        await show_profiles(message)
        return True
    if data == "set_bk":
        await show_backup(message)
        return True

    # --- notifications -------------------------------------------------
    if data == "set_nq_toggle":
        _toggle_quiet_hours()
        await show_notifications(message)
        return True
    if data in ("set_nq_start", "set_nq_end"):
        which = data.rsplit("_", 1)[-1]
        quiet = load_notification_settings().get("quiet_hours") or {}
        await _ask_value(
            message,
            chat_id,
            "quiet_time",
            [
                "=== QUIET HOURS ===",
                "During quiet hours only critical battery alerts are sent.",
                "",
                f"Current start: {_fmt_hhmm(quiet.get('start'))}",
                f"Current end: {_fmt_hhmm(quiet.get('end'))}",
                "",
                f"Send the new {which} time as HH:MM, for example 23:00.",
            ],
            back="set_notif",
            which=which,
        )
        return True
    if data.startswith("set_nt_"):
        key = "notify_" + data[len("set_nt_") :]
        if key not in TOGGLE_FIELDS:
            return False
        _toggle_notification(key)
        await show_notifications(message)
        return True
    if data.startswith("set_nbx_"):
        field = data[len("set_nbx_") :]
        if field not in NOTIFICATION_FIELDS:
            return False
        spec = NOTIFICATION_FIELDS[field]
        current = load_notification_settings()
        low, high = _notif_limits(field, current)
        await _ask_value(
            message,
            chat_id,
            "notif_field",
            [
                f"=== {spec['title']} ===",
                str(spec["help"]),
                "",
                f"Current: {_notif_value_text(field, _notif_value(field, current))}",
                f"Allowed: {low}-{high} {spec['unit']}".strip(),
                "Send the exact value as a number.",
            ],
            back=f"set_nb_{field}",
            field=field,
        )
        return True
    if data.startswith("set_nb_"):
        tail = data[len("set_nb_") :]
        direction = 0
        if tail.endswith("_d"):
            direction, tail = -1, tail[:-2]
        elif tail.endswith("_u"):
            direction, tail = 1, tail[:-2]
        if tail not in NOTIFICATION_FIELDS:
            return False
        if direction == 0:
            await show_notification_field(message, tail)
            return True
        spec = NOTIFICATION_FIELDS[tail]
        current = float(_notif_value(tail, load_notification_settings()))
        _store_notif_value(tail, current + direction * float(spec["step"]), False)
        await show_notification_field(message, tail)
        return True

    # --- monitoring alerts ---------------------------------------------
    if data == "set_acooldowns":
        await show_alert_cooldowns(message)
        return True
    if data == "set_ai":
        await show_alert_interval(message)
        return True
    if data == "set_aix":
        data_now = load_alert_settings()
        await _ask_value(
            message,
            chat_id,
            "alert_interval",
            [
                "=== CHECK INTERVAL ===",
                "How often the monitor samples this PC.",
                "",
                f"Current: {_fmt_duration(data_now.get('interval_seconds', 60))}",
                f"Allowed: {MIN_INTERVAL_SECONDS} s - 24 h.",
                "Send seconds or use min/h, for example 60 or 5 min.",
            ],
            back="set_ai",
        )
        return True
    interval = _suffix_index(data, "set_ai_")
    if interval is not None:
        _store_alert_interval(interval)
        await show_alert_interval(message)
        return True
    if data.startswith("set_atx_"):
        key = data[len("set_atx_") :]
        if key not in ALERT_FIELDS:
            return False
        spec = ALERT_FIELDS[key]
        current = load_alert_settings()
        await _ask_value(
            message,
            chat_id,
            "alert_threshold",
            [
                f"=== {spec['title']} ===",
                f"Send an alert when {spec['long']} goes above the limit.",
                "",
                "Current: "
                + _alert_value_text(key, (current.get("thresholds") or {}).get(key)),
                f"Allowed: {spec['low']}-{spec['high']} {spec['unit']}".strip(),
                "Send the exact value as a number.",
            ],
            back=f"set_at_{key}",
            key=key,
        )
        return True
    if data.startswith("set_at_"):
        tail = data[len("set_at_") :]
        direction = 0
        if tail.endswith("_d"):
            direction, tail = -1, tail[:-2]
        elif tail.endswith("_u"):
            direction, tail = 1, tail[:-2]
        if tail not in ALERT_FIELDS:
            return False
        if direction == 0:
            await show_alert_threshold(message, tail)
            return True
        spec = ALERT_FIELDS[tail]
        current = load_alert_settings()
        raw = (current.get("thresholds") or {}).get(tail)
        base = float(raw) if isinstance(raw, (int, float)) else float(spec["low"])
        _store_alert_threshold(tail, base + direction * float(spec["step"]), False)
        await show_alert_threshold(message, tail)
        return True
    if data.startswith("set_acx_"):
        key = data[len("set_acx_") :]
        if key not in ALERT_FIELDS:
            return False
        raw = (load_alert_settings().get("cooldowns") or {}).get(key)
        await _ask_value(
            message,
            chat_id,
            "alert_cooldown",
            [
                f"=== {ALERT_FIELDS[key]['title']} COOLDOWN ===",
                "Send the number of minutes between repeated alerts.",
                'Send "never" to alert once per breach.',
                "",
                f"Current: {_fmt_minutes(raw)}",
                "Allowed: 0-1440 minutes, or never.",
            ],
            back=f"set_ac_{key}",
            key=key,
        )
        return True
    if data.startswith("set_ac_"):
        tail = data[len("set_ac_") :]
        if tail.endswith("_never"):
            key = tail[: -len("_never")]
            if key not in ALERT_FIELDS:
                return False
            _store_alert_cooldown(key, None, False)
            await show_alert_cooldown(message, key)
            return True
        direction = 0
        if tail.endswith("_d"):
            direction, tail = -1, tail[:-2]
        elif tail.endswith("_u"):
            direction, tail = 1, tail[:-2]
        if tail not in ALERT_FIELDS:
            return False
        if direction == 0:
            await show_alert_cooldown(message, tail)
            return True
        raw = (load_alert_settings().get("cooldowns") or {}).get(tail)
        try:
            minutes = float(raw) / 60.0
        except (TypeError, ValueError):
            minutes = math.inf
        if not math.isfinite(minutes):
            new_minutes = 5.0
        else:
            new_minutes = max(0.0, minutes + direction)
        _store_alert_cooldown(tail, new_minutes, False)
        await show_alert_cooldown(message, tail)
        return True

    # --- applications --------------------------------------------------
    page = _suffix_index(data, "set_apps_")
    if page is not None:
        await show_applications(message, page)
        return True
    index = _suffix_index(data, "set_apd_")
    if index is not None:
        await show_application_detail(message, index)
        return True
    index = _suffix_index(data, "set_ap_l_")
    if index is not None:
        await launch_application_from_ui(message, index)
        await show_application_detail(message, index)
        return True
    index = _suffix_index(data, "set_ap_n_")
    if index is not None:
        apps = _applications()
        if index < 0 or index >= len(apps):
            await show_applications(message)
            return True
        name = apps[index][0]
        await _ask_value(
            message,
            chat_id,
            "app_rename",
            [
                "=== RENAME APPLICATION ===",
                f"Current name: {md_safe(name)}",
                "",
                "Send the new name.",
                "Allowed: letters, digits, spaces and . - _ +",
                "Gaming profiles that use this name are updated automatically.",
            ],
            back=f"set_apd_{index}",
            name=name,
        )
        return True
    index = _suffix_index(data, "set_ap_p_")
    if index is not None:
        apps = _applications()
        if index < 0 or index >= len(apps):
            await show_applications(message)
            return True
        name, path = apps[index]
        directory: Optional[Path] = None
        try:
            parent = Path(path).parent
            if parent.is_dir():
                directory = parent
        except OSError:
            directory = None
        _start_picker(chat_id, "change", target=name, directory=directory)
        await show_picker(message, chat_id)
        return True
    index = _suffix_index(data, "set_ap_r_")
    if index is not None:
        await show_remove_application(message, index)
        return True
    index = _suffix_index(data, "set_ap_ry_")
    if index is not None:
        await remove_application_from_ui(message, index)
        return True

    # --- executable picker ---------------------------------------------
    if data == "set_add":
        _start_picker(chat_id, "new")
        await show_picker(message, chat_id)
        return True
    if data == "set_pk_u":
        state = _PICKER.get(chat_id)
        if not state or not state.get("dir"):
            await show_applications(message)
            return True
        current_dir = Path(str(state["dir"]))
        parent = current_dir.parent
        state["dir"] = str(parent) if str(parent) != str(current_dir) else None
        state["page"] = 0
        await show_picker(message, chat_id)
        return True
    if data == "set_pk_root":
        state = _PICKER.get(chat_id)
        if state:
            state["dir"] = None
            state["page"] = 0
        await show_picker(message, chat_id)
        return True
    if data == "set_pk_x":
        _PICKER.pop(chat_id, None)
        await show_applications(message)
        return True
    if data == "set_pk_name":
        state = _PICKER.get(chat_id)
        if not state:
            await show_applications(message)
            return True
        current_name = state.get("name") or _default_application_name(
            str(state.get("selected") or "")
        )
        await _ask_value(
            message,
            chat_id,
            "picker_name",
            [
                "=== APPLICATION NAME ===",
                f"Current name: {md_safe(current_name)}",
                "",
                "Send the name to show on the launcher.",
                "Allowed: letters, digits, spaces and . - _ +",
            ],
            back="set_pk_back",
        )
        return True
    if data == "set_pk_back":
        await show_picker_confirm(message, chat_id)
        return True
    if data == "set_pk_ok":
        state = _PICKER.get(chat_id)
        if not state or not state.get("selected"):
            await show_applications(message)
            return True
        if state.get("mode") == "new":
            name = _add_picked_application(chat_id)
        else:
            name = _save_picked_path(chat_id)
        index = _application_index(name)
        if index >= 0:
            await show_application_detail(message, index)
        else:
            await show_applications(message)
        return True
    index = _suffix_index(data, "set_pk_d_")
    if index is not None:
        state = _PICKER.get(chat_id)
        entry = _resolve_picker_entry(chat_id, index)
        if state is None or entry is None:
            await show_picker(message, chat_id)
            return True
        state["dir"] = str(entry.get("path"))
        state["page"] = 0
        await show_picker(message, chat_id)
        return True
    index = _suffix_index(data, "set_pk_f_")
    if index is not None:
        state = _PICKER.get(chat_id)
        entry = _resolve_picker_entry(chat_id, index)
        if state is None or entry is None:
            await show_picker(message, chat_id)
            return True
        state["selected"] = str(entry.get("path"))
        if state.get("mode") == "new":
            state["name"] = _default_application_name(state["selected"])
        await show_picker_confirm(message, chat_id)
        return True
    page = _suffix_index(data, "set_pk_")
    if page is not None:
        state = _PICKER.get(chat_id)
        if not state:
            await show_applications(message)
            return True
        state["page"] = page
        await show_picker(message, chat_id)
        return True

    # --- gaming profiles ------------------------------------------------
    if data == "set_pf_new":
        await _ask_value(
            message,
            chat_id,
            "profile_new",
            [
                "=== NEW PROFILE ===",
                "Send the profile name, for example GAMING.",
                "Allowed: letters, digits, spaces and . - _ +",
                "",
                "Add applications to it on the next screen.",
            ],
            back="set_pf",
        )
        return True
    index = _suffix_index(data, "set_pfry_")
    if index is not None:
        names = _profile_names()
        if index < 0 or index >= len(names):
            await show_profiles(message)
            return True
        name = names[index]
        removed = _delete_profile(name)
        lines = [
            f"Profile \"{md_safe(name)}\" deleted."
            if removed
            else "That profile was already deleted.",
            "",
            "Applications and files were not touched.",
        ]
        await _send(message, lines, [[_button("Profiles", "set_pf")]])
        return True
    index = _suffix_index(data, "set_pfd_")
    if index is not None:
        await show_profile(message, index)
        return True
    index = _suffix_index(data, "set_pfl_")
    if index is not None:
        names = _profile_names()
        if index < 0 or index >= len(names):
            await show_profiles(message)
            return True
        await launch_profile_from_ui(message, names[index])
        return True
    index = _suffix_index(data, "set_pfn_")
    if index is not None:
        names = _profile_names()
        if index < 0 or index >= len(names):
            await show_profiles(message)
            return True
        name = names[index]
        await _ask_value(
            message,
            chat_id,
            "profile_rename",
            [
                "=== RENAME PROFILE ===",
                f"Current name: {md_safe(name)}",
                "",
                "Send the new name.",
                "Allowed: letters, digits, spaces and . - _ +",
            ],
            back=f"set_pfd_{index}",
            name=name,
        )
        return True
    index = _suffix_index(data, "set_pfe_")
    if index is not None:
        names = _profile_names()
        if index < 0 or index >= len(names):
            await show_profiles(message)
            return True
        name = names[index]
        spec = _profile_by_name(name) or {}
        await _ask_value(
            message,
            chat_id,
            "profile_description",
            [
                "=== PROFILE DESCRIPTION ===",
                f"Current: {md_safe(spec.get('description') or '-')}",
                "",
                "Send a short description (up to 200 characters).",
                'Send "-" to clear it.',
            ],
            back=f"set_pfd_{index}",
            name=name,
        )
        return True
    index = _suffix_index(data, "set_pfr_")
    if index is not None:
        await show_remove_profile(message, index)
        return True
    match = _PROFILE_APPS_RE.fullmatch(data)
    if match:
        index, page = int(match.group(1)), int(match.group(2))
        await show_profile_apps(message, index, page)
        return True
    match = _PROFILE_CYCLE_RE.fullmatch(data)
    if match:
        index, app_index = int(match.group(1)), int(match.group(2))
        _cycle_profile_app(index, app_index)
        await show_profile_apps(message, index)
        return True

    # --- backup and restore --------------------------------------------
    if data == "set_bk_export":
        await export_settings(message)
        return True
    if data == "set_bk_import":
        await arm_import(chat_id, message)
        return True
    if data == "set_bk_reset":
        await show_reset_confirm(message, chat_id)
        return True
    if data == "set_bk_reset_yes":
        await reset_settings(message, chat_id)
        return True

    return False


async def handle_callback(
    callback: Any,
    data: Optional[str] = None,
) -> None:
    """Route every ``set_*`` callback to its screen or action."""
    payload = str(data if data is not None else (getattr(callback, "data", "") or ""))
    message = getattr(callback, "message", None)
    if message is None:
        return

    chat_id = _chat_id(message) or _chat_id(callback)
    _prune_pending()
    # Any button press abandons a waiting text prompt and any armed import,
    # so a forgotten "Import settings" can never claim a later JSON upload.
    _PENDING_TEXT.pop(chat_id, None)
    _PENDING_IMPORT.pop(chat_id, None)

    try:
        handled = await _route_callback(callback, message, chat_id, payload)
    except ValueError as e:
        await message.answer(f"Not changed: {md_safe(e)}")
        handled = True
    except Exception:
        logger.exception("Settings action failed: %s", payload)
        await message.answer("Settings action failed. Nothing was changed.")
        handled = True

    if not handled:
        try:
            await callback.answer(
                "This settings screen has expired. Open Settings again.",
                show_alert=True,
            )
        except Exception:
            logger.debug("Could not answer stale settings callback", exc_info=True)