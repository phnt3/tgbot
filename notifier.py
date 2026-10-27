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
        battery_warning: int = DEFAULT_BATTERY_WARNING,
        battery_critical: int = DEFAULT_BATTERY_CRITICAL,
        app_cooldown_seconds: float = DEFAULT_APP_COOLDOWN_SECONDS,
        crash_cooldown_seconds: float = DEFAULT_CRASH_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        apps = watched_apps if watched_apps is not None else load_watched_apps()
        self.watched_apps: Set[str] = {
            str(name).casefold() for name in apps if str(name or "").strip()
        }
        self.battery_warning = int(battery_warning)
        self.battery_critical = int(battery_critical)
        self.app_cooldown_seconds = float(app_cooldown_seconds)
        self.crash_cooldown_seconds = float(crash_cooldown_seconds)
        self._clock = clock

        # ``None`` means "no baseline yet": the first poll only records state
        # so restarting the bot never produces fake login/app-exit events.
        self._known_sessions: Optional[Set[Tuple[str, str]]] = None
        self._battery_stage = 0
        self._running_apps: Optional[Set[str]] = None
        self._app_notified_at: Dict[str, float] = {}
        self._crash_seen = False
        self._crash_notified_at: Optional[float] = None

    # -- public API -----------------------------------------------------

    def poll(self) -> List[Dict[str, Any]]:
        """Check every source once and return new events.

        Returns:
            List of alert dicts with ``level``, ``category`` and ``message``.
        """
        events: List[Dict[str, Any]] = []

        events.extend(self._check_sessions())
        events.extend(self._check_battery())

        processes: Optional[Set[str]]
        try:
            processes = _running_executables()
        except Exception as e:
            logger.debug("Failed to enumerate processes: %s", e)
            processes = None
        if processes is not None:
            events.extend(self._check_applications(processes))
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