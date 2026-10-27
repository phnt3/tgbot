"""PC -> Telegram event notifications.

The notifier is polled by the background monitor and returns alert dicts in the
same shape as the system alert manager, so the existing delivery and logging
code is reused.

Tracked events:
- interactive user login (new local or RDP session),
- low / critical battery, re-armed after charging,
- a configured application exiting,
- Windows Error Reporting (WerFault) activity, a good crash proxy.

The class is deliberately stateful and side-effect free: ``poll()`` never sends
anything, it only returns events. This keeps it trivial to unit test.
"""

import logging
import math
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import psutil

logger = logging.getLogger(__name__)

DEFAULT_BATTERY_WARNING = 20
DEFAULT_BATTERY_CRITICAL = 10
BATTERY_REARM_MARGIN = 5
DEFAULT_APP_COOLDOWN_SECONDS = 300
DEFAULT_CRASH_COOLDOWN_SECONDS = 600
CRASH_PROCESS_NAMES = {"werfault.exe"}

# Editable in the bot; stored as one JSON document in the settings table.
DEFAULT_NOTIFICATION_SETTINGS: Dict[str, Any] = {
    "battery_warning": DEFAULT_BATTERY_WARNING,
    "battery_critical": DEFAULT_BATTERY_CRITICAL,
    "app_cooldown_minutes": DEFAULT_APP_COOLDOWN_SECONDS / 60,
    "crash_cooldown_minutes": DEFAULT_CRASH_COOLDOWN_SECONDS / 60,
    "notify_login": True,
    "notify_battery": True,
    "notify_apps": True,
    "notify_crashes": True,
    "quiet_hours": {"enabled": False, "start": "23:00", "end": "08:00"},
}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return default


def _parse_hhmm(value: Any) -> Optional[int]:
    match = _TIME_RE.match(str(value or "").strip())
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def normalize_notification_settings(raw: Any) -> Dict[str, Any]:
    """Merge a raw mapping over the notification defaults.

    Invalid values fall back to the defaults instead of raising, so a bad
    import can never disable event notifications by accident.
    """
    defaults = DEFAULT_NOTIFICATION_SETTINGS
    settings: Dict[str, Any] = {
        "battery_warning": defaults["battery_warning"],
        "battery_critical": defaults["battery_critical"],
        "app_cooldown_minutes": defaults["app_cooldown_minutes"],
        "crash_cooldown_minutes": defaults["crash_cooldown_minutes"],
        "notify_login": defaults["notify_login"],
        "notify_battery": defaults["notify_battery"],
        "notify_apps": defaults["notify_apps"],
        "notify_crashes": defaults["notify_crashes"],
        "quiet_hours": dict(defaults["quiet_hours"]),
    }
    if not isinstance(raw, dict):
        return settings

    warning = _clamp_int(raw.get("battery_warning"), 5, 100, settings["battery_warning"])
    settings["battery_warning"] = warning
    settings["battery_critical"] = _clamp_int(
        raw.get("battery_critical"), 1, warning, settings["battery_critical"]
    )

    for key in ("app_cooldown_minutes", "crash_cooldown_minutes"):
        try:
            minutes = float(raw.get(key))
        except (TypeError, ValueError):
            continue
        if math.isfinite(minutes):
            settings[key] = max(0.0, min(1440.0, minutes))

    for key in ("notify_login", "notify_battery", "notify_apps", "notify_crashes"):
        settings[key] = _coerce_bool(raw.get(key), settings[key])

    quiet = raw.get("quiet_hours")
    if isinstance(quiet, dict):
        start = quiet.get("start")
        end = quiet.get("end")
        settings["quiet_hours"] = {
            "enabled": _coerce_bool(quiet.get("enabled"), False),
            "start": str(start) if _parse_hhmm(start) is not None else defaults["quiet_hours"]["start"],
            "end": str(end) if _parse_hhmm(end) is not None else defaults["quiet_hours"]["end"],
        }
    return settings


def load_notification_settings() -> Dict[str, Any]:
    """Return the stored notification settings, or the defaults."""
    try:
        from settings_store import MISSING, NOTIFICATIONS, get_setting

        stored = get_setting(NOTIFICATIONS, MISSING)
    except Exception as e:  # pragma: no cover - database unavailable
        logger.debug("Could not read notification settings: %s", e)
        return normalize_notification_settings({})
    if stored is MISSING:
        return normalize_notification_settings({})
    return normalize_notification_settings(stored)


