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


def load_applications_config(config_path: Optional[Path] = None) -> Dict[str, str]:
    """Load application allowlist from JSON configuration file.

    Args:
        config_path: Path to the applications JSON config file.
                     Defaults to config/applications.json, falling back to
                     config/applications.example.json when the user config
                     does not exist yet.

    Returns:
        Dictionary mapping application names to executable paths.
    """
    if config_path is None:
        config_path = DEFAULT_APPLICATIONS_CONFIG
        if not config_path.exists():
            config_path = FALLBACK_APPLICATIONS_CONFIG

    if not config_path.exists():
        logger.warning(f"Applications config not found at {config_path}")
        return {}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)

        # Validate and filter entries
        valid_apps = {}
        for name, path in config_data.items():
            if validate_application_name(name) and validate_path_safe(path):
                valid_apps[name] = path
            else:
                logger.warning(
                    f"Skipping invalid application config: name='{name}', path='{path}'"
                )

        return valid_apps

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse applications config: {e}")
        return {}
    except Exception as e:
        logger.error(f"Unexpected error loading applications config: {e}")
        return {}


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