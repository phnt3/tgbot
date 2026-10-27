"""Application launcher module for PC Remote.

Provides safe application launching from a configurable allowlist.

Security:
- Only launches applications from the configured allowlist
- Validates paths for safety
- Requires explicit user confirmation
- Records launch events in SQLite
"""

import json
import logging
import os
import psutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from config import config
from database import Database
from security import validate_application_name, validate_path_safe

logger = logging.getLogger(__name__)


# User-editable config first; the bundled example is the fallback
CONFIG_DIR = Path(config["BASE_DIR"]) / "config"
DEFAULT_APPLICATIONS_CONFIG = CONFIG_DIR / "applications.json"
FALLBACK_APPLICATIONS_CONFIG = CONFIG_DIR / "applications.example.json"


def _clean_applications(raw: Any) -> Dict[str, str]:
    """Validate a raw name -> path mapping, dropping unsafe entries."""
    valid: Dict[str, str] = {}
    if not isinstance(raw, dict):
        logger.warning("Application settings must be an object, got %s", type(raw).__name__)
        return valid
    for name, path in raw.items():
        name = str(name or "").strip()
        path = str(path or "").strip()
        if validate_application_name(name) and validate_path_safe(path):
            valid[name] = path
        else:
            logger.warning(
                "Skipping invalid application config: name=%r path=%r", name, path
            )
    return valid


def _stored_applications() -> Optional[Dict[str, str]]:
    """Return the database-backed allowlist, or None when it is missing.

    ``None`` means "not configured yet": startup seeding has not run, so the
    JSON config file is still authoritative.
    """
    try:
        from settings_store import APPLICATIONS, MISSING, get_setting

        stored = get_setting(APPLICATIONS, MISSING)
    except Exception as e:  # pragma: no cover - database unavailable
        logger.warning("Could not read applications from settings: %s", e)
        return None
    if stored is MISSING:
        return None
    return _clean_applications(stored)


def load_applications_config(config_path: Optional[Path] = None) -> Dict[str, str]:
    """Load application allowlist.

    Args:
        config_path: Explicit JSON file used for tests and seeding. When it is
            None the database setting is preferred, falling back to
            config/applications.json and then the bundled example.

    Returns:
        Dictionary mapping application names to executable paths.
    """
    if config_path is None:
        stored = _stored_applications()
        if stored is not None:
            return stored
        config_path = DEFAULT_APPLICATIONS_CONFIG
        if not config_path.exists():
            config_path = FALLBACK_APPLICATIONS_CONFIG

    if not config_path.exists():
        logger.warning(f"Applications config not found at {config_path}")
        return {}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)
        return _clean_applications(config_data)

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse applications config: {e}")
        return {}
    except Exception as e:
        logger.error(f"Unexpected error loading applications config: {e}")
        return {}


def seed_applications() -> Dict[str, str]:
    """Read the allowlist from the JSON files, for first-run seeding.

    Never reads the database, so it is safe to call from startup and tests.
    """
    for candidate in (DEFAULT_APPLICATIONS_CONFIG, FALLBACK_APPLICATIONS_CONFIG):
        if candidate.is_file():
            return load_applications_config(candidate)
    return {}


def save_applications_config(applications: Dict[str, str]) -> Dict[str, str]:
    """Validate and store the allowlist in the bot database.

    Raises:
        ValueError: when a name or path is unsafe; nothing is written.
    """
    cleaned: Dict[str, str] = {}
    for raw_name, raw_path in (applications or {}).items():
        name = str(raw_name or "").strip()
        path = str(raw_path or "").strip()
        if not validate_application_name(name):
            raise ValueError(f"Invalid application name: {name!r}")
        if not validate_path_safe(path):
            raise ValueError(f"Unsafe executable path for {name!r}")
        cleaned[name] = path

    from settings_store import APPLICATIONS, set_setting

    set_setting(APPLICATIONS, cleaned)
    return cleaned


def add_application(name: str, path: str, replace: bool = False) -> Dict[str, str]:
    """Add one application to the allowlist.

    Raises:
        ValueError: on an invalid entry or a duplicate name (unless ``replace``).
    """
    name = str(name or "").strip()
    path = str(path or "").strip()
    if not validate_application_name(name):
        raise ValueError(f"Invalid application name: {name!r}")
    if not validate_path_safe(path):
        raise ValueError("That path contains unsupported characters.")

    current = load_applications_config()
    for existing in current:
        if existing == name:
            if not replace:
                raise ValueError(f"{existing!r} is already configured.")
        elif existing.casefold() == name.casefold():
            raise ValueError(
                f"{existing!r} already exists; names must be unique (case-insensitive)."
            )
    current[name] = path
    return save_applications_config(current)


