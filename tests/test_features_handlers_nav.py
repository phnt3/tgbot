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
    features._FILE_PENDING.clear()
    features._SEARCH_STATE.clear()
    features._SEARCH_PENDING.clear()
    features.set_scheduler(None)
    yield
    features._FILE_STATE.clear()
    features._FILE_PENDING.clear()
    features._SEARCH_STATE.clear()
    features._SEARCH_PENDING.clear()
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
    assert datas[:4] == ["fm_open_0", "fm_act_0", "fm_get_1", "fm_act_1"]
    assert {"fm_drives", "fm_up", "fm_refresh"}.issubset(datas)

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
    assert f"=== FILES: DRIVES ===" in texts(callback.message)[0]


def test_file_browser_reports_unreadable_path(tmp_path):
    message = FakeMessage()
    file_path = tmp_path / "file.txt"
    file_path.write_text("x")

    run(features.show_files(message, file_path))

    assert "Cannot open folder" in texts(message)[0]


# --- File search -----------------------------------------------------------


def _authorize(monkeypatch):
    import security

    monkeypatch.setattr(security, "is_user_authorized", lambda user_id: True)


def test_search_prompt_and_results(tmp_path, monkeypatch):
    _authorize(monkeypatch)
    (tmp_path / "videos").mkdir()
    (tmp_path / "movie_clip.mp4").write_bytes(b"x")
    message = FakeMessage()
    run(features.show_files(message, tmp_path))

    callback = FakeCallback()
    run(features._handle_file_callback(callback, "fm_search"))

    prompt = texts(callback.message)[-1]
    assert "=== SEARCH FILES ===" in prompt
    assert f"Folder: {tmp_path}" in prompt
    assert button_datas(callback.message.sent[-1]["reply_markup"]) == [
        "fm_search_cancel"
    ]
    assert features._SEARCH_PENDING

    message.text = "movie_*"
    assert run(features.handle_text(message)) is True

    body = texts(message)[-1]
    assert "=== SEARCH: movie_* ===" in body
    assert "Matches: 1" in body
    datas = button_datas(message.sent[-1]["reply_markup"])
    labels = [
        row[0].text
        for row in message.sent[-1]["reply_markup"].inline_keyboard
        if row
    ]
    assert any("movie_clip.mp4" in label for label in labels)
    assert "fm_hit_0" in datas
    assert {"fm_search", "fm_back"}.issubset(datas)
    assert features._SEARCH_PENDING == {}


def test_search_result_opens_folder_and_sends_file(tmp_path, monkeypatch):
    _authorize(monkeypatch)
    (tmp_path / "target_dir").mkdir()
    (tmp_path / "target.txt").write_text("hello")
    message = FakeMessage()
    run(features.show_files(message, tmp_path))

    run(features._handle_file_callback(FakeCallback(), "fm_search"))
    message.text = "target_dir"
    run(features.handle_text(message))
    callback = FakeCallback()
    run(features._handle_file_callback(callback, "fm_hit_0"))
    assert f"=== FILES: {tmp_path / 'target_dir'} ===" in texts(callback.message)[-1]

    run(features._handle_file_callback(FakeCallback(), "fm_back"))
    run(features._handle_file_callback(FakeCallback(), "fm_search"))
    message.text = "target.txt"
    run(features.handle_text(message))
    callback = FakeCallback()
    run(features._handle_file_callback(callback, "fm_hit_0"))
    sent = callback.message.sent[-1]
    assert sent["text"] == "target.txt"
    assert sent["document"].path == str(tmp_path / "target.txt")


def test_search_results_page(tmp_path, monkeypatch):
    _authorize(monkeypatch)
    for index in range(10):
        (tmp_path / f"match{index:02d}.txt").write_text("x")
    message = FakeMessage()
    run(features.show_files(message, tmp_path))

    run(features._handle_file_callback(FakeCallback(), "fm_search"))
    message.text = "match"
    run(features.handle_text(message))
    assert "Page 1/2" in texts(message)[-1]
    assert "fm_spage_1" in button_datas(message.sent[-1]["reply_markup"])

    callback = FakeCallback()
    run(features._handle_file_callback(callback, "fm_spage_1"))
    assert "Page 2/2" in texts(callback.message)[-1]
    assert "fm_spage_0" in button_datas(callback.message.sent[-1]["reply_markup"])


def test_search_empty_text_keeps_prompt(tmp_path, monkeypatch):
    _authorize(monkeypatch)
    message = FakeMessage()
    run(features.show_files(message, tmp_path))
    run(features._handle_file_callback(FakeCallback(), "fm_search"))

    message.text = "   "
    assert run(features.handle_text(message)) is True

    assert "Search text is empty" in texts(message)[-1]
    assert features._SEARCH_PENDING


