"""Network and VPN status collection for PC Remote.

Collects active interfaces, IPv4 addresses, Wi-Fi details (SSID/signal) and
detects VPN adapters by their well-known names. Public IP lookup is optional
and always time-limited so a blocked network cannot stall the bot.
"""

import logging
import re
import socket
import subprocess
import urllib.request
from typing import Any, Dict, List, Optional

import psutil

logger = logging.getLogger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

VPN_MARKERS = (
    "vpn", "wireguard", "wintun", "openvpn", "tap-", "tap ", "tun",
    "zerotier", "tailscale", "hamachi", "nordlynx", "proton",
)


def is_vpn_interface(name: str) -> bool:
    """Heuristically decide whether an interface name belongs to a VPN."""
    lowered = (name or "").casefold()
    return any(marker in lowered for marker in VPN_MARKERS)


def _wifi_fields(text: str) -> Dict[str, str]:
    """Parse ``netsh wlan show interfaces`` output into a field mapping."""
    fields: Dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        key = key.strip().casefold()
        value = value.strip()
        if not value:
            continue
        if key in {"state", "ssid", "bssid", "signal", "authentication",
                   "radio type", "channel"}:
            fields[key] = value
    return fields


def parse_wifi_output(text: str) -> Optional[Dict[str, Any]]:
    """Return Wi-Fi details from netsh output, or None when not connected."""
    fields = _wifi_fields(text)
    state = fields.get("state", "").casefold()
    ssid = fields.get("ssid")
    if state != "connected" or not ssid:
        return None
    return {
        "ssid": ssid,
        "signal": fields.get("signal", "N/A"),
        "channel": fields.get("channel", "N/A"),
        "authentication": fields.get("authentication", "N/A"),
    }


def get_wifi_info() -> Optional[Dict[str, Any]]:
    """Query the current Wi-Fi connection (Windows only)."""
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=5,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("Wi-Fi query failed: %s", e)
        return None
    return parse_wifi_output(result.stdout)


def get_public_ip(timeout: float = 3.0) -> Optional[str]:
    """Return the current public IPv4 address, or None when unavailable."""
    try:
        with urllib.request.urlopen(
            "https://api.ipify.org", timeout=timeout
        ) as response:
            text = response.read(64).decode("ascii", "ignore").strip()
        return text or None
    except Exception as e:
        logger.debug("Public IP lookup failed: %s", e)
        return None


def get_interfaces() -> List[Dict[str, Any]]:
    """Return every network interface with its addresses and state."""
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except Exception as e:
        logger.error("Failed to read network interfaces: %s", e)
        return []

    result: List[Dict[str, Any]] = []
    for name, addresses in addrs.items():
        stat = stats.get(name)
        ipv4: List[str] = []
        mac = ""
        for addr in addresses:
            if addr.family == socket.AF_INET and addr.address:
                ipv4.append(addr.address)
            elif addr.family == psutil.AF_LINK and addr.address and not mac:
                mac = addr.address
        if not ipv4 and not mac:
            continue
        result.append(
            {
                "name": name,
                "ipv4": ipv4,
                "mac": mac,
                "is_up": bool(stat.isup) if stat else False,
                "speed_mbps": int(stat.speed) if stat else 0,
                "vpn": is_vpn_interface(name),
            }
        )
    result.sort(key=lambda item: (not item["is_up"], item["name"].casefold()))
    return result


def collect_network_status(include_public_ip: bool = True) -> Dict[str, Any]:
    """Collect a complete network/VPN snapshot for display."""
    interfaces = get_interfaces()
    active = [item for item in interfaces if item["is_up"] and item["ipv4"]]
    vpn_active = [item for item in active if item["vpn"]]
    return {
        "hostname": socket.gethostname(),
        "interfaces": interfaces,
        "active": active,
        "vpn_active": vpn_active,
        "wifi": get_wifi_info(),
        "public_ip": get_public_ip() if include_public_ip else None,
    }