def remove_application(name: str) -> bool:
    """Remove one application from the allowlist.

    Returns:
        True when the entry existed and was removed.
    """
    current = load_applications_config()
    if name not in current:
        return False
    del current[name]
    save_applications_config(current)

    # Profiles referencing the removed key are left untouched: launch_profile
    # reports such keys as failed instead of silently changing the profile.
    return True


def find_application_path(
    application_name: str,
    custom_config: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Find the executable path for a configured application.

    Args:
        application_name: Name of the application to find.
        custom_config: Optional custom configuration override.

    Returns:
        Executable path if found, None otherwise.
    """
    # Check custom config first
    config_data = custom_config or load_applications_config()

    # Exact match
    if application_name in config_data:
        path = config_data[application_name]
        if os.path.exists(path):
            return path

    # Case-insensitive match
    name_lower = application_name.lower()
    for app_name, path in config_data.items():
        if app_name.lower() == name_lower:
            if os.path.exists(path):
                return path

    logger.info(f"Application '{application_name}' not found in config or path not found")
    return None


def launch_application(
    application_name: str,
    custom_config: Optional[Dict[str, str]] = None,
    db: Optional[Database] = None,
) -> Tuple[bool, str]:
    """Launch a configured application.

    Safety checks:
    - Validates the application name
    - Checks that the path exists
    - Does not execute arbitrary paths from user input
    - Records the launch event

    Args:
        application_name: Name of the application to launch (must be in allowlist).
        custom_config: Optional custom application config override.
        db: Optional Database instance for recording. If None, creates a new one.

    Returns:
        Tuple of (success, message) where success is a boolean and message
        is a human-readable result string.
    """
    # Validate application name
    if not application_name or not validate_application_name(application_name):
        return False, f"❌ Invalid application name: '{application_name}'"

    # Find the executable path
    exe_path = find_application_path(application_name, custom_config)

    if not exe_path:
        return False, f"❌ Application '{application_name}' not configured or not found."

    # Validate path safety
    if not validate_path_safe(exe_path):
        return False, f"❌ Application path failed safety validation: '{exe_path}'"

    # Check if process is already running
    try:
        process_name = os.path.basename(exe_path).lower()
        for proc in psutil.process_iter(["name"]):
            if proc.info["name"] and proc.info["name"].lower() == process_name:
                return True, f"ℹ️ '{application_name}' is already running."

    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pass

    # Launch the application
    try:
        logger.info(f"Launching application: {application_name} at {exe_path}")

        # .lnk shortcuts must go through the shell (Popen cannot execute them);
        # .exe files get a proper working directory so apps find their resources
        if exe_path.lower().endswith(".lnk"):
            logger.info("Shortcut launch via os.startfile: %s", exe_path)
            os.startfile(exe_path)  # noqa: S606 - path comes from the allowlist config
        else:
            subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path) or None)

        time.sleep(0.5)

        # Record launch event in database
        if db is not None:
            try:
                db.insert_app_launch(application_name, success=1)
            except Exception as e:
                logger.warning(f"Failed to record app launch in DB: {e}")

        return True, f"✅ Successfully launched '{application_name}'."

    except FileNotFoundError:
        error_msg = f"❌ Application executable not found: '{exe_path}'"
        logger.error(error_msg)
        if db is not None:
            try:
                db.insert_app_launch(application_name, success=0, error_message="Executable not found")
            except Exception:
                pass
        return False, error_msg

    except PermissionError:
        error_msg = f"❌ Permission denied launching '{application_name}'."
        logger.error(error_msg)
        if db is not None:
            try:
                db.insert_app_launch(application_name, success=0, error_message="Permission denied")
            except Exception:
                pass
        return False, error_msg

    except Exception as e:
        error_msg = f"❌ Failed to launch '{application_name}': {str(e)}"
        logger.error(error_msg)
        if db is not None:
            try:
                db.insert_app_launch(application_name, success=0, error_message=str(e))
            except Exception:
                pass
        return False, error_msg


def launch_gaming_profile(
    profile_name: str = "GAMING",
    db: Optional[Database] = None,
) -> Tuple[bool, str]:
    """Launch a gaming profile - starts configured gaming applications.

    Args:
        profile_name: Name of the profile to launch (GAMING, VR, WORK, NORMAL).
        db: Optional Database instance for recording.

    Returns:
        Tuple of (success, message).
    """
    # Delegate to the dedicated profiles module: real per-profile app lists
    # come from config/profiles.json, through the normal allowlist launcher
    from gaming_profiles import launch_profile

    started, failed = launch_profile(profile_name, db=db)

    names_started = ", ".join(entry["name"] for entry in started) if started else "none"
    msg = f"Profile '{profile_name}' initiated. Started: {names_started}"
    if failed:
        msg += ". Failed: " + ", ".join(entry["name"] for entry in failed)

    return True, msg