def test_search_expired_prompt_is_ignored(tmp_path, monkeypatch):
    _authorize(monkeypatch)
    message = FakeMessage()
    run(features.show_files(message, tmp_path))
    run(features._handle_file_callback(FakeCallback(), "fm_search"))
    features._SEARCH_PENDING[message.chat.id]["expiry"] = 0.0

    message.text = "anything"
    assert run(features.handle_text(message)) is False
    assert features._SEARCH_PENDING == {}


def test_search_cancel_clears_prompt_and_returns_to_folder(tmp_path, monkeypatch):
    _authorize(monkeypatch)
    message = FakeMessage()
    run(features.show_files(message, tmp_path))
    callback = FakeCallback()
    run(features._handle_file_callback(callback, "fm_search"))
    assert features._SEARCH_PENDING

    run(features._handle_file_callback(callback, "fm_search_cancel"))

    assert features._SEARCH_PENDING == {}
    assert callback.answers[-1]["text"] == "Search cancelled"
    assert f"=== FILES: {tmp_path} ===" in texts(callback.message)[-1]


def test_search_failure_is_reported(tmp_path, monkeypatch):
    _authorize(monkeypatch)
    message = FakeMessage()
    run(features.show_files(message, tmp_path))
    run(features._handle_file_callback(FakeCallback(), "fm_search"))
    monkeypatch.setattr(
        features, "search_files", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad"))
    )

    message.text = "x"
    assert run(features.handle_text(message)) is True

    assert "Search failed: bad" in texts(message)[-1]


# --- File operations -------------------------------------------------------


def _file_callback(message, data):
    """Run one fm_* callback for the same chat and return the callback."""
    callback = FakeCallback()
    callback.message.chat.id = message.chat.id
    run(features._handle_file_callback(callback, data))
    return callback


def test_file_browser_starts_with_drives_and_returns_to_them(tmp_path, monkeypatch):
    drives = [
        {"name": "C:\\", "path": "C:\\", "total": 100 * 1024**3, "free": 40 * 1024**3},
        {"name": "D:\\", "path": "D:\\", "total": 0, "free": 0},
    ]
    monkeypatch.setattr(features, "list_drives", lambda: drives)
    message = FakeMessage()

    run(features.show_files(message))

    body = texts(message)[0]
    assert "=== FILES: DRIVES ===" in body
    assert "Drives: 2" in body
    sent = message.sent[0]
    assert button_datas(sent["reply_markup"]) == [
        "fm_drive_0",
        "fm_drive_1",
        "fm_refresh",
        "menu_main",
    ]
    labels = [row[0].text for row in sent["reply_markup"].inline_keyboard]
    assert "C:\\  40.0 GB free of 100.0 GB" in labels
    assert "D:\\  (size unavailable)" in labels

    # A drive root has no parent, so Up goes back to the drive picker.
    features._FILE_STATE[message.chat.id] = {
        "mode": "dir",
        "path": "C:\\",
        "parent": None,
        "entries": [],
        "page": 0,
    }
    callback = _file_callback(message, "fm_up")
    assert "=== FILES: DRIVES ===" in texts(callback.message)[-1]


def test_file_actions_menu_for_folder_and_file(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "note.txt").write_text("hello")
    message = FakeMessage()
    run(features.show_files(message, tmp_path))

    callback = _file_callback(message, "fm_act_0")
    body = texts(callback.message)[-1]
    assert "=== FILE ACTIONS ===" in body
    assert "Selected folder: docs" in body
    datas = button_datas(callback.message.sent[-1]["reply_markup"])
    assert "fm_get_0" not in datas
    assert {
        "fm_rename_0",
        "fm_copy_0",
        "fm_move_0",
        "fm_delete_0",
        "fm_cancel",
    }.issubset(datas)

    callback = _file_callback(message, "fm_act_1")
    assert "Selected file: note.txt" in texts(callback.message)[-1]
    assert "fm_get_1" in button_datas(callback.message.sent[-1]["reply_markup"])


def test_rename_file_prompt_and_apply(tmp_path, monkeypatch):
    _authorize(monkeypatch)
    source = tmp_path / "old.txt"
    source.write_text("data")
    message = FakeMessage()
    run(features.show_files(message, tmp_path))

    prompt = _file_callback(message, "fm_rename_0")
    body = texts(prompt.message)[-1]
    assert "=== FILE OPERATION ===" in body
    assert "Send a new name for: old.txt" in body
    assert features._FILE_PENDING

    message.text = "new.txt"
    assert run(features.handle_text(message)) is True

    assert not source.exists()
    assert (tmp_path / "new.txt").read_text() == "data"
    assert "Renamed to: new.txt" in texts(message)
    assert features._FILE_PENDING == {}


