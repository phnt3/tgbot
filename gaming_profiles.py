"""Gaming profiles module for PC Remote.

Profiles are defined in config/profiles.json and reference application keys
declared in the applications allowlist (config/applications.example.json).

A profile launch:
- uses application_manager.launch_application for each configured key,
  so allowlist validation, duplicate detection, and SQLite logging apply
- launches optional apps after required ones, reporting them separately
- never kills background processes or modifies Windows settings

NORMAL profile is special: it displays current system status only.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from application_manager import load_applications_config, launch_application

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent / "config"
PROFILES_CONFIG_PATH = CONFIG_DIR / "profiles.json"
PROFILES_EXAMPLE_PATH = CONFIG_DIR / "profiles.example.json"


def load_profiles_config(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Load gaming profiles from JSON configuration.

    Falls back to the bundled example when no user config exists, so the bot
    works out of the box and the user can later edit profiles.json.

    Args:
        path: Optional explicit path used for tests.

    Returns:
        Mapping of profile name -> profile definition.
    """
    target = path if path is not None else PROFILES_CONFIG_PATH
    if not target.exists():
        logger.info("profiles.json not found, using bundled example profiles")
        target = PROFILES_EXAMPLE_PATH

    try:
        with open(target, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error("Failed to load profiles config: %s", e)
        return {}

    profiles: Dict[str, Dict[str, Any]] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            logger.warning("Skipping malformed profile entry: %s", name)
            continue
        profiles[name] = {
            "description": str(spec.get("description", "")),
            "apps": [str(a) for a in spec.get("apps", [])],
            "optional": [str(a) for a in spec.get("optional", [])],
        }
    return profiles


def get_profile(profile_key: str) -> Optional[Dict[str, Any]]:
    """Fetch a single profile by name (case-insensitive).

    Args:
        profile_key: Profile name, e.g. "gaming".

    Returns:
        Profile definition dict or None.
    """
    profiles = load_profiles_config()
    wanted = profile_key.strip().upper()
    for name, spec in profiles.items():
        if name.upper() == wanted:
            # Preserve canonical profile name (e.g. "GAMING") in a dict field
            return {"name": name, **spec}
    return None


def launch_profile(
    profile_key: str,
    db: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Launch the applications configured in a profile.

    Required apps are attempted first, then optional ones. Each launch goes
    through application_manager.launch_application, which validates the
    allowlist, skips already-running instances, and records events to SQLite.

    Args:
        profile_key: Profile name (e.g. "GAMING", "VR", "WORK").
        db: Optional database used by launch_application for event logging.

    Returns:
        Tuple of (started, failed) lists; each item is a result dict with
        keys: name, ok, message, optional.
    """
    profile = get_profile(profile_key)
    if profile is None:
        return [], [{"name": profile_key, "ok": False,
                     "message": f"Unknown profile: {profile_key}", "optional": False}]

    if profile_key.strip().upper() == "NORMAL":
        # NORMAL is read-only: caller handles it by showing system status.
        return [], []

    started: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    for optional, keys in ((False, profile["apps"]), (True, profile["optional"])):
        for key in keys:
            ok, message = launch_application(key, db=db)
            entry = {"name": key, "ok": ok, "message": message, "optional": optional}
            if ok:
                # An "already running" info message still counts as started
                started.append(entry)
            else:
                failed.append(entry)

    return started, failed