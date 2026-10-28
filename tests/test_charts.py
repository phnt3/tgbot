"""Tests for Phase 5 chart rendering.

Charts are rendered with matplotlib's non-interactive backend into in-memory
PNGs. All databases live in pytest's temporary directory, so no real history
is read or modified.
"""

import time
from pathlib import Path

import pytest

from charts import Charts, render_chart
from database import Database

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _seed(db: Database, points: int = 6, age: float = 0.0) -> None:
    """Insert a small, recent history sample for every metric."""
    now = time.time() - age
    for i in range(points):
        ts = now - points * 10 + i * 10
        db.insert_cpu_history(20.0 + i, 50.0 + i, 3000.0 + i, timestamp=ts)
        db.insert_gpu_history(
            30.0 + i,
            60.0 + i,
            1500.0,
            25.0,
            2048.0 + i,
            6144.0,
            45.0,
            timestamp=ts,
        )
        db.insert_ram_history(
            16384.0, 8192.0 + i, 8192.0 - i, 50.0 + i, timestamp=ts
        )
        db.insert_disk_history(
            "C:\\", 500000.0, 100000.0, 70.0 + i, timestamp=ts
        )


def test_generate_all_metric_charts(tmp_path):
    db = Database(tmp_path / "charts.db")
    _seed(db)
    charts = Charts(db)

    for metric in ("cpu", "gpu", "ram", "disk"):
        png = charts.generate_chart(metric, "5m")
        assert png is not None, metric
        assert png.startswith(PNG_MAGIC), metric
        assert len(png) > 1000, metric

    db.close()


def test_chart_ranges_include_older_data(tmp_path):
    db = Database(tmp_path / "charts.db")
    # 30 minutes old: outside 5m, inside 1h and 24h
    _seed(db, points=3, age=1800)
    charts = Charts(db)

    assert charts.generate_chart("cpu", "5m") is None
    assert charts.generate_chart("cpu", "1h").startswith(PNG_MAGIC)
    assert charts.generate_chart("cpu", "24h").startswith(PNG_MAGIC)

    db.close()


def test_disk_chart_accepts_explicit_device(tmp_path):
    db = Database(tmp_path / "charts.db")
    now = time.time()
    db.insert_disk_history("C:\\", 500000.0, 100000.0, 40.0, timestamp=now)
    db.insert_disk_history("D:\\", 1000000.0, 500000.0, 50.0, timestamp=now)

    png = Charts(db).generate_disk_chart("5m", device_id="D:\\")
    assert png is not None
    assert png.startswith(PNG_MAGIC)

    db.close()


def test_empty_database_returns_none(tmp_path):
    db = Database(tmp_path / "empty.db")
    charts = Charts(db)

    for metric in ("cpu", "gpu", "ram", "disk"):
        assert charts.generate_chart(metric, "5m") is None

    db.close()


def test_unknown_metric_returns_none(tmp_path):
    db = Database(tmp_path / "charts.db")
    _seed(db)
    assert Charts(db).generate_chart("network", "5m") is None
    db.close()


def test_get_current_status_reads_latest_rows(tmp_path):
    db = Database(tmp_path / "charts.db")
    _seed(db)
    status = Charts(db).get_current_status()

    assert status["cpu_utilization"] == pytest.approx(25.0)
    assert status["cpu_temperature"] == pytest.approx(55.0)
    assert status["ram_utilization_percent"] == pytest.approx(55.0)
    assert status["gpu_vram_total_mb"] == pytest.approx(6144.0)

    db.close()


def test_get_current_status_on_empty_database(tmp_path):
    db = Database(tmp_path / "empty.db")
    assert Charts(db).get_current_status() == {}
    db.close()


def test_render_chart_opens_its_own_connection(tmp_path):
    db_path = tmp_path / "worker.db"
    db = Database(db_path)
    _seed(db)
    db.close()

    png = render_chart(Path(db_path), "cpu", "5m")
    assert png is not None
    assert png.startswith(PNG_MAGIC)

    assert render_chart(Path(db_path), "unknown", "5m") is None


def test_cleanup_prunes_history(tmp_path):
    db = Database(tmp_path / "charts.db")
    _seed(db, points=10)
    charts = Charts(db, retention={"cpu_history": 2})

    charts.cleanup()

    assert len(db.get_cpu_history(limit=100)) == 2
    # Other tables are pruned to the same minimum
    assert len(db.get_gpu_history(limit=100)) == 2
    db.close()


def test_get_disk_devices_lists_distinct_drives(tmp_path):
    db = Database(tmp_path / "charts.db")
    now = time.time()
    db.insert_disk_history("C:\\", 500000.0, 100000.0, 40.0, timestamp=now)
    db.insert_disk_history("D:\\", 1000000.0, 500000.0, 50.0, timestamp=now)
    db.insert_disk_history("D:\\", 1000000.0, 400000.0, 60.0, timestamp=now + 1)
    db.insert_disk_history("", 1.0, 1.0, 0.0, timestamp=now + 2)

    assert db.get_disk_devices() == ["C:\\", "D:\\"]
    db.close()


def test_render_disk_chart_for_explicit_drive(tmp_path):
    db_path = tmp_path / "worker.db"
    db = Database(db_path)
    now = time.time()
    db.insert_disk_history("C:\\", 500000.0, 100000.0, 40.0, timestamp=now)
    db.insert_disk_history("D:\\", 1000000.0, 500000.0, 50.0, timestamp=now)
    db.close()

    png = render_chart(Path(db_path), "disk", "5m", device_id="D:\\")
    assert png is not None
    assert png.startswith(PNG_MAGIC)

    assert render_chart(Path(db_path), "disk", "5m", device_id="Z:\\") is None