def test_rename_rejects_invalid_name_and_cancel_clears_prompt(tmp_path, monkeypatch):
    _authorize(monkeypatch)
    source = tmp_path / "old.txt"
    source.write_text("data")
    message = FakeMessage()
    run(features.show_files(message, tmp_path))
    _file_callback(message, "fm_rename_0")

    message.text = "bad/name"
    assert run(features.handle_text(message)) is True

    assert source.exists()
    assert "Rename failed" in texts(message)[-1]
    assert features._FILE_PENDING

    callback = _file_callback(message, "fm_cancel")
    assert callback.answers[-1]["text"] == "Cancelled"
    assert features._FILE_PENDING == {}


def test_mkdir_prompt_creates_folder(tmp_path, monkeypatch):
    _authorize(monkeypatch)
    message = FakeMessage()
    run(features.show_files(message, tmp_path))

    prompt = _file_callback(message, "fm_mkdir")
    assert "Send a name for the new folder" in texts(prompt.message)[-1]

    message.text = "Reports"
    assert run(features.handle_text(message)) is True

    assert (tmp_path / "Reports").is_dir()
    assert any(text.startswith("Created folder:") for text in texts(message))
    assert features._FILE_PENDING == {}


def test_copy_file_to_destination_folder(tmp_path, monkeypatch):
    _authorize(monkeypatch)
    source = tmp_path / "note.txt"
    source.write_text("hello")
    destination = tmp_path / "backup"
    destination.mkdir()
    message = FakeMessage()
    run(features.show_files(message, tmp_path))

    prompt = _file_callback(message, "fm_copy_1")
    assert "Send an existing destination folder for: note.txt" in texts(
        prompt.message
    )[-1]

    message.text = str(destination)
    assert run(features.handle_text(message)) is True

    assert source.read_text() == "hello"
    assert (destination / "note.txt").read_text() == "hello"
    copied = f"Copied to: {destination / 'note.txt'}"
    assert copied in texts(message)
    assert features._FILE_PENDING == {}


def test_move_folder_to_destination_folder(tmp_path, monkeypatch):
    _authorize(monkeypatch)
    source = tmp_path / "docs"
    source.mkdir()
    (source / "a.txt").write_text("x")
    destination = tmp_path / "archive"
    destination.mkdir()
    message = FakeMessage()
    run(features.show_files(message, tmp_path))

    # Entries are folders first: archive (0), docs (1).
    _file_callback(message, "fm_move_1")
    message.text = str(destination)
    assert run(features.handle_text(message)) is True

    assert not source.exists()
    assert (destination / "docs" / "a.txt").read_text() == "x"
    assert f"Moved to: {destination / 'docs'}" in texts(message)


def test_delete_needs_confirmation_and_can_be_cancelled(tmp_path, monkeypatch):
    _authorize(monkeypatch)
    target = tmp_path / "old.txt"
    target.write_text("x")
    message = FakeMessage()
    run(features.show_files(message, tmp_path))

    confirm = _file_callback(message, "fm_delete_0")
    body = texts(confirm.message)[-1]
    assert "=== DELETE ===" in body
    assert "This cannot be undone." in body
    assert button_datas(confirm.message.sent[-1]["reply_markup"]) == [
        "fm_del_yes",
        "fm_del_no",
    ]
    assert target.exists()

    cancelled = _file_callback(message, "fm_del_no")
    assert cancelled.answers[-1]["text"] == "Deletion cancelled"
    assert target.exists()
    assert features._FILE_PENDING == {}

    _file_callback(message, "fm_delete_0")
    done = _file_callback(message, "fm_del_yes")
    assert not target.exists()
    assert texts(done.message)[0].startswith(f"Deleted: {target}")
    assert features._FILE_PENDING == {}


def test_file_prompt_accepts_empty_text_and_expires(tmp_path, monkeypatch):
    _authorize(monkeypatch)
    source = tmp_path / "old.txt"
    source.write_text("data")
    message = FakeMessage()
    run(features.show_files(message, tmp_path))
    _file_callback(message, "fm_rename_0")

    message.text = "   "
    assert run(features.handle_text(message)) is True
    assert "Nothing was sent" in texts(message)[-1]
    assert features._FILE_PENDING

    features._FILE_PENDING[message.chat.id]["expiry"] = 0.0
    message.text = "new.txt"
    assert run(features.handle_text(message)) is False
    assert features._FILE_PENDING == {}
    assert source.exists()


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


def test_net_menu_returns_to_status(monkeypatch):
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
    callback = FakeCallback()

    run(features.handle_callback(callback, "net_menu"))

    assert "=== NETWORK / VPN ===" in texts(callback.message)[-1]


