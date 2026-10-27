"""Database-backed bot settings.

All user-editable configuration lives in the ``settings`` table of the bot
SQLite database, one JSON document per section:

    applications   name -> executable path allowlist
    profiles       gaming profile definitions
    alerts         monitoring thresholds/cooldowns/interval
    notifications  PC event and quiet-hours settings

The JSON files under ``config/`` are only a first-run seed (and an import/export
format).  ``ensure_defaults()`` is called once at startup, so an existing
installation is upgraded from its files without losing anything, and every
later change made through the bot survives a restart.

Connections are short-lived and each public function is serialized with a
module lock; SQLite runs in WAL mode on the main database, so the bot and the
background monitor can read settings while the Telegram handler writes them.
"""

import json
import logging
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Section keys (also used as UI identifiers and callback payloads).
APPLICATIONS = "applications"
PROFILES = "profiles"
ALERTS = "alerts"
NOTIFICATIONS = "notifications"

SECTIONS = (APPLICATIONS, PROFILES, ALERTS, NOTIFICATIONS)

DEFAULT_DB_NAME = "pc_remote.db"

EXPORT_FORMAT = "pc-remote-settings"
EXPORT_VERSION = 1

# ``None`` is a valid stored value, so a private sentinel marks "no row".
MISSING = object()

_lock = threading.RLock()


# --- storage ---------------------------------------------------------------


def default_db_path() -> Path:
    """Return the live bot database path (``data/pc_remote.db``)."""
    from config import config

    return Path(config["DATA_DIR"]) / DEFAULT_DB_NAME


def _connect(db_path: Optional[Path]) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings ("
        " key TEXT PRIMARY KEY,"
        " value TEXT NOT NULL,"
        " updated_at REAL NOT NULL)"
    )
    return conn


def get_setting(
    key: str,
    default: Any = None,
    db_path: Optional[Path] = None,
) -> Any:
    """Read one settings section.

    Args:
        key: Section name.
        default: Value returned when the section does not exist or cannot be
            read.  Pass ``MISSING`` to distinguish "absent" from ``None``.
        db_path: Optional database override (tests).

    Returns:
        The decoded JSON value, or ``default``.
    """
    try:
        with _lock:
            conn = _connect(db_path)
            try:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key = ?", (key,)
                ).fetchone()
            finally:
                conn.close()
    except sqlite3.Error as e:
        logger.warning("Could not read setting %s: %s", key, e)
        return default

    if row is None:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError) as e:
        logger.warning("Ignoring corrupt setting %s: %s", key, e)
        return default


def _portable_json(value: Any) -> Any:
    """Replace non-standard JSON floats (Infinity/NaN) with ``null``."""
    if isinstance(value, float):
        if math.isinf(value) or math.isnan(value):
            return None
        return value
    if isinstance(value, dict):
        return {str(k): _portable_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_portable_json(v) for v in value]
    return value


def set_setting(key: str, value: Any, db_path: Optional[Path] = None) -> Any:
    """Store one settings section as JSON.

    Raises:
        sqlite3.Error: when the database cannot be written, so callers can
            report the failure to the user.
    """
    payload = json.dumps(_portable_json(value), ensure_ascii=False, sort_keys=True)
    with _lock:
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, payload, time.time()),
            )
            conn.commit()
        finally:
            conn.close()
    return value


def delete_setting(key: str, db_path: Optional[Path] = None) -> None:
    """Remove one settings section (it will be re-seeded on next startup)."""
    with _lock:
        conn = _connect(db_path)
        try:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            conn.commit()
        finally:
            conn.close()


