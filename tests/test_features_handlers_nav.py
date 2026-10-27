"""Handler tests for file browsing, uploads, media, network and scheduling."""

import asyncio
import time
from pathlib import Path

import pytest

from handlers import _PENDING_CONFIRMATIONS, features
from scheduler import TaskScheduler
from tests.fakes import FakeBot, FakeCallback, FakeMessage, button_datas


@pytest.fixture(autouse=True)
def reset_feature_state():
    features._FILE_STATE.clear()
    features.set_scheduler(None)
    yield
    features._FILE_STATE.clear()
    features.set_scheduler(None)


def run(coro):
    return asyncio.run(coro)


def texts(message):
    return [item["text"] for item in message.sent]


class Clock:
    def __init__(self, value=1_700_000_000.0):
        self.value = value

    def __call__(self):
        return self.value


def make_scheduler(tmp_path, clock=None):
    return TaskScheduler(
        tmp_path / "tasks.json",
        bot=FakeBot(),
        user_ids=[1],
        executor=lambda task: (True, "ok"),
        clock=clock or Clock(),
    )


# --- File browser ----------------------------------------------------------


def test_file_browser_lists_entries_and_opens_directory(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "note.txt").write_bytes(b"hello")
    message = FakeMessage()

    run(features.show_files(message, tmp_path))

    body = texts(message)[0]
    assert f"=== FILES: {tmp_path} ===" in body
    assert "Entries: 2 | Page 1/1" in body
    datas = button_datas(message.sent[0]["reply_markup"])
    assert datas[:2] == ["fm_open_0", "fm_get_1"]
    assert {"fm_home", "fm_up", "fm_refresh"}.issubset(datas)

    run(features.handle_callback(FakeCallback(), "fm_noop"))
    callback = FakeCallback()
    callback.message.chat.id = message.chat.id
    run(features._handle_file_callback(callback, "fm_open_0"))

    assert f"=== FILES: {tmp_path / 'docs'} ===" in texts(callback.message)[0]


def test_file_browser_pages_long_listings(tmp_path):
    for index in range(10):
        (tmp_path / f"file{index:02d}.txt").write_text("x")
    message = FakeMessage()

    run(features.show_files(message, tmp_path))

    assert "Page 1/2" in texts(message)[0]
    assert "fm_page_1" in button_datas(message.sent[0]["reply_markup"])

    callback = FakeCallback()
    callback.message.chat.id = message.chat.id
    run(features._handle_file_callback(callback, "fm_page_1"))

    assert "Page 2/2" in texts(callback.message)[0]
    assert "fm_page_0" in button_datas(callback.message.sent[0]["reply_markup"])


def test_sending_files_enforces_the_limit(tmp_path, monkeypatch):
    small = tmp_path / "small.txt"
    small.write_bytes(b"abc")
    large = tmp_path / "large.bin"
    large.write_bytes(b"abc")

    message = FakeMessage()
    run(features.show_files(message, tmp_path))

    monkeypatch.setattr(features, "MAX_SEND_BYTES", 2)
    callback = FakeCallback()
    callback.message.chat.id = message.chat.id
    run(features._handle_file_callback(callback, "fm_get_1"))
    assert "exceeds the 2 B limit" in texts(callback.message)[-1]

    monkeypatch.setattr(features, "MAX_SEND_BYTES", 10)
    callback = FakeCallback()
    callback.message.chat.id = message.chat.id
    run(features._handle_file_callback(callback, "fm_get_0"))
    sent = callback.message.sent[-1]
    assert sent["text"] == "large.bin"
    assert sent["document"].path == str(large)


def test_file_browser_rejects_wrong_entry_types_and_stale_state(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "note.txt").write_text("x")
    message = FakeMessage()
    run(features.show_files(message, tmp_path))

    callback = FakeCallback()
    callback.message.chat.id = message.chat.id
    run(features._handle_file_callback(callback, "fm_get_0"))
    assert callback.answers[-1]["text"] == "That is a folder"

    callback = FakeCallback()
    callback.message.chat.id = message.chat.id
    run(features._handle_file_callback(callback, "fm_open_1"))
    assert callback.answers[-1]["text"] == "That is a file"

    callback = FakeCallback()
    callback.message.chat.id = 999
    run(features._handle_file_callback(callback, "fm_refresh"))
    assert callback.answers[-1]["text"] == "File browser expired"
    assert f"=== FILES: {Path.home()} ===" in texts(callback.message)[0]


