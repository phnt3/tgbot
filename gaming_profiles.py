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


def _clean_profiles(raw: Any) -> Dict[str, Dict[str, Any]]:
    """Normalize a raw profile mapping, dropping malformed entries."""
    profiles: Dict[str, Dict[str, Any]] = {}
    if not isinstance(raw, dict):
        logger.warning("Profiles settings must be an object, got %s", type(raw).__name__)
        return profiles
    for name, spec in raw.items():
        name = str(name or "").strip()
        if not name or not isinstance(spec, dict):
            logger.warning("Skipping malformed profile entry: %s", name)
            continue
        apps = spec.get("apps") or []
        optional = spec.get("optional") or []
        if not isinstance(apps, list) or not isinstance(optional, list):
            logger.warning("Skipping profile %s: apps must be lists", name)
            continue
        profiles[name] = {
            "description": str(spec.get("description", "")),
            "apps": [str(a) for a in apps],
            "optional": [str(a) for a in optional],
        }
    return profiles


def _stored_profiles() -> Optional[Dict[str, Dict[str, Any]]]:
    """Return the database-backed profiles, or None when not configured yet."""
    try:
        from settings_store import MISSING, PROFILES, get_setting

        stored = get_setting(PROFILES, MISSING)
    except Exception as e:  # pragma: no cover - database unavailable
        logger.warning("Could not read profiles from settings: %s", e)
        return None
    if stored is MISSING:
        return None
    return _clean_profiles(stored)


def load_profiles_config(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Load gaming profiles.

    Args:
        path: Optional explicit JSON file used for tests and seeding. When it
            is None the database setting is preferred, falling back to
            config/profiles.json and then the bundled example.

    Returns:
        Mapping of profile name -> profile definition.
    """
    if path is None:
        stored = _stored_profiles()
        if stored is not None:
            return stored

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

    return _clean_profiles(raw)


def seed_profiles() -> Dict[str, Dict[str, Any]]:
    """Read profiles from the JSON files, for first-run seeding."""
    for candidate in (PROFILES_CONFIG_PATH, PROFILES_EXAMPLE_PATH):
        if candidate.is_file():
            return load_profiles_config(candidate)
    return {}


def save_profiles_config(profiles: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Validate and store profiles in the bot database.

    Raises:
        ValueError: when a profile name or app key is invalid.
    """
    from security import validate_application_name

    cleaned: Dict[str, Dict[str, Any]] = {}
    for raw_name, spec in (profiles or {}).items():
        name = str(raw_name or "").strip()
        if not validate_application_name(name):
            raise ValueError(f"Invalid profile name: {name!r}")
        if not isinstance(spec, dict):
            raise ValueError(f"Profile {name!r} must be an object")
        apps = spec.get("apps") or []
        optional = spec.get("optional") or []
        if not isinstance(apps, list) or not isinstance(optional, list):
            raise ValueError(f"Profile {name!r} app lists must be arrays")
        for key in list(apps) + list(optional):
            if not validate_application_name(str(key)):
                raise ValueError(f"Profile {name!r} has an invalid app key: {key!r}")
        cleaned[name] = {
            "description": str(spec.get("description", "")),
            "apps": [str(a) for a in apps],
            "optional": [str(a) for a in optional],
        }

    from settings_store import PROFILES, set_setting

    set_setting(PROFILES, cleaned)
    return cleaned


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