def test_lan_diagnostics_screen_displays_ping_and_dns(monkeypatch):
    monkeypatch.setattr(
        features,
        "collect_lan_diagnostics",
        lambda: {
            "gateway": "192.168.1.1",
            "gateway_ping": {"avg_ms": 1, "loss_percent": 0},
            "internet_ping": {"avg_ms": 12.5, "loss_percent": 0},
            "dns_servers": ["1.1.1.1", "8.8.8.8"],
            "lookups": [
                {
                    "host": "example.com",
                    "ok": True,
                    "address": "93.184.216.34",
                    "seconds": 0.012,
                },
                {"host": "broken.invalid", "ok": False},
            ],
        },
    )
    callback = FakeCallback()

    run(features.handle_callback(callback, "net_lan"))

    body = texts(callback.message)[-1]
    assert "=== LAN DIAGNOSTICS ===" in body
    assert "Gateway: 192.168.1.1" in body
    assert "  Ping: 1 ms, 0% loss" in body
    assert "Internet: 12.5 ms, 0% loss (1.1.1.1)" in body
    assert "DNS servers: 1.1.1.1, 8.8.8.8" in body
    assert "example.com -> 93.184.216.34 (12 ms)" in body
    assert "broken.invalid -> failed" in body
    datas = button_datas(callback.message.sent[-1]["reply_markup"])
    assert {"net_lan", "net_speed", "net_menu"}.issubset(datas)


def test_lan_diagnostics_failure_is_reported(monkeypatch):
    def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(features, "collect_lan_diagnostics", boom)
    callback = FakeCallback()

    run(features.handle_callback(callback, "net_lan"))

    assert texts(callback.message)[-1] == "LAN diagnostics failed: network down"
    datas = button_datas(callback.message.sent[-1]["reply_markup"])
    assert datas[0] == "net_menu"


def test_speedtest_screen_shows_rates_and_latency(monkeypatch):
    monkeypatch.setattr(
        features,
        "run_speedtest",
        lambda: {
            "server": "Cloudflare",
            "download_mbps": 94.25,
            "download_bytes": 118_000_000,
            "download_seconds": 10.0,
            "upload_mbps": 12.34,
            "upload_bytes": 12_000_000,
            "upload_seconds": 8.0,
            "errors": [],
        },
    )
    monkeypatch.setattr(
        features, "ping_host", lambda *a, **k: {"avg_ms": 9, "loss_percent": 0}
    )
    callback = FakeCallback()

    run(features.handle_callback(callback, "net_speed"))

    sent = texts(callback.message)
    assert sent[0] == "Running speed test (up to ~20 seconds)..."
    body = sent[-1]
    assert "=== SPEED TEST ===" in body
    assert "Download: 94.2 Mbps (118.0 MB in 10.0 s)" in body
    assert "Upload: 12.3 Mbps (12.0 MB in 8.0 s)" in body
    assert "Latency: 9 ms, 0% loss" in body
    datas = button_datas(callback.message.sent[-1]["reply_markup"])
    assert {"net_speed", "net_menu"}.issubset(datas)


def test_speedtest_failure_is_reported(monkeypatch):
    def boom():
        raise RuntimeError("offline")

    monkeypatch.setattr(features, "run_speedtest", boom)
    monkeypatch.setattr(
        features, "ping_host", lambda *a, **k: {"avg_ms": None, "loss_percent": 100}
    )
    callback = FakeCallback()

    run(features.handle_callback(callback, "net_speed"))

    assert "Speed test failed: offline" in texts(callback.message)[-1]


def test_traceroute_screen_shows_hops_and_missing_replies(monkeypatch):
    monkeypatch.setattr(
        features,
        "traceroute",
        lambda host: [
            {"hop": 1, "address": "192.168.1.1", "ms": 2.0},
            {"hop": 2, "address": None, "ms": None},
        ],
    )
    callback = FakeCallback()

    run(features.handle_callback(callback, "net_route"))

    sent = texts(callback.message)
    assert sent[0] == "Tracing route to 1.1.1.1..."
    body = sent[-1]
    assert "=== TRACEROUTE ===" in body
    assert "Target: 1.1.1.1" in body
    assert "1  192.168.1.1" in body
    assert "2.0 ms" in body
    assert "no reply" in body


def test_traceroute_empty_and_failure_are_reported(monkeypatch):
    monkeypatch.setattr(features, "traceroute", lambda host: [])
    callback = FakeCallback()
    run(features.handle_callback(callback, "net_route"))
    assert "No hops returned" in texts(callback.message)[-1]

    def boom(host):
        raise RuntimeError("blocked")

    monkeypatch.setattr(features, "traceroute", boom)
    callback = FakeCallback()
    run(features.handle_callback(callback, "net_route"))
    assert "Traceroute failed: blocked" in texts(callback.message)[-1]


def test_network_unknown_action_is_rejected():
    callback = FakeCallback()

    run(features.handle_callback(callback, "net_bogus"))

    assert callback.answers[-1] == {
        "text": "Unknown network action",
        "show_alert": True,
    }
    assert callback.message.sent == []


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