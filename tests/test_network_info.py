"""Tests for network/VPN status collection.

psutil and the subprocess/urllib calls are replaced so the tests are
deterministic and never touch the real network.
"""

import socket

import network_info
from network_info import (
    collect_network_status,
    is_vpn_interface,
    parse_wifi_output,
)


class FakeAddress:
    def __init__(self, family, address):
        self.family = family
        self.address = address


class FakeStats:
    def __init__(self, isup=True, speed=1000):
        self.isup = isup
        self.speed = speed


def test_is_vpn_interface_matches_known_adapters():
    assert is_vpn_interface("WireGuard Tunnel")
    assert is_vpn_interface("wintun")
    assert is_vpn_interface("OpenVPN TAP-Windows6")
    assert is_vpn_interface("Tailscale")
    assert not is_vpn_interface("Ethernet")
    assert not is_vpn_interface("")


def test_parse_wifi_output_connected():
    output = """
    Name                   : Wi-Fi
    State                  : connected
    SSID                   : HomeNet
    BSSID                  : aa:bb:cc:dd:ee:ff
    Signal                 : 87%
    Authentication         : WPA2-Personal
    Radio type             : 802.11ac
    Channel                : 36
    """

    wifi = parse_wifi_output(output)

    assert wifi == {
        "ssid": "HomeNet",
        "signal": "87%",
        "channel": "36",
        "authentication": "WPA2-Personal",
    }


def test_parse_wifi_output_returns_none_when_disconnected():
    output = "    State                  : disconnected\n    SSID                   : \n"
    assert parse_wifi_output(output) is None


def test_collect_network_status_marks_active_vpn(monkeypatch):
    monkeypatch.setattr(
        network_info.psutil,
        "net_if_addrs",
        lambda: {
            "Ethernet": [
                FakeAddress(socket.AF_INET, "192.168.1.10"),
                FakeAddress(network_info.psutil.AF_LINK, "aa:bb:cc:dd:ee:ff"),
            ],
            "WireGuard Tunnel": [
                FakeAddress(socket.AF_INET, "10.8.0.2"),
                FakeAddress(network_info.psutil.AF_LINK, ""),
            ],
            "Idle Adapter": [
                FakeAddress(socket.AF_INET, "169.254.1.1"),
            ],
        },
    )
    monkeypatch.setattr(
        network_info.psutil,
        "net_if_stats",
        lambda: {
            "Ethernet": FakeStats(isup=True, speed=1000),
            "WireGuard Tunnel": FakeStats(isup=True, speed=0),
            "Idle Adapter": FakeStats(isup=False, speed=0),
        },
    )
    monkeypatch.setattr(network_info, "get_wifi_info", lambda: None)
    monkeypatch.setattr(network_info, "get_public_ip", lambda: "203.0.113.9")
    monkeypatch.setattr(network_info.socket, "gethostname", lambda: "TEST-PC")

    status = collect_network_status()

    assert status["hostname"] == "TEST-PC"
    assert [item["name"] for item in status["active"]] == [
        "Ethernet",
        "WireGuard Tunnel",
    ]
    assert [item["name"] for item in status["vpn_active"]] == ["WireGuard Tunnel"]
    assert status["public_ip"] == "203.0.113.9"
    assert status["interfaces"][0]["ipv4"] == ["192.168.1.10"]


def test_public_ip_can_be_skipped(monkeypatch):
    monkeypatch.setattr(network_info, "get_interfaces", lambda: [])
    monkeypatch.setattr(network_info, "get_wifi_info", lambda: None)

    called = []
    monkeypatch.setattr(
        network_info, "get_public_ip", lambda: called.append(True) or "1.2.3.4"
    )

    status = collect_network_status(include_public_ip=False)

    assert status["public_ip"] is None
    assert called == []


# --- LAN diagnostics -------------------------------------------------------


def _completed(stdout="", stderr=""):
    return network_info.subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=stderr
    )


def test_valid_ipv4_normalizes_and_rejects():
    assert network_info._valid_ipv4(" 192.168.1.10 ") == "192.168.1.10"
    assert network_info._valid_ipv4("999.1.2.3") is None
    assert network_info._valid_ipv4("not-an-ip") is None
    assert network_info._valid_ipv4(None) is None