def save_notification_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and store notification settings in the bot database."""
    normalized = normalize_notification_settings(settings)

    from settings_store import NOTIFICATIONS, set_setting

    set_setting(NOTIFICATIONS, normalized)
    return normalized


def quiet_hours_active(
    settings: Optional[Dict[str, Any]] = None,
    now: Any = None,
) -> bool:
    """Return True when local time is inside the configured quiet window."""
    if settings is None:
        settings = load_notification_settings()
    quiet = settings.get("quiet_hours") if isinstance(settings, dict) else None
    if not isinstance(quiet, dict) or not _coerce_bool(quiet.get("enabled"), False):
        return False

    start = _parse_hhmm(quiet.get("start"))
    end = _parse_hhmm(quiet.get("end"))
    if start is None or end is None or start == end:
        return False

    if now is None:
        hour, minute = time.localtime().tm_hour, time.localtime().tm_min
    elif hasattr(now, "hour"):
        hour, minute = int(now.hour), int(now.minute)
    else:
        hour, minute = int(getattr(now, "tm_hour", 0)), int(getattr(now, "tm_min", 0))

    minutes = hour * 60 + minute
    if start < end:
        return start <= minutes < end
    # Window crosses midnight, e.g. 23:00 -> 08:00.
    return minutes >= start or minutes < end


def load_watched_apps() -> Set[str]:
    """Return lower-case executable names of configured applications."""
    try:
        from application_manager import load_applications_config

        configured = load_applications_config()
    except Exception as e:  # pragma: no cover - defensive
        logger.error("Failed to load watched applications: %s", e)
        return set()

    names: Set[str] = set()
    for exe_path in configured.values():
        name = str(exe_path).replace("/", "\\").rsplit("\\", 1)[-1]
        if name.lower().endswith(".exe"):
            names.add(name.casefold())
    return names


def _session_key(user: Any) -> Optional[Tuple[str, str]]:
    """Build a stable key for one logged-in session."""
    name = str(getattr(user, "name", "") or "").strip().casefold()
    if not name:
        return None
    host = str(getattr(user, "host", "") or "").strip().casefold()
    return name, host


def _running_executables() -> Set[str]:
    """Return lower-case executable names of all currently running processes."""
    names: Set[str] = set()
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info.get("name")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name:
            names.add(str(name).casefold())
    return names


class Notifier:
    """Collect PC events that are interesting as Telegram notifications."""

    def __init__(
        self,
        watched_apps: Optional[Iterable[str]] = None,
        battery_warning: Optional[int] = None,
        battery_critical: Optional[int] = None,
        app_cooldown_seconds: Optional[float] = None,
        crash_cooldown_seconds: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
        live_settings: bool = True,
    ) -> None:
        """Initialize the notifier.

        When no explicit overrides are passed, settings are read from the bot
        database and refreshed on every poll, so changes made in Telegram apply
        without a restart. Passing any override pins that value and disables
        live reloading (used by tests and by callers with custom settings).
        """
        explicit = any(
            value is not None
            for value in (
                watched_apps,
                battery_warning,
                battery_critical,
                app_cooldown_seconds,
                crash_cooldown_seconds,
            )
        )
        self._live = bool(live_settings) and not explicit
        settings = (
            load_notification_settings()
            if self._live
            else normalize_notification_settings({})
        )

        apps = watched_apps if watched_apps is not None else load_watched_apps()
        self.watched_apps: Set[str] = {
            str(name).casefold() for name in apps if str(name or "").strip()
        }
        self._clock = clock
        self.apply_settings(settings, refresh_apps=False)

        # Explicit values win over stored settings (pinned mode).
        if battery_warning is not None:
            self.battery_warning = int(battery_warning)
        if battery_critical is not None:
            self.battery_critical = int(battery_critical)
        if app_cooldown_seconds is not None:
            self.app_cooldown_seconds = float(app_cooldown_seconds)
        if crash_cooldown_seconds is not None:
            self.crash_cooldown_seconds = float(crash_cooldown_seconds)

        # ``None`` means "no baseline yet": the first poll only records state
        # so restarting the bot never produces fake login/app-exit events.
        self._known_sessions: Optional[Set[Tuple[str, str]]] = None
        self._battery_stage = 0
        self._running_apps: Optional[Set[str]] = None
        self._app_notified_at: Dict[str, float] = {}
        self._crash_seen = False
        self._crash_notified_at: Optional[float] = None

    def apply_settings(
        self,
        settings: Dict[str, Any],
        refresh_apps: bool = False,
    ) -> None:
        """Apply notification settings without losing polling state."""
        normalized = normalize_notification_settings(settings)
        self.notification_settings = normalized
        self.battery_warning = int(normalized["battery_warning"])
        self.battery_critical = int(normalized["battery_critical"])
        self.app_cooldown_seconds = float(normalized["app_cooldown_minutes"]) * 60
        self.crash_cooldown_seconds = float(normalized["crash_cooldown_minutes"]) * 60
        self.notify_login = bool(normalized["notify_login"])
        self.notify_battery = bool(normalized["notify_battery"])
        self.notify_apps = bool(normalized["notify_apps"])
        self.notify_crashes = bool(normalized["notify_crashes"])

        if refresh_apps:
            try:
                self.watched_apps = {
                    str(name).casefold()
                    for name in load_watched_apps()
                    if str(name or "").strip()
                }
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("Could not refresh watched applications: %s", e)

    # -- public API -----------------------------------------------------

    def poll(self) -> List[Dict[str, Any]]:
        """Check every source once and return new events.

        Returns:
            List of alert dicts with ``level``, ``category`` and ``message``.
        """
        if self._live:
            try:
                self.apply_settings(load_notification_settings(), refresh_apps=True)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("Could not refresh notification settings: %s", e)

        events: List[Dict[str, Any]] = []

        if self.notify_login:
            events.extend(self._check_sessions())
        if self.notify_battery:
            events.extend(self._check_battery())

        if self.notify_apps or self.notify_crashes:
            processes: Optional[Set[str]]
            try:
                processes = _running_executables()
            except Exception as e:
                logger.debug("Failed to enumerate processes: %s", e)
                processes = None
            if processes is not None:
                if self.notify_apps:
                    events.extend(self._check_applications(processes))
                if self.notify_crashes:
                    events.extend(self._check_crashes(processes))

        return events

    # -- checks ---------------------------------------------------------

    def _check_sessions(self) -> List[Dict[str, Any]]:
        try:
            sessions = psutil.users()
        except Exception as e:
            logger.debug("Failed to read user sessions: %s", e)
            return []

        current: Set[Tuple[str, str]] = set()
        details: Dict[Tuple[str, str], Any] = {}
        for user in sessions:
            key = _session_key(user)
            if key is None:
                continue
            current.add(key)
            details[key] = user

        if self._known_sessions is None:
            self._known_sessions = current
            return []

        new_sessions = current - self._known_sessions
        self._known_sessions = current

        events: List[Dict[str, Any]] = []
        for key in sorted(new_sessions):
            name, host = key
            user = details.get(key)
            message = f"User '{name}' logged in"
            if host:
                message += f" from {host}"
            started = getattr(user, "started", None) if user else None
            if started:
                message += " (session started at " + time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(float(started))
                ) + ")"
            events.append(
                {"level": "info", "category": "login", "message": message + "."}
            )
        return events

    def _check_battery(self) -> List[Dict[str, Any]]:
        try:
            battery = psutil.sensors_battery()
        except Exception as e:
            logger.debug("Failed to read battery: %s", e)
            return []
        if battery is None:
            return []

        percent = float(battery.percent)
        plugged = bool(battery.power_plugged)

        if plugged or percent > self.battery_warning + BATTERY_REARM_MARGIN:
            self._battery_stage = 0
            return []

        if percent <= self.battery_critical:
            stage = 2
        elif percent <= self.battery_warning:
            stage = 1
        else:
            stage = 0

        if stage <= self._battery_stage:
            return []
        self._battery_stage = stage

        level = "critical" if stage == 2 else "warning"
        state = "not charging" if not plugged else "charging"
        return [
            {
                "level": level,
                "category": "battery",
                "message": f"Battery is at {percent:.0f}% ({state}).",
            }
        ]

    def _check_applications(self, processes: Set[str]) -> List[Dict[str, Any]]:
        if not self.watched_apps:
            return []

        if self._running_apps is None:
            self._running_apps = set(processes)
            return []

        stopped = (self._running_apps & self.watched_apps) - processes
        self._running_apps = set(processes)

        now = self._clock()
        events: List[Dict[str, Any]] = []
        for exe in sorted(stopped):
            last = self._app_notified_at.get(exe)
            if last is not None and now - last < self.app_cooldown_seconds:
                continue
            self._app_notified_at[exe] = now
            events.append(
                {
                    "level": "warning",
                    "category": "application",
                    "message": f"'{exe}' is no longer running "
                    "(closed or crashed).",
                }
            )
        return events

    def _check_crashes(self, processes: Set[str]) -> List[Dict[str, Any]]:
        crashed = bool(processes & CRASH_PROCESS_NAMES)
        if not crashed:
            self._crash_seen = False
            return []

        if self._crash_seen:
            return []
        self._crash_seen = True

        now = self._clock()
        if (
            self._crash_notified_at is not None
            and now - self._crash_notified_at < self.crash_cooldown_seconds
        ):
            return []
        self._crash_notified_at = now
        return [
            {
                "level": "warning",
                "category": "crash",
                "message": "Windows Error Reporting (WerFault) is active: "
                "an application may have crashed.",
            }
        ]