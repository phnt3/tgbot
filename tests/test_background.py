"""Tests for the Phase 5 background sampler/notifier.

The bot and the metric source are faked; only a temporary SQLite database is
used. A test bug can therefore never send a real Telegram message, touch real
hardware or modify the live database.
"""

import asyncio
import time

import background
from alerts import AlertManager
from background import BackgroundMonitor
from database import Database


class FakeBot:
    """Minimal aiogram Bot replacement that records outgoing messages."""

    def __init__(self, fail_for=()):
        self.sent = []
        self.fail_for = set(fail_for)

    async def send_message(self, chat_id, text):
        if chat_id in self.fail_for:
            raise RuntimeError("user blocked the bot")
        self.sent.append((chat_id, text))


def _metrics(cpu_temp=50.0, gpu_temp=50.0, ram_util=50.0, disk_util=50.0):
    return {
        "hostname": "TEST-PC",
        "uptime": 1234.0,
        "internet": True,
        "cpu": {
            "utilization": 12.5,
            "temperature": cpu_temp,
            "frequency": 3200.0,
        },
        "gpu": {
            "utilization": 20.0,
            "temperature": gpu_temp,
            "frequency": 1500.0,
            "memory_utilization": 10.0,
            "vram_used_mb": 1024.0,
            "vram_total_mb": 6144.0,
            "power_watts": 45.0,
        },
        "ram": {
            "total_mb": 16384.0,
            "used_mb": 8192.0,
            "available_mb": 8192.0,
            "utilization_percent": ram_util,
        },
        "disk": {
            "C:\\": {
                "total_mb": 500000.0,
                "used_mb": 400000.0,
                "available_mb": 100000.0,
                "utilization_percent": disk_util,
                "temperature": "N/A",
            }
        },
    }


def _db(tmp_path) -> Database:
    return Database(tmp_path / "background.db")


def _manager(db: Database) -> AlertManager:
    # High thresholds so unrelated metrics never interfere with a scenario.
    return AlertManager(
        db,
        thresholds={
            "cpu_temperature": 80,
            "gpu_temperature": 80,
            "ram_utilization": 85,
            "disk_utilization": 85,
        },
        cooldowns={"cpu_temperature": 300},
    )


def test_run_once_records_history_and_sends_alerts(tmp_path, monkeypatch):
    db = _db(tmp_path)
    bot = FakeBot()
    monkeypatch.setattr(
        background, "get_all_metrics", lambda: _metrics(cpu_temp=95.0)
    )
    monitor = BackgroundMonitor(
        bot,
        db,
        alert_manager=_manager(db),
        user_ids=[1, 2],
        interval_seconds=60,
    )

    alerts = asyncio.run(monitor.run_once())

    assert len(alerts) == 1
    assert alerts[0]["level"] == "critical"
    assert alerts[0]["category"] == "cpu"

    # One alert is delivered to every configured user
    assert [uid for uid, _ in bot.sent] == [1, 2]
    assert all(text.startswith("[CRITICAL] cpu") for _, text in bot.sent)

    # One row per metric was written
    assert len(db.get_cpu_history(limit=10)) == 1
    assert len(db.get_gpu_history(limit=10)) == 1
    assert len(db.get_ram_history(limit=10)) == 1
    disks = db.get_disk_history(limit=10)
    assert len(disks) == 1
    assert disks[0][1] == "C:\\"
    # "N/A" is stored as NULL, not as text
    assert disks[0][5] is None

    db.close()


def test_run_once_without_breach_sends_nothing(tmp_path, monkeypatch):
    db = _db(tmp_path)
    bot = FakeBot()
    monkeypatch.setattr(background, "get_all_metrics", _metrics)
    monitor = BackgroundMonitor(
        bot, db, alert_manager=_manager(db), user_ids=[1]
    )

    assert asyncio.run(monitor.run_once()) == []
    assert bot.sent == []

    db.close()


def test_send_failure_is_isolated_per_user(tmp_path, monkeypatch):
    db = _db(tmp_path)
    bot = FakeBot(fail_for={1})
    monkeypatch.setattr(
        background, "get_all_metrics", lambda: _metrics(gpu_temp=99.0)
    )
    monitor = BackgroundMonitor(
        bot,
        db,
        alert_manager=_manager(db),
        user_ids=[1, 2],
        interval_seconds=60,
    )

    # A blocked user must not abort the cycle or the second delivery
    alerts = asyncio.run(monitor.run_once())
    assert len(alerts) == 1
    assert [uid for uid, _ in bot.sent] == [2]

    db.close()


def test_history_write_failure_does_not_abort_cycle(tmp_path, monkeypatch):
    db = _db(tmp_path)
    bot = FakeBot()

    def broken_insert(*args, **kwargs):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(db, "insert_ram_history", broken_insert)
    monkeypatch.setattr(background, "get_all_metrics", _metrics)
    monitor = BackgroundMonitor(
        bot, db, alert_manager=_manager(db), user_ids=[1]
    )

    assert asyncio.run(monitor.run_once()) == []

    # Other metrics were still recorded
    assert len(db.get_cpu_history(limit=10)) == 1
    assert len(db.get_ram_history(limit=10)) == 0

    db.close()


def test_prune_runs_on_cycle_boundary(tmp_path, monkeypatch):
    db = _db(tmp_path)
    calls = []
    monkeypatch.setattr(
        db, "prune_history", lambda max_rows=10000: calls.append(max_rows) or 0
    )
    monkeypatch.setattr(background, "get_all_metrics", _metrics)
    monitor = BackgroundMonitor(
        bot=FakeBot(),
        db=db,
        alert_manager=_manager(db),
        user_ids=[],
        interval_seconds=60,
        history_rows=1234,
    )
    monitor._cycles = background.PRUNE_EVERY_CYCLES - 1

    asyncio.run(monitor.run_once())

    assert calls == [1234]
    db.close()


def test_format_alert_is_plain_text():
    text = BackgroundMonitor.format_alert(
        {"level": "warning", "category": "ram", "message": "RAM is high"}
    )
    assert text == "[WARNING] ram\nRAM is high"
    # No Markdown formatting markers: messages are sent as plain text
    assert not any(ch in text for ch in "*_`")


def test_start_and_stop_lifecycle(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(background, "get_all_metrics", _metrics)
    monitor = BackgroundMonitor(
        FakeBot(),
        db,
        alert_manager=_manager(db),
        user_ids=[],
        interval_seconds=3600,
    )

    async def scenario():
        task = monitor.start()
        # start() is idempotent
        assert monitor.start() is task

        await asyncio.sleep(0.05)
        assert not task.done()

        await monitor.stop()
        assert task.done()
        assert task.cancelled()

        # Stopping twice is harmless
        await monitor.stop()

    asyncio.run(scenario())
    db.close()


def test_metric_collection_failure_is_logged_not_fatal(tmp_path, monkeypatch):
    db = _db(tmp_path)

    def fail():
        raise RuntimeError("NVML unavailable")

    monkeypatch.setattr(background, "get_all_metrics", fail)
    monitor = BackgroundMonitor(
        FakeBot(), db, alert_manager=_manager(db), user_ids=[]
    )

    async def scenario():
        started = time.time()
        monitor.interval_seconds = 1
        task = monitor.start()
        # Let the first cycle fail, then give it a moment to reach sleep()
        await asyncio.sleep(0.1)
        assert not task.done()
        await monitor.stop()
        assert time.time() - started < 5

    asyncio.run(scenario())
    db.close()