def test_windows_default_gateway_chooses_lowest_metric(monkeypatch):
    output = """
IPv4 Route Table
===========================================================================
Active Routes:
Network Destination        Netmask          Gateway       Interface  Metric
          0.0.0.0          0.0.0.0      192.168.1.1     192.168.1.50      25
          0.0.0.0          0.0.0.0        10.0.0.1       10.0.0.20       5
          0.0.0.0          0.0.0.0        172.16.0.1     172.16.0.20      50
===========================================================================
"""
    monkeypatch.setattr(
        network_info.subprocess, "run", lambda *a, **k: _completed(output)
    )
    assert network_info._windows_default_gateway() == "10.0.0.1"


def test_default_gateway_posix_fallback(monkeypatch):
    monkeypatch.setattr(network_info.os, "name", "posix")
    monkeypatch.setattr(
        network_info.subprocess,
        "run",
        lambda *a, **k: _completed("default via 192.168.0.1 dev eth0\n"),
    )
    assert network_info.get_default_gateway() == "192.168.0.1"


def test_dns_lookup_reports_success(monkeypatch):
    monkeypatch.setattr(
        network_info.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))],
    )
    result = network_info.dns_lookup("example.com")
    assert result["ok"] is True
    assert result["address"] == "1.2.3.4"
    assert result["error"] is None


def test_dns_lookup_reports_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise socket.gaierror("name not known")

    monkeypatch.setattr(network_info.socket, "getaddrinfo", boom)
    result = network_info.dns_lookup("missing.invalid")
    assert result["ok"] is False
    assert result["address"] is None
    assert "name not known" in result["error"]


def test_dns_lookup_flags_slow_lookup(monkeypatch):
    ticks = iter([100.0, 104.5])
    monkeypatch.setattr(network_info.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        network_info.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))],
    )
    result = network_info.dns_lookup("slow.example", timeout=3.0)
    assert result["ok"] is False
    assert "lookup took" in result["error"]


def test_parse_ping_output_windows_and_russian():
    windows = """
Pinging 1.1.1.1 with 32 bytes of data:
Reply from 1.1.1.1: bytes=32 time=12ms TTL=57
Reply from 1.1.1.1: bytes=32 time<1ms TTL=57
Packets: Sent = 4, Received = 4, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 0ms, Maximum = 12ms, Average = 6ms
"""
    parsed = network_info.parse_ping_output(windows)
    assert parsed == {"loss_percent": 0, "avg_ms": 6.0, "ok": True}

    russian = """
Ответ от 1.1.1.1: число байт=32 время=3мс TTL=57
Пакеты: отправлено = 4, получено = 3, потеряно = 1 (25% потерь),
    Минимальное = 2мсек, Максимальное = 3мсек, Среднее = 2мсек
"""
    parsed = network_info.parse_ping_output(russian)
    assert parsed["loss_percent"] == 25
    assert parsed["ok"] is True


def test_parse_ping_output_detects_total_loss():
    parsed = network_info.parse_ping_output("Request timed out.")
    assert parsed["loss_percent"] == 100
    assert parsed["ok"] is False


def test_parse_ping_output_posix_summary():
    output = """
4 packets transmitted, 4 received, 0% packet loss, time 3005ms
rtt min/avg/max/mdev = 0.038/0.045/0.052/0.001 ms
"""
    parsed = network_info.parse_ping_output(output)
    assert parsed["loss_percent"] == 0
    assert parsed["avg_ms"] == 0.0
    assert parsed["ok"] is True


def test_ping_host_returns_error_for_empty_host():
    result = network_info.ping_host("")
    assert result["ok"] is False
    assert result["error"] == "no host"


def test_ping_host_parses_command_output(monkeypatch):
    output = "Packets: Sent = 4, Received = 4, Lost = 0 (0% loss), Average = 8ms"
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _completed(output)

    monkeypatch.setattr(network_info.subprocess, "run", fake_run)
    result = network_info.ping_host("example.com", count=4, timeout_ms=1000)
    assert result["ok"] is True
    assert result["avg_ms"] == 8.0
    assert result["host"] == "example.com"
    assert "example.com" in calls[0]