def test_file_browser_reports_unreadable_path(tmp_path):
    message = FakeMessage()
    file_path = tmp_path / "file.txt"
    file_path.write_text("x")

    run(features.show_files(message, file_path))

    assert "Cannot open folder" in texts(message)[0]


# --- Uploads ---------------------------------------------------------------


class FakeDocument:
    def __init__(self, file_id="doc1", file_name="report.pdf", file_size=5):
        self.file_id = file_id
        self.file_name = file_name
        self.file_size = file_size


class FakePhoto:
    def __init__(self, file_id="photo1", file_size=4):
        self.file_id = file_id
        self.file_size = file_size


def _authorize(monkeypatch):
    import security

    monkeypatch.setattr(security, "is_user_authorized", lambda user_id: True)


def test_upload_saves_document(monkeypatch, tmp_path):
    _authorize(monkeypatch)
    monkeypatch.setattr(features, "default_upload_dir", lambda: tmp_path)
    message = FakeMessage()
    message.document = FakeDocument()
    message.bot = FakeBot(payload=b"hello")

    run(features.handle_upload(message))

    assert message.bot.downloads == ["doc1"]
    saved = tmp_path / "report.pdf"
    assert saved.read_bytes() == b"hello"
    assert "Saved 5 B" in texts(message)[-1]


def test_upload_names_photos_and_never_overwrites(monkeypatch, tmp_path):
    _authorize(monkeypatch)
    monkeypatch.setattr(features, "default_upload_dir", lambda: tmp_path)
    monkeypatch.setattr(features.time, "strftime", lambda fmt: "20240102_030405")

    message = FakeMessage()
    message.photo = [FakePhoto(file_id="small"), FakePhoto(file_id="large")]
    message.bot = FakeBot(payload=b"jpeg")

    run(features.handle_upload(message))

    assert message.bot.downloads == ["large"]
    assert (tmp_path / "photo_20240102_030405.jpg").read_bytes() == b"jpeg"


def test_upload_rejects_large_files_before_download(monkeypatch, tmp_path):
    _authorize(monkeypatch)
    monkeypatch.setattr(features, "default_upload_dir", lambda: tmp_path)
    message = FakeMessage()
    message.document = FakeDocument(file_size=features.MAX_UPLOAD_BYTES + 1)

    run(features.handle_upload(message))

    assert message.bot.downloads == []
    assert "upload limit" in texts(message)[-1]


def test_upload_failure_and_unauthorized_are_silent(monkeypatch, tmp_path):
    import security

    monkeypatch.setattr(features, "default_upload_dir", lambda: tmp_path)
    message = FakeMessage()
    message.document = FakeDocument()

    monkeypatch.setattr(security, "is_user_authorized", lambda user_id: False)
    run(features.handle_upload(message))
    assert message.sent == [] and message.bot.downloads == []

    monkeypatch.setattr(security, "is_user_authorized", lambda user_id: True)
    message.bot = FakeBot(fail=True)
    run(features.handle_upload(message))
    assert "Failed to receive file" in texts(message)[-1]


# --- Media -----------------------------------------------------------------


def test_media_menu_and_whitelisted_action(monkeypatch):
    message = FakeMessage()
    run(features.show_media_menu(message))
    assert "VOLUME & MEDIA" in texts(message)[0]
    assert "med_play_pause" in button_datas(message.sent[0]["reply_markup"])

    calls = []
    monkeypatch.setattr(
        features, "press_key", lambda action: calls.append(action) or (True, "ok")
    )
    callback = FakeCallback()
    run(features.handle_callback(callback, "med_vol_up"))
    assert calls == ["vol_up"]
    assert callback.answers[-1] == {"text": "ok", "show_alert": False}


def test_media_rejects_unknown_action(monkeypatch):
    calls = []
    monkeypatch.setattr(features, "press_key", lambda action: calls.append(action))
    callback = FakeCallback()

    run(features.handle_callback(callback, "med_format_disk"))

    assert calls == []
    assert callback.answers[-1]["show_alert"] is True


# --- Network ---------------------------------------------------------------


