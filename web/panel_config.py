"""Configuration for the local web panel.

The panel settings live outside ``.env`` on purpose: ``.env`` only holds the
Telegram credentials. The file is created with safe defaults on first use and
is ignored by git (``data/`` is untracked).

Defaults:
    host  0.0.0.0   - reachable from the LAN, not just this PC
    port  8090
    enabled true

Binding to ``0.0.0.0`` still requires the access password, and the panel is
never exposed to the internet by this application (no port forwarding).
"""

import ipaddress
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_NAME = "web_panel.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "host": "0.0.0.0",
    "port": 8090,
}

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class PanelConfigError(ValueError):
    """Raised when a panel configuration value is not acceptable."""


def default_config_path(data_dir: Path) -> Path:
    """Return the panel config path for a data directory."""
    return Path(data_dir) / DEFAULT_CONFIG_NAME


def _clean_host(value: Any) -> str:
    host = str(value if value is not None else "").strip()
    if not host:
        raise PanelConfigError("Host must not be empty")
    if host in LOOPBACK_HOSTS:
        return "127.0.0.1" if host == "localhost" else host
    if host in {"*", "all"}:
        return "0.0.0.0"
    try:
        ipaddress.ip_address(host)
    except ValueError as e:
        raise PanelConfigError(f"Invalid host address: {host!r}") from e
    return host


def _clean_port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as e:
        raise PanelConfigError("Port must be a number") from e
    if not 1 <= port <= 65535:
        raise PanelConfigError("Port must be between 1 and 65535")
    return port


def _clean_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def sanitize_config(raw: Any) -> Dict[str, Any]:
    """Return a validated copy of ``raw`` merged over the defaults."""
    merged = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        if "host" in raw:
            merged["host"] = _clean_host(raw.get("host"))
        if "port" in raw:
            merged["port"] = _clean_port(raw.get("port"))
        if "enabled" in raw:
            merged["enabled"] = _clean_enabled(raw.get("enabled"))
    return merged


def load_panel_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the panel config, creating it with defaults when missing."""
    target = Path(path) if path is not None else default_config_path(
        Path(__file__).resolve().parent.parent / "data"
    )
    if not target.exists():
        config = dict(DEFAULT_CONFIG)
        try:
            save_panel_config(config, target)
            logger.info("Created web panel config at %s", target)
        except OSError as e:
            logger.warning("Could not create %s: %s", target, e)
        return config

    try:
        with open(target, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Cannot read %s (%s); using defaults", target, e)
        return dict(DEFAULT_CONFIG)

    try:
        return sanitize_config(raw)
    except PanelConfigError as e:
        logger.warning("Invalid panel config in %s (%s); using defaults", target, e)
        return dict(DEFAULT_CONFIG)


def save_panel_config(config: Dict[str, Any], path: Optional[Path] = None) -> Dict[str, Any]:
    """Validate and atomically write the panel config."""
    cleaned = sanitize_config(config)
    target = Path(path) if path is not None else default_config_path(
        Path(__file__).resolve().parent.parent / "data"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(cleaned, handle, ensure_ascii=False, indent=2)
    os.replace(temp, target)
    return cleaned


def panel_urls(host: str, port: int) -> Dict[str, str]:
    """Return the local and LAN URLs a user can open in a browser."""
    if host in {"0.0.0.0", "::"}:
        return {"local": f"http://127.0.0.1:{port}/", "lan": f"http://<LAN-IP>:{port}/"}
    return {"local": f"http://{host}:{port}/", "lan": f"http://{host}:{port}/"}