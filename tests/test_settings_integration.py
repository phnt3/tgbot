"""Integration tests for database-backed settings.

Every test runs against a temporary database and generic names, so the live
``data/pc_remote.db`` and the machine's personal config files are never read
or modified.
"""

import asyncio
import json
import math
from datetime import time
from types import SimpleNamespace

import pytest

import alerts
import application_manager as am
import background
import gaming_profiles as gp
import notifier
import settings_store as store


class FakeBot:
    """Minimal aiogram Bot replacement that records outgoing messages."""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "settings.db"
    monkeypatch.setattr(store, "default_db_path", lambda: path)
    return path


def _write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


# ---------- loaders prefer the database ----------


def test_application_loader_prefers_database(tmp_path, db_path, monkeypatch):
    store.set_setting(
        store.APPLICATIONS, {"Alpha": r"C:\Games\alpha.exe"}, db_path
    )
    config_file = _write_json(
        tmp_path / "applications.json", {"FromFile": r"C:\Games\file.exe"}
    )
    monkeypatch.setattr(am, "DEFAULT_APPLICATIONS_CONFIG", config_file)

    assert am.load_applications_config() == {"Alpha": r"C:\Games\alpha.exe"}


def test_profile_loader_prefers_database(tmp_path, db_path, monkeypatch):
    store.set_setting(
        store.PROFILES,
        {
            "GAMING": {
                "description": "play",
                "apps": ["Alpha"],
                "optional": [],
            }
        },
        db_path,
    )
    config_file = _write_json(
        tmp_path / "profiles.json",
        {"FROMFILE": {"description": "", "apps": [], "optional": []}},
    )
    monkeypatch.setattr(gp, "PROFILES_CONFIG_PATH", config_file)

    assert gp.load_profiles_config() == {
        "GAMING": {"description": "play", "apps": ["Alpha"], "optional": []}
    }


def test_alert_loader_prefers_database(tmp_path, db_path, monkeypatch):
    store.set_setting(
        store.ALERTS, {"interval_seconds": 90, "thresholds": {}}, db_path
    )
    config_file = _write_json(tmp_path / "alerts.json", {"interval_seconds": 120})
    monkeypatch.setattr(alerts, "ALERT_CONFIG_PATH", config_file)

    assert alerts.load_alert_settings()["interval_seconds"] == 90


def test_notification_loader_prefers_database(db_path):
    store.set_setting(
        store.NOTIFICATIONS, {"battery_warning": 42, "notify_login": False}, db_path
    )

    settings = notifier.load_notification_settings()

    assert settings["battery_warning"] == 42
    assert settings["notify_login"] is False


def test_explicit_files_still_bypass_the_database(tmp_path, db_path):
    store.set_setting(
        store.APPLICATIONS, {"Alpha": r"C:\Games\alpha.exe"}, db_path
    )
    store.set_setting(
        store.PROFILES,
        {"GAMING": {"description": "", "apps": ["Alpha"], "optional": []}},
        db_path,
    )
    store.set_setting(store.ALERTS, {"interval_seconds": 90}, db_path)

    apps_file = _write_json(
        tmp_path / "apps.json", {"FromFile": r"C:\Games\file.exe"}
    )
    profiles_file = _write_json(
        tmp_path / "profiles.json",
        {"FROMFILE": {"description": "", "apps": [], "optional": []}},
    )
    alerts_file = _write_json(tmp_path / "alerts.json", {"interval_seconds": 120})

    assert am.load_applications_config(apps_file) == {
        "FromFile": r"C:\Games\file.exe"
    }
    assert set(gp.load_profiles_config(profiles_file)) == {"FROMFILE"}
    assert alerts.load_alert_settings(alerts_file)["interval_seconds"] == 120


# ---------- quiet hours ----------


def _quiet_settings(start="23:00", end="08:00", enabled=True):
    return {
        "quiet_hours": {"enabled": enabled, "start": start, "end": end},
    }


@pytest.mark.parametrize(
    "now, expected",
    [
        (time(22, 59), False),
        (time(23, 0), True),
        (time(23, 59), True),
        (time(0, 0), True),
        (time(7, 59), True),
        (time(8, 0), False),
        (time(12, 0), False),
    ],
)
def test_quiet_hours_overnight_boundaries(now, expected):
    assert notifier.quiet_hours_active(_quiet_settings(), now) is expected


