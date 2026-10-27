"""Handler-level tests for the feature screens in ``handlers.features``.

Everything external is faked: disk/network measurements, media keys, the
scheduler executor, Telegram downloads and the uploads folder. No real system
folder, power action or network request is involved.
"""

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


# --- Disk screens ----------------------------------------------------------


def test_disk_menu_lists_disks_and_actions(monkeypatch):
    monkeypatch.setattr(
        features,
        "get_disk_overview",
        lambda: [
            {
                "device": "C:\\",
                "mountpoint": "C:\\",
                "fstype": "NTFS",
                "total_bytes": 100 * 1024 ** 3,
                "used_bytes": 80 * 1024 ** 3,
                "free_bytes": 20 * 1024 ** 3,
                "percent": 80.0,
            }
        ],
    )
    message = FakeMessage()

    run(features.show_disk_menu(message))

    body = texts(message)[0]
    assert "DISK SPACE" in body
    assert "C:\\ (NTFS)" in body
    assert "20.0 GB free of 100.0 GB (80.0% used)" in body
    assert set(button_datas(message.sent[0]["reply_markup"])) == {
        "dsk_analyze",
        "dsk_clean",
        "dsk_bin",
        "menu_main",
    }


def test_disk_menu_handles_no_disks(monkeypatch):
    monkeypatch.setattr(features, "get_disk_overview", lambda: [])
    message = FakeMessage()

    run(features.show_disk_menu(message))

    assert "No fixed disks detected." in texts(message)[0]


def test_analyze_disk_totals_only_cleanable_targets(monkeypatch):
    monkeypatch.setattr(
        features,
        "scan_cleanup_targets",
        lambda: [
            {
                "name": "Temp",
                "size_bytes": 2048,
                "files": 4,
                "exists": True,
                "cleanable": True,
                "partial": False,
            },
            {
                "name": "Windows Update",
                "size_bytes": 999999,
                "files": 1,
                "exists": True,
                "cleanable": False,
                "partial": True,
            },
            {
                "name": "Missing",
                "size_bytes": 0,
                "files": 0,
                "exists": False,
                "cleanable": True,
                "partial": False,
            },
        ],
    )
    message = FakeMessage()

    run(features.analyze_disk(message))

    body = texts(message)[-1]
    assert "Temp: 2.0 KB in 4 file(s)" in body
    assert "Windows Update" in body and "[report only]" in body
    assert "Missing: not found" in body
    assert "Cleanable now: 2.0 KB" in body
    assert "too large to scan fully" in body


def test_cleanup_requires_and_consumes_confirmation(monkeypatch):
    executed = []
    monkeypatch.setattr(
        features,
        "clean_targets",
        lambda: executed.append(True)
        or {
            "freed_bytes": 5 * 1024 ** 2,
            "removed_files": 12,
            "errors": 1,
            "cleaned": ["Temp"],
            "skipped": ["Windows Update (report only)"],
        },
    )
    message = FakeMessage()

    run(features.request_clean(message))
    assert features.CLEAN_CONFIRM_KEY in _PENDING_CONFIRMATIONS
    assert "dsk_clean_yes" in button_datas(message.sent[-1]["reply_markup"])

    run(features.confirm_clean(message))
    body = texts(message)[-1]
    assert executed == [True]
    assert "Freed: 5.0 MB" in body
    assert "Removed files: 12" in body
    assert "Errors: 1" in body
    assert "Cleaned: Temp" in body
    assert "Skipped: Windows Update (report only)" in body

    # A second tap on the same confirm button does nothing.
    run(features.confirm_clean(message))
    assert executed == [True]
    assert "confirmation expired" in texts(message)[-1].lower()


def test_cancel_cleanup_clears_the_confirmation(monkeypatch):
    called = []
    monkeypatch.setattr(features, "clean_targets", lambda: called.append(True) or {})
    callback = FakeCallback()

    run(features.handle_callback(callback, "dsk_clean"))
    assert features.CLEAN_CONFIRM_KEY in _PENDING_CONFIRMATIONS

    run(features.handle_callback(callback, "dsk_clean_no"))

    assert features.CLEAN_CONFIRM_KEY not in _PENDING_CONFIRMATIONS
    assert called == []
    assert callback.answers[-1]["text"] == "Cleanup cancelled"


def test_recycle_bin_size_and_empty_flow(monkeypatch):
    monkeypatch.setattr(features, "get_recycle_bin_size", lambda: 3 * 1024 ** 2)
    emptied = []
    monkeypatch.setattr(
        features,
        "empty_recycle_bin",
        lambda: emptied.append(True) or (True, "Recycle Bin emptied."),
    )
    message = FakeMessage()

    run(features.show_recycle_bin(message))
    assert "Current size: 3.0 MB" in texts(message)[-1]

    run(features.request_empty_bin(message))
    assert features.RECYCLE_CONFIRM_KEY in _PENDING_CONFIRMATIONS

    run(features.confirm_empty_bin(message))
    assert emptied == [True]
    assert texts(message)[-1] == "[DONE]\nRecycle Bin emptied."


def test_recycle_bin_reports_failure_and_expired_confirmation(monkeypatch):
    monkeypatch.setattr(features, "empty_recycle_bin", lambda: (False, "access denied"))
    message = FakeMessage()

    run(features.confirm_empty_bin(message))
    assert "expired" in texts(message)[-1].lower()

    features._request_confirmation(features.RECYCLE_CONFIRM_KEY)
    run(features.confirm_empty_bin(message))
    assert texts(message)[-1] == "[FAILED]\naccess denied"