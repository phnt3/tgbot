"""Network and VPN status collection for PC Remote.

Collects active interfaces, IPv4 addresses, Wi-Fi details (SSID/signal) and
detects VPN adapters by their well-known names. Public IP lookup is optional
and always time-limited so a blocked network cannot stall the bot.
"""

import ipaddress
import logging
import os
import re
import socket
import subprocess
import time
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


# --- LAN diagnostics -------------------------------------------------------

SPEEDTEST_DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes={size}"
SPEEDTEST_UPLOAD_URL = "https://speed.cloudflare.com/__up"
SPEEDTEST_USER_AGENT = "PCRemoteBot/1.0"

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _valid_ipv4(value: str) -> Optional[str]:
    """Return a normalized IPv4 string, or None when *value* is not an IP."""
    try:
        return str(ipaddress.IPv4Address((value or "").strip()))
    except ipaddress.AddressValueError:
        return None


def _windows_default_gateway() -> Optional[str]:
    """Read the lowest-metric IPv4 default route from ``route print``."""
    try:
        result = subprocess.run(
            ["route", "print", "-4"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=5,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("Default route query failed: %s", e)
        return None

    best: Optional[tuple] = None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        if parts[0] != "0.0.0.0" or parts[1] != "0.0.0.0":
            continue
        gateway = _valid_ipv4(parts[2])
        if not gateway:
            continue
        metric = 9999
        if len(parts) >= 5:
            try:
                metric = int(parts[4])
            except ValueError:
                pass
        if best is None or metric < best[0]:
            best = (metric, gateway)
    return best[1] if best else None


def get_default_gateway() -> Optional[str]:
    """Return the active IPv4 default gateway, or None when unavailable."""
    if os.name == "nt":
        return _windows_default_gateway()

    # POSIX fallback: parse the default line from ``ip route``.
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("Default route query failed: %s", e)
        return None
    match = re.search(r"\bdefault\s+via\s+(\S+)", result.stdout)
    return _valid_ipv4(match.group(1)) if match else None


def get_dns_servers() -> List[str]:
    """Return configured DNS server addresses (Windows registry)."""
    if os.name != "nt":
        return []

    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows always has winreg
        return []

    servers: List[str] = []
    base = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            index = 0
            while True:
                try:
                    guid = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, guid) as iface:
                        values = []
                        for name in ("NameServer", "DhcpNameServer"):
                            try:
                                value, _ = winreg.QueryValueEx(iface, name)
                            except OSError:
                                continue
                            values.append(str(value or ""))
                except OSError:
                    continue

                for value in values:
                    for raw in re.split(r"[,;\s]+", value):
                        address = _valid_ipv4(raw)
                        if address and address != "0.0.0.0" and address not in servers:
                            servers.append(address)
    except OSError as e:
        logger.debug("DNS registry query failed: %s", e)
        return []
    return servers


def dns_lookup(host: str, timeout: float = 3.0) -> Dict[str, Any]:
    """Resolve *host* once and report the address and elapsed time."""
    started = time.monotonic()
    try:
        infos = socket.getaddrinfo(
            host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
        addresses = [info[4][0] for info in infos if info[4]]
        address = addresses[0] if addresses else None
        ok = address is not None
        error = None if ok else "no address returned"
    except (OSError, UnicodeError) as e:
        address = None
        ok = False
        error = str(e)
    seconds = time.monotonic() - started
    if seconds > timeout:
        ok = False
        error = error or f"lookup took {seconds:.1f}s"
    return {
        "host": host,
        "address": address,
        "ok": ok,
        "seconds": round(seconds, 3),
        "error": error,
    }


def _to_number(value: str) -> Optional[float]:
    """Convert a ping timing that may use a comma decimal separator."""
    try:
        return float(value.replace(",", "."))
    except (TypeError, ValueError):
        return None


def parse_ping_output(text: str) -> Dict[str, Any]:
    """Extract packet loss and average latency from Windows/POSIX ping output."""
    raw = text or ""
    lowered = raw.casefold()
    loss_match = re.search(r"\((\d+)\s*%", raw)
    if not loss_match:
        loss_match = re.search(
            r"(\d+)\s*%\s*(?:packet\s+)?(?:loss|потерь)", raw, re.IGNORECASE
        )
    if loss_match:
        loss: Optional[int] = int(loss_match.group(1))
    else:
        loss = 100 if ("100%" in raw or "timed out" in lowered
                       or "недоступен" in lowered) else None

    times = [
        value
        for value in (
            _to_number(item)
            for item in re.findall(
                r"(?:time|время|average|среднее)\s*[=<]\s*(\d+(?:[.,]\d+)?)\s*(?:ms|мс)",
                raw,
                re.IGNORECASE,
            )
        )
        if value is not None
    ]
    if not times:
        # POSIX summary: "rtt min/avg/max/mdev = 0.038/0.045/0.052/0.001 ms"
        summary = re.search(
            r"=\s*[\d.]+/(\d+(?:[.,]\d+)?)/[\d.]+/[\d.]+\s*(?:ms|мс)",
            raw,
            re.IGNORECASE,
        )
        if summary:
            value = _to_number(summary.group(1))
            if value is not None:
                times.append(value)

    average = round(times[-1], 1) if times else None
    ok = loss is not None and loss < 100
    return {"loss_percent": loss, "avg_ms": average, "ok": ok}


def ping_host(host: str, count: int = 4, timeout_ms: int = 1000) -> Dict[str, Any]:
    """Ping *host* and return loss/latency information."""
    if not host:
        return {
            "host": host,
            "sent": count,
            "loss_percent": None,
            "avg_ms": None,
            "ok": False,
            "error": "no host",
        }

    if os.name == "nt":
        command = ["ping", "-n", str(count), "-w", str(timeout_ms), host]
    else:
        command = ["ping", "-c", str(count), "-W", str(max(1, timeout_ms // 1000)), host]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=max(5.0, count * (timeout_ms / 1000.0) + 5.0),
            creationflags=_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("Ping %s failed: %s", host, e)
        return {
            "host": host,
            "sent": count,
            "loss_percent": 100,
            "avg_ms": None,
            "ok": False,
            "error": str(e),
        }

    parsed = parse_ping_output(result.stdout or result.stderr or "")
    parsed.update({"host": host, "sent": count})
    return parsed


def parse_traceroute_output(text: str) -> List[Dict[str, Any]]:
    """Parse numbered hops from Windows ``tracert`` or POSIX ``traceroute``."""
    hops: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        match = re.match(r"^\s*(\d+)\s+(.+)$", line)
        if not match:
            continue
        rest = match.group(2)
        address = _IPV4_RE.search(rest)
        times = [
            value
            for value in (
                _to_number(item)
                for item in re.findall(
                    r"(?<![\d.])(\d+(?:[.,]\d+)?)\s*(?:ms|мс)\b",
                    rest,
                    re.IGNORECASE,
                )
            )
            if value is not None
        ]
        hops.append(
            {
                "hop": int(match.group(1)),
                "address": address.group(0) if address else "*",
                "ms": round(times[-1], 1) if times else None,
            }
        )
    return hops


def traceroute(
    host: str, max_hops: int = 8, probe_timeout: float = 0.4
) -> List[Dict[str, Any]]:
    """Run a bounded traceroute and return parsed hops."""
    if not host:
        return []
    if os.name == "nt":
        command = [
            "tracert", "-d", "-h", str(max_hops),
            "-w", str(max(100, int(probe_timeout * 1000))), host,
        ]
    else:
        command = [
            "traceroute", "-n", "-m", str(max_hops),
            "-w", str(max(1, int(probe_timeout))), host,
        ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=max(10.0, max_hops * 3 * probe_timeout + 5.0),
            creationflags=_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("Traceroute to %s failed: %s", host, e)
        return []
    return parse_traceroute_output(result.stdout)


def collect_lan_diagnostics() -> Dict[str, Any]:
    """Collect gateway, ping, DNS servers and DNS lookup timings."""
    gateway = get_default_gateway()
    gateway_ping = (
        ping_host(gateway, count=2, timeout_ms=800) if gateway else None
    )
    internet_ping = ping_host("1.1.1.1", count=2, timeout_ms=800)
    return {
        "gateway": gateway,
        "gateway_ping": gateway_ping,
        "internet_ping": internet_ping,
        "dns_servers": get_dns_servers(),
        "lookups": [dns_lookup("example.com"), dns_lookup("cloudflare.com")],
    }


# --- Speed test ------------------------------------------------------------

def _mbps(byte_count: int, seconds: float) -> Optional[float]:
    """Convert bytes transferred in *seconds* to megabits per second."""
    if byte_count <= 0 or seconds <= 0.01:
        return None
    return round(byte_count * 8 / seconds / 1_000_000, 2)


def _download_test(max_bytes: int, time_budget: float) -> Dict[str, Any]:
    """Download at most *max_bytes* from Cloudflare within *time_budget*."""
    started = time.monotonic()
    max_bytes = max(0, int(max_bytes))
    if max_bytes == 0 or time_budget <= 0:
        return {"bytes": 0, "seconds": 0.0, "error": "skipped (budget)"}
    total = 0
    error: Optional[str] = None
    timeout = max(1.0, min(6.0, float(time_budget)))
    request = urllib.request.Request(
        SPEEDTEST_DOWNLOAD_URL.format(size=max_bytes),
        headers={
            "User-Agent": SPEEDTEST_USER_AGENT,
            "Cache-Control": "no-store",
            "Accept": "*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            while total < max_bytes:
                if time.monotonic() - started >= time_budget:
                    break
                chunk = response.read(min(65_536, max_bytes - total))
                if not chunk:
                    break
                total += len(chunk)
    except Exception as e:
        logger.debug("Speedtest download failed: %s", e)
        error = str(e)
    seconds = time.monotonic() - started
    return {"bytes": total, "seconds": seconds, "error": error}


def _upload_test(max_bytes: int, time_budget: float) -> Dict[str, Any]:
    """Upload at most *max_bytes* to Cloudflare within *time_budget*."""
    started = time.monotonic()
    max_bytes = max(0, int(max_bytes))
    if max_bytes == 0 or time_budget <= 0:
        return {"bytes": 0, "seconds": 0.0, "error": "skipped (budget)"}
    total = 0
    error: Optional[str] = None
    payload = b"0" * max_bytes
    timeout = max(1.0, min(8.0, float(time_budget)))
    request = urllib.request.Request(
        SPEEDTEST_UPLOAD_URL,
        data=payload,
        method="POST",
        headers={
            "User-Agent": SPEEDTEST_USER_AGENT,
            "Content-Type": "application/octet-stream",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(256)
        total = len(payload)
    except Exception as e:
        logger.debug("Speedtest upload failed: %s", e)
        error = str(e)
    seconds = time.monotonic() - started
    return {"bytes": total, "seconds": seconds, "error": error}


def run_speedtest(
    download_bytes: int = 12_500_000,
    upload_bytes: int = 4_000_000,
    download_budget: float = 10.0,
    upload_budget: float = 10.0,
) -> Dict[str, Any]:
    """Run a bounded Cloudflare download/upload test (stdlib only)."""
    down = _download_test(download_bytes, download_budget)
    up = _upload_test(upload_bytes, upload_budget)
    errors = []
    if down["error"]:
        errors.append(f"download: {down['error']}")
    if up["error"]:
        errors.append(f"upload: {up['error']}")
    return {
        "server": "Cloudflare",
        "download_bytes": down["bytes"],
        "download_seconds": round(down["seconds"], 2),
        "download_mbps": _mbps(down["bytes"], down["seconds"]),
        "upload_bytes": up["bytes"],
        "upload_seconds": round(up["seconds"], 2),
        "upload_mbps": _mbps(up["bytes"], up["seconds"]),
        "errors": errors,
    }