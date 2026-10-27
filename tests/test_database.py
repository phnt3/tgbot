"""Tests for Phase 5 database additions: history tables, filters and pruning."""

import time

import pytest

from database import Database


def _db(tmp_path) -> Database:
    return Database(tmp_path / "test.db")


def test_history_insert_and_since_filter(tmp_path):
    db = _db(tmp_path)
    now = time.time()
    old = now - 10_000

    db.insert_cpu_history(10.0, 50.0, 3000.0, timestamp=old)
    db.insert_cpu_history(20.0, 55.0, 3100.0, timestamp=now)

    all_rows = db.get_cpu_history(limit=10)
    assert len(all_rows) == 2
    # Query returns newest first
    assert all_rows[0][0] == pytest.approx(now)
    assert all_rows[0][1] == 20.0

    recent = db.get_cpu_history(limit=10, since=now - 100)
    assert len(recent) == 1
    assert recent[0][1] == 20.0

    db.close()


def test_gpu_and_ram_history_roundtrip(tmp_path):
    db = _db(tmp_path)
    now = time.time()

    db.insert_gpu_history(
        30.0, 60.0, 1500.0, 25.0, 2048.0, 6144.0, 45.0, timestamp=now
    )
    db.insert_ram_history(16384.0, 8192.0, 8192.0, 50.0, timestamp=now)

    gpu = db.get_gpu_history(limit=1)[0]
    assert gpu[0] == pytest.approx(now)
    assert gpu[1:8] == (30.0, 60.0, 1500.0, 25.0, 2048.0, 6144.0, 45.0)

    ram = db.get_ram_history(limit=1)[0]
    assert ram[1:] == (16384.0, 8192.0, 8192.0, 50.0)

    db.close()


def test_disk_history_device_filter(tmp_path):
    db = _db(tmp_path)
    now = time.time()

    db.insert_disk_history("C:\\", 500000.0, 100000.0, 80.0, None, timestamp=now)
    db.insert_disk_history("D:\\", 1000000.0, 900000.0, 10.0, None, timestamp=now)

    all_rows = db.get_disk_history(limit=10)
    assert len(all_rows) == 2

    c_rows = db.get_disk_history(limit=10, device_id="C:\\")
    assert len(c_rows) == 1
    assert c_rows[0][1] == "C:\\"
    assert c_rows[0][4] == 80.0

    # The since filter combines with the device filter
    assert db.get_disk_history(limit=10, since=now + 60, device_id="C:\\") == []

    db.close()


def test_prune_history_keeps_newest_rows_per_table(tmp_path):
    db = _db(tmp_path)
    now = time.time()

    for i in range(10):
        ts = now - 100 + i
        db.insert_cpu_history(float(i), timestamp=ts)
        db.insert_gpu_history(float(i), timestamp=ts)
        db.insert_ram_history(16000.0, float(i), 16000.0 - i, 50.0, timestamp=ts)
        db.insert_disk_history("C:\\", 500000.0, float(i), float(i), timestamp=ts)

    deleted = db.prune_history(max_rows=3)
    # 7 removed from each of the four tables
    assert deleted == 28

    for getter in (
        db.get_cpu_history,
        db.get_gpu_history,
        db.get_ram_history,
        db.get_disk_history,
    ):
        rows = getter(limit=10)
        assert len(rows) == 3
        # Newest timestamps survive
        assert rows[0][0] > rows[1][0] > rows[2][0]

    db.close()


def test_alert_history_and_acknowledged_filter(tmp_path):
    db = _db(tmp_path)
    db.insert_alert("warning", "cpu", "CPU hot")
    db.insert_alert("info", "recovery", "CPU recovered", acknowledged=1)

    rows = db.get_recent_alerts(limit=10)
    assert len(rows) == 2
    assert {row[2] for row in rows} == {"cpu", "recovery"}

    acked = db.get_recent_alerts(limit=10, acknowledged_only=True)
    assert len(acked) == 1
    assert acked[0][2] == "recovery"
    assert acked[0][4] == 1

    db.close()


def test_closed_database_rejects_writes(tmp_path):
    db = _db(tmp_path)
    db.close()

    with pytest.raises(RuntimeError):
        db.insert_cpu_history(10.0)