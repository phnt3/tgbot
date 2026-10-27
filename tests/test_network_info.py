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