def all_settings(db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Return every stored section as a dict (missing sections omitted)."""
    try:
        with _lock:
            conn = _connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT key, value FROM settings ORDER BY key"
                ).fetchall()
            finally:
                conn.close()
    except sqlite3.Error as e:
        logger.warning("Could not read settings: %s", e)
        return {}

    result: Dict[str, Any] = {}
    for key, value in rows:
        try:
            result[key] = json.loads(value)
        except (TypeError, ValueError):
            logger.warning("Ignoring corrupt setting %s", key)
    return result


def get_or_seed(
    key: str,
    seed: Callable[[], Any],
    db_path: Optional[Path] = None,
) -> Any:
    """Return a section, creating it from ``seed()`` when it is missing."""
    value = get_setting(key, MISSING, db_path)
    if value is not MISSING:
        return value
    value = seed()
    try:
        set_setting(key, value, db_path)
    except sqlite3.Error as e:
        logger.warning("Could not store default setting %s: %s", key, e)
    return value


# --- defaults --------------------------------------------------------------


def _seed_applications() -> Dict[str, str]:
    from application_manager import seed_applications

    return seed_applications()


def _seed_profiles() -> Dict[str, Dict[str, Any]]:
    from gaming_profiles import seed_profiles

    return seed_profiles()


def _seed_alerts() -> Dict[str, Any]:
    from alerts import seed_alert_settings

    return seed_alert_settings()


def _seed_notifications() -> Dict[str, Any]:
    from notifier import normalize_notification_settings

    return normalize_notification_settings({})


_SEEDS: Dict[str, Callable[[], Any]] = {
    APPLICATIONS: _seed_applications,
    PROFILES: _seed_profiles,
    ALERTS: _seed_alerts,
    NOTIFICATIONS: _seed_notifications,
}


def ensure_defaults(db_path: Optional[Path] = None) -> Dict[str, bool]:
    """Create missing sections from the config files / built-in defaults.

    Returns:
        Mapping section -> True when it was created by this call.
    """
    created: Dict[str, bool] = {}
    for key in SECTIONS:
        try:
            exists = get_setting(key, MISSING, db_path) is not MISSING
        except Exception:  # pragma: no cover - defensive
            exists = False
        if exists:
            continue
        try:
            value = _SEEDS[key]()
            set_setting(key, value, db_path)
            created[key] = True
            logger.info("Seeded default setting: %s", key)
        except Exception as e:
            logger.error("Failed to seed setting %s: %s", key, e)
    return created


def reset_defaults(db_path: Optional[Path] = None) -> Dict[str, bool]:
    """Drop all sections and re-create them from files/defaults."""
    for key in SECTIONS:
        try:
            delete_setting(key, db_path)
        except sqlite3.Error as e:
            logger.warning("Could not reset setting %s: %s", key, e)
    return ensure_defaults(db_path)


# --- export / import -------------------------------------------------------


def export_payload(db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Build a portable JSON payload with every settings section."""
    stored = all_settings(db_path)
    settings: Dict[str, Any] = {}
    for key in SECTIONS:
        if key in stored:
            settings[key] = stored[key]
        else:
            try:
                settings[key] = _SEEDS[key]()
            except Exception:  # pragma: no cover - defensive
                settings[key] = {}
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "settings": _portable_json(settings),
    }


def _validate_applications(raw: Any) -> Dict[str, str]:
    from security import validate_application_name, validate_path_safe

    if not isinstance(raw, dict):
        raise ValueError("applications must be an object")
    cleaned: Dict[str, str] = {}
    for name, path in raw.items():
        name = str(name or "").strip()
        path = str(path or "").strip()
        if not validate_application_name(name):
            raise ValueError(f"invalid application name: {name!r}")
        if not validate_path_safe(path):
            raise ValueError(f"unsafe path for application {name!r}")
        cleaned[name] = path
    return cleaned


def _validate_profiles(raw: Any) -> Dict[str, Dict[str, Any]]:
    from security import validate_application_name

    if not isinstance(raw, dict):
        raise ValueError("profiles must be an object")
    cleaned: Dict[str, Dict[str, Any]] = {}
    for name, spec in raw.items():
        name = str(name or "").strip()
        if not validate_application_name(name):
            raise ValueError(f"invalid profile name: {name!r}")
        if not isinstance(spec, dict):
            raise ValueError(f"profile {name!r} must be an object")
        apps = spec.get("apps") or []
        optional = spec.get("optional") or []
        if not isinstance(apps, list) or not isinstance(optional, list):
            raise ValueError(f"profile {name!r} apps must be lists")
        for key in apps + optional:
            if not validate_application_name(str(key)):
                raise ValueError(f"profile {name!r} has an invalid app key: {key!r}")
        cleaned[name] = {
            "description": str(spec.get("description", "")),
            "apps": [str(a).strip() for a in apps],
            "optional": [str(a).strip() for a in optional],
        }
    return cleaned


def _validate_alerts(raw: Any) -> Dict[str, Any]:
    from alerts import normalize_alert_settings

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("alerts must be an object")
    return normalize_alert_settings(raw)


def _validate_notifications(raw: Any) -> Dict[str, Any]:
    from notifier import normalize_notification_settings

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("notifications must be an object")
    return normalize_notification_settings(raw)


def validate_import(payload: Any) -> Dict[str, Any]:
    """Validate an exported settings payload.

    Args:
        payload: Already-decoded JSON object.

    Returns:
        Mapping section -> cleaned value.  Only sections present in the
        payload are included.

    Raises:
        ValueError: with a user-readable reason when the payload is invalid.
    """
    if not isinstance(payload, dict):
        raise ValueError("file must contain a JSON object")
    sections = payload.get("settings")
    if sections is None:
        # Accept a bare {"applications": ...} payload too, for hand-written
        # imports.
        sections = {
            key: payload[key] for key in SECTIONS if key in payload
        }
    if not isinstance(sections, dict) or not sections:
        raise ValueError("no settings sections found")

    validators = {
        APPLICATIONS: _validate_applications,
        PROFILES: _validate_profiles,
        ALERTS: _validate_alerts,
        NOTIFICATIONS: _validate_notifications,
    }
    cleaned: Dict[str, Any] = {}
    for key, validate in validators.items():
        if key in sections:
            cleaned[key] = validate(sections[key])
    return cleaned


def import_payload(
    payload: Any,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Validate and store an exported settings payload.

    All sections are validated before anything is written, so a bad file can
    never leave the bot half-configured.

    Returns:
        Mapping section -> stored value.

    Raises:
        ValueError: when validation fails (nothing is written).
    """
    cleaned = validate_import(payload)
    for key, value in cleaned.items():
        set_setting(key, value, db_path)
    return cleaned