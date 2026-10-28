"""Handler tests for the chart menu and the per-drive disk picker."""

import asyncio

import pytest

import handlers
from handlers import _CHART_DRIVES, _charts_keyboard, show_chart
from tests.fakes import FakeMessage, button_datas


@pytest.fixture(autouse=True)
def reset_chart_state():
    _CHART_DRIVES.clear()
    yield
    _CHART_DRIVES.clear()


def run(coro):
    return asyncio.run(coro)


def texts(message):
    return [item["text"] for item in message.sent]


def test_charts_keyboard_offers_disk_drive_picker():
    datas = button_datas(_charts_keyboard())
    assert "chart_disk" in datas
    assert "chart_disk_5m" not in datas
    assert "chart_cpu_5m" in datas
    assert "chart_ram_24h" in datas


def test_show_chart_disk_opens_drive_picker(monkeypatch):
    monkeypatch.setattr(
        handlers, "_disk_drives", lambda: ["C:\\", "D:\\"]
    )
    message = FakeMessage()

    run(show_chart(message, "disk"))

    assert "=== DISK CHART ===" in texts(message)[0]
    datas = button_datas(message.sent[0]["reply_markup"])
    assert "chart_diskd_0_5m" in datas
    assert "chart_diskd_0_1h" in datas
    assert "chart_diskd_1_24h" in datas
    assert "chart_menu" in datas
    assert _CHART_DRIVES[message.chat.id] == ["C:\\", "D:\\"]


def test_show_chart_legacy_disk_range_opens_picker(monkeypatch):
    monkeypatch.setattr(handlers, "_disk_drives", lambda: ["D:\\"])
    message = FakeMessage()

    run(show_chart(message, "disk_1h"))

    assert "Suggested range: last hour." in texts(message)[0]
    assert "chart_diskd_0_1h" in button_datas(message.sent[0]["reply_markup"])


def test_show_chart_without_drives_explains_history(monkeypatch):
    monkeypatch.setattr(handlers, "_disk_drives", lambda: [])
    message = FakeMessage()

    run(show_chart(message, "disk"))

    assert "No drives found yet" in texts(message)[0]
    assert button_datas(message.sent[0]["reply_markup"]).count("chart_disk") == 1


def test_show_chart_sends_selected_drive(monkeypatch):
    calls = []

    async def fake_send(message, metric, time_range, device_id=None):
        calls.append((metric, time_range, device_id))

    monkeypatch.setattr(handlers, "_send_chart", fake_send)
    message = FakeMessage()
    _CHART_DRIVES[message.chat.id] = ["C:\\", "D:\\"]

    run(show_chart(message, "diskd_1_1h"))

    assert calls == [("disk", "1h", "D:\\")]


def test_show_chart_rejects_expired_drive_selection(monkeypatch):
    calls = []

    async def fake_send(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(handlers, "_send_chart", fake_send)
    message = FakeMessage()

    run(show_chart(message, "diskd_3_5m"))

    assert calls == []
    assert "Drive selection expired" in texts(message)[0]


def test_show_chart_routes_generic_metric(monkeypatch):
    calls = []

    async def fake_send(message, metric, time_range, device_id=None):
        calls.append((metric, time_range, device_id))

    monkeypatch.setattr(handlers, "_send_chart", fake_send)
    message = FakeMessage()

    run(show_chart(message, "cpu_24h"))

    assert calls == [("cpu", "24h", None)]


def test_show_chart_rejects_unknown_selection(monkeypatch):
    calls = []

    async def fake_send(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(handlers, "_send_chart", fake_send)
    message = FakeMessage()

    run(show_chart(message, "network_1h"))

    assert calls == []
    assert "Unknown chart selection" in texts(message)[0]