def test_network_screen_shows_vpn_wifi_and_ip(monkeypatch):
    monkeypatch.setattr(
        features,
        "collect_network_status",
        lambda: {
            "hostname": "TEST-PC",
            "interfaces": [],
            "active": [
                {"name": "Ethernet", "ipv4": ["192.168.1.2"], "speed_mbps": 1000}
            ],
            "vpn_active": [
                {"name": "WireGuard", "ipv4": ["10.0.0.2"], "speed_mbps": 0}
            ],
            "wifi": {
                "ssid": "Home",
                "signal": "80%",
                "authentication": "WPA2",
            },
            "public_ip": "203.0.113.5",
        },
    )
    message = FakeMessage()

    run(features.show_network(message))

    body = texts(message)[0]
    assert "Host: TEST-PC" in body
    assert "VPN: ACTIVE - WireGuard (10.0.0.2)" in body
    assert "Ethernet: 192.168.1.2 (1000 Mbps)" in body
    assert "Wi-Fi: Home (80% signal)" in body
    assert "Public IP: 203.0.113.5" in body


def test_network_screen_without_vpn(monkeypatch):
    monkeypatch.setattr(
        features,
        "collect_network_status",
        lambda: {
            "hostname": "PC",
            "active": [],
            "vpn_active": [],
            "wifi": None,
            "public_ip": None,
        },
    )
    message = FakeMessage()

    run(features.show_network(message))

    body = texts(message)[0]
    assert "VPN: not detected" in body
    assert "No active network interfaces." in body
    assert "Public IP: unavailable" in body


# --- Scheduler -------------------------------------------------------------


def test_scheduler_screen_without_running_scheduler():
    message = FakeMessage()
    run(features.show_scheduler(message))
    assert "Scheduler is not running." in texts(message)[0]


def test_quick_schedule_adds_and_cancels(tmp_path):
    scheduler = make_scheduler(tmp_path)
    features.set_scheduler(scheduler)
    message = FakeMessage()

    run(features.show_scheduler(message))
    assert "No pending tasks." in texts(message)[0]

    callback = FakeCallback()
    callback.message.chat.id = message.chat.id
    run(features.handle_callback(callback, "sch_add_lock_15"))

    tasks = scheduler.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["action"] == "lock"
    assert f"Cancelled" not in callback.answers[-1]["text"]
    assert f"#{tasks[0]['id']}" in texts(callback.message)[-1]

    callback = FakeCallback()
    callback.message.chat.id = message.chat.id
    run(features.handle_callback(callback, f"sch_cancel_{tasks[0]['id']}"))
    assert scheduler.list_tasks() == []
    assert callback.answers[-1]["text"] == "Task cancelled"

    callback = FakeCallback()
    callback.message.chat.id = message.chat.id
    run(features.handle_callback(callback, "sch_cancel_99"))
    assert callback.answers[-1]["show_alert"] is True


def test_profile_quick_schedule_keeps_profile(tmp_path):
    scheduler = make_scheduler(tmp_path)
    features.set_scheduler(scheduler)
    callback = FakeCallback()

    run(features.handle_callback(callback, "sch_add_prof_GAMING_30"))

    task = scheduler.list_tasks()[0]
    assert task["action"] == "profile"
    assert task["params"] == {"profile": "GAMING"}


def test_scheduler_cancel_all_and_refresh(tmp_path):
    scheduler = make_scheduler(tmp_path)
    features.set_scheduler(scheduler)
    scheduler.add_delay("lock", 60)
    scheduler.add_delay("sleep", 120)
    message = FakeMessage()

    run(features.show_scheduler(message))
    assert "Pending: 2" in texts(message)[0]
    assert "sch_cancel_all" in button_datas(message.sent[-1]["reply_markup"])

    callback = FakeCallback()
    callback.message.chat.id = message.chat.id
    run(features.handle_callback(callback, "sch_cancel_all"))
    assert callback.answers[-1]["text"] == "Cancelled 2 task(s)"
    assert "No pending tasks." in texts(callback.message)[-1]


def test_scheduler_callback_errors():
    features.set_scheduler(None)
    callback = FakeCallback()
    run(features.handle_callback(callback, "sch_add_shut_15"))
    assert callback.answers[-1]["text"] == "Scheduler is not running"

    callback = FakeCallback()
    run(features.handle_callback(callback, "med_"))
    assert callback.answers[-1]["text"] == "Unknown media action"


def test_unknown_prefix_is_rejected():
    callback = FakeCallback()
    run(features.handle_callback(callback, "zzz_nope"))
    assert callback.answers[-1]["text"] == "Unknown action"


def test_format_remaining_boundaries(monkeypatch):
    now = 1_700_000_000.0
    monkeypatch.setattr(features.time, "time", lambda: now)
    assert features._format_remaining(now - 5) == "due now"
    assert features._format_remaining(now + 30) == "in 30s"
    assert features._format_remaining(now + 120) == "in 2m"
    assert features._format_remaining(now + 3700) == "in 1h 1m"