def test_quiet_hours_same_day_window():
    settings = _quiet_settings(start="09:00", end="17:00")

    assert notifier.quiet_hours_active(settings, time(8, 59)) is False
    assert notifier.quiet_hours_active(settings, time(9, 0)) is True
    assert notifier.quiet_hours_active(settings, time(16, 59)) is True
    assert notifier.quiet_hours_active(settings, time(17, 0)) is False


def test_quiet_hours_disabled_or_empty_window_is_inactive():
    assert notifier.quiet_hours_active(_quiet_settings(enabled=False), time(23, 30)) is False
    # start == end is treated as disabled instead of a 24-hour silence.
    assert notifier.quiet_hours_active(_quiet_settings("08:00", "08:00"), time(8, 0)) is False
    broken = _quiet_settings("not-a-time", "08:00")
    assert notifier.quiet_hours_active(broken, time(23, 30)) is False


def test_send_alert_suppresses_noncritical_during_quiet_hours(
    tmp_path, db_path, monkeypatch
):
    store.set_setting(
        store.ALERTS, {"interval_seconds": 60, "thresholds": {}, "cooldowns": {}}, db_path
    )
    store.set_setting(
        store.NOTIFICATIONS,
        {
            "battery_warning": 20,
            "battery_critical": 10,
            "notify_login": True,
            "quiet_hours": {"enabled": True, "start": "23:00", "end": "08:00"},
        },
        db_path,
    )
    # Freeze the clock inside the quiet window for this process only.
    monkeypatch.setattr(
        notifier,
        "time",
        SimpleNamespace(localtime=lambda: SimpleNamespace(tm_hour=23, tm_min=30)),
    )

    bot = FakeBot()
    monitor = background.BackgroundMonitor(
        bot, db=None, user_ids=[1], interval_seconds=60
    )

    asyncio.run(
        monitor.send_alert({"level": "warning", "category": "cpu", "message": "hot"})
    )
    assert bot.sent == []

    asyncio.run(
        monitor.send_alert(
            {"level": "critical", "category": "cpu", "message": "very hot"}
        )
    )
    assert [uid for uid, _ in bot.sent] == [1]


# ---------- cooldown normalization ----------


def test_zero_cooldown_is_preserved():
    settings = alerts.normalize_alert_settings(
        {"cooldowns": {"cpu_temperature": 0}}
    )

    assert settings["cooldowns"]["cpu_temperature"] == 0


def test_negative_and_nan_cooldowns_fall_back():
    defaults = alerts.DEFAULT_COOLDOWNS

    settings = alerts.normalize_alert_settings(
        {
            "cooldowns": {
                "cpu_temperature": -1,
                "gpu_temperature": math.nan,
                "ram_utilization": "nope",
            }
        }
    )

    assert settings["cooldowns"]["cpu_temperature"] == defaults["cpu_temperature"]
    assert settings["cooldowns"]["gpu_temperature"] == defaults["gpu_temperature"]
    assert settings["cooldowns"]["ram_utilization"] == defaults["ram_utilization"]


def test_infinity_cooldown_is_accepted_as_never():
    settings = alerts.normalize_alert_settings(
        {"cooldowns": {"disk_utilization": math.inf}}
    )

    assert settings["cooldowns"]["disk_utilization"] == math.inf


def test_null_cooldown_still_means_never():
    settings = alerts.normalize_alert_settings(
        {"cooldowns": {"disk_utilization": None}}
    )

    assert settings["cooldowns"]["disk_utilization"] == math.inf


def test_zero_notification_cooldown_is_preserved():
    settings = notifier.normalize_notification_settings(
        {"app_cooldown_minutes": 0, "crash_cooldown_minutes": 0}
    )

    assert settings["app_cooldown_minutes"] == 0
    assert settings["crash_cooldown_minutes"] == 0


def test_export_payload_is_strict_json(db_path):
    store.set_setting(
        store.ALERTS, {"cooldowns": {"disk_utilization": math.inf}}, db_path
    )

    payload = store.export_payload(db_path)

    # allow_nan=False raises when Infinity leaked into the export.
    json.dumps(payload, allow_nan=False)
    assert payload["settings"]["alerts"]["cooldowns"]["disk_utilization"] is None