def test_parse_traceroute_output_windows_and_posix():
    windows = """
Tracing route to example.com over a maximum of 8 hops:
  1     1 ms     1 ms     1 ms  192.168.1.1
  2     *        *        *     Request timed out.
  3    10 ms    11 ms    12 ms  203.0.113.1
"""
    hops = network_info.parse_traceroute_output(windows)
    assert hops == [
        {"hop": 1, "address": "192.168.1.1", "ms": 1.0},
        {"hop": 2, "address": "*", "ms": None},
        {"hop": 3, "address": "203.0.113.1", "ms": 12.0},
    ]

    posix = " 1  192.168.1.1  1.123 ms  1.456 ms  1.789 ms\n"
    hops = network_info.parse_traceroute_output(posix)
    assert hops == [{"hop": 1, "address": "192.168.1.1", "ms": 1.8}]


def test_traceroute_returns_empty_on_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise network_info.subprocess.TimeoutExpired("tracert", 5)

    monkeypatch.setattr(network_info.subprocess, "run", boom)
    assert network_info.traceroute("example.com") == []


def test_collect_lan_diagnostics_wires_helpers(monkeypatch):
    calls = []
    monkeypatch.setattr(network_info, "get_default_gateway", lambda: "192.168.1.1")
    monkeypatch.setattr(network_info, "get_dns_servers", lambda: ["1.1.1.1"])
    monkeypatch.setattr(
        network_info,
        "ping_host",
        lambda host, count=4, timeout_ms=1000: calls.append(host)
        or {"host": host, "ok": True, "loss_percent": 0, "avg_ms": 5.0},
    )
    monkeypatch.setattr(
        network_info,
        "dns_lookup",
        lambda host: {"host": host, "ok": True, "address": "1.2.3.4", "seconds": 0.01},
    )
    result = network_info.collect_lan_diagnostics()
    assert result["gateway"] == "192.168.1.1"
    assert result["dns_servers"] == ["1.1.1.1"]
    assert calls == ["192.168.1.1", "1.1.1.1"]
    assert [item["host"] for item in result["lookups"]] == [
        "example.com",
        "cloudflare.com",
    ]


# --- Speed test ------------------------------------------------------------


def test_mbps_conversion_and_guards():
    assert network_info._mbps(1_000_000, 1.0) == 8.0
    assert network_info._mbps(0, 1.0) is None
    assert network_info._mbps(100, 0.0) is None


class _FakeResponse:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if size is not None and size >= 0 and len(chunk) > size:
            remainder = chunk[size:]
            self.chunks.insert(0, remainder)
            return chunk[:size]
        return chunk


def test_download_test_measures_bounded_bytes(monkeypatch):
    requests = []

    def fake_urlopen(request, timeout=0):
        requests.append((request, timeout))
        return _FakeResponse([b"a" * 6, b"b" * 6])

    monkeypatch.setattr(network_info.urllib.request, "urlopen", fake_urlopen)
    result = network_info._download_test(10, 5.0)
    assert result["bytes"] == 10
    assert result["error"] is None
    assert requests[0][1] == 5.0


def test_download_test_skips_without_budget(monkeypatch):
    calls = []
    monkeypatch.setattr(
        network_info.urllib.request, "urlopen", lambda *a, **k: calls.append(1)
    )
    result = network_info._download_test(100, 0)
    assert result["bytes"] == 0
    assert result["error"] == "skipped (budget)"
    assert calls == []


def test_upload_test_sends_payload_and_honours_timeout(monkeypatch):
    uploaded = []

    def fake_urlopen(request, timeout=0):
        uploaded.append((request.data, timeout))
        return _FakeResponse([b"ok"])

    monkeypatch.setattr(network_info.urllib.request, "urlopen", fake_urlopen)
    result = network_info._upload_test(16, 3.0)
    assert result["bytes"] == 16
    assert uploaded[0][0] == b"0" * 16
    assert uploaded[0][1] == 3.0


def test_upload_test_reports_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(network_info.urllib.request, "urlopen", boom)
    result = network_info._upload_test(8, 2.0)
    assert result["bytes"] == 0
    assert "network down" in result["error"]


def test_run_speedtest_combines_results(monkeypatch):
    monkeypatch.setattr(
        network_info,
        "_download_test",
        lambda *a: {"bytes": 1_000_000, "seconds": 1.0, "error": None},
    )
    monkeypatch.setattr(
        network_info,
        "_upload_test",
        lambda *a: {"bytes": 500_000, "seconds": 1.0, "error": "denied"},
    )
    result = network_info.run_speedtest()
    assert result["download_mbps"] == 8.0
    assert result["upload_mbps"] == 4.0
    assert result["errors"] == ["upload: denied"]