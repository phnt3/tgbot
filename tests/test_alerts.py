"""Tests for Phase 5 alert evaluation.

The manager is stateful and must alert only on transitions (plus cooldown
repeats). These tests never touch real hardware: metrics are plain dicts.
"""

import json
import time

import pytest

import alerts
from alerts import AlertManager, load_alert_settings
from database import Database


def _metrics(cpu_temp=None, gpu_temp=None, ram_util=None, disks=None):
    return {
        "cpu": {"temperature": cpu_temp},
        "gpu": {"temperature": gpu_temp},
        "ram": {"utilization_percent": ram_util},
        "disk": disks or {},
    }


def _manager(tmp_path, thresholds=None, cooldowns=None) -> AlertManager:
    db = Database(tmp_path / "alerts.db")
    return AlertManager(db, thresholds=thresholds, cooldowns=cooldowns)


def test_breach_alerts_once_then_recovery(tmp_path):
    manager = _manager(tmp_path, cooldowns={"cpu_temperature": 0})

    first = manager.evaluate_metrics(_metrics(cpu_temp=90))
    assert len(first) == 1
    assert first[0]["level"] == "warning"
    assert first[0]["category"] == "cpu"
    assert "too high" in first[0]["message"]

    # Cooldown 0 means repeats are allowed, marked as ongoing
    repeat = manager.evaluate_metrics(_metrics(cpu_temp=90))
    assert len(repeat) == 1
    assert "still high" in repeat[0]["message"]

    recovery = manager.evaluate_metrics(_metrics(cpu_temp=70))
    assert len(recovery) == 1
    assert recovery[0]["level"] == "info"
    assert recovery[0]["category"] == "recovery"

    # Recovery is emitted only once for the transition
    assert manager.evaluate_metrics(_metrics(cpu_temp=70)) == []

    # All three alerts were persisted
    assert len(manager.db.get_recent_alerts(limit=10)) == 3
    manager.db.close()


def test_cooldown_suppresses_repeats(tmp_path):
    manager = _manager(tmp_path, cooldowns={"cpu_temperature": 300})

    assert len(manager.evaluate_metrics(_metrics(cpu_temp=90))) == 1
    assert manager.evaluate_metrics(_metrics(cpu_temp=90)) == []
    assert manager.evaluate_metrics(_metrics(cpu_temp=95)) == []
    manager.db.close()


def test_critical_level_above_margin(tmp_path):
    manager = _manager(tmp_path, thresholds={"cpu_temperature": 80})

    result = manager.evaluate_metrics(_metrics(cpu_temp=91))
    assert len(result) == 1
    assert result[0]["level"] == "critical"

    # Exactly at the threshold is not a breach (strict comparison)
    manager.db.close()
    manager = _manager(tmp_path / "second", thresholds={"gpu_temperature": 80})
    assert manager.evaluate_metrics(_metrics(gpu_temp=80)) == []
    manager.db.close()


def test_unavailable_values_are_ignored(tmp_path):
    manager = _manager(tmp_path)

    for value in (None, "N/A", "unknown"):
        assert manager.evaluate_metrics(_metrics(cpu_temp=value)) == []

    assert manager.db.get_recent_alerts(limit=10) == []
    manager.db.close()


def test_disk_threshold_state_is_per_drive(tmp_path):
    manager = _manager(tmp_path)

    both = manager.evaluate_metrics(
        _metrics(disks={
            "C:\\": {"utilization_percent": 90},
            "D:\\": {"utilization_percent": 90},
        })
    )
    assert len(both) == 2

    # C drops to exactly the threshold, D stays high
    changed = manager.evaluate_metrics(
        _metrics(disks={
            "C:\\": {"utilization_percent": 85},
            "D:\\": {"utilization_percent": 90},
        })
    )
    assert len(changed) == 1
    assert "C:\\" in changed[0]["message"]
    assert changed[0]["category"] == "recovery"

    manager.db.close()


def test_agent_failure_alerts_once_then_recovers(tmp_path, monkeypatch):
    manager = _manager(tmp_path)

    def fail():
        raise RuntimeError("monitor exploded")

    monkeypatch.setattr(alerts, "get_all_metrics", fail)

    first = manager.check_once()
    assert len(first) == 1
    assert first[0]["category"] == "agent"
    assert first[0]["level"] == "warning"

    # A run of failures is not spammed
    assert manager.check_once() == []

    recovered = manager.evaluate_metrics(_metrics(cpu_temp=50))
    assert len(recovered) == 1
    assert recovered[0]["category"] == "recovery"
    assert "reachable" in recovered[0]["message"]

    manager.db.close()


def test_non_dict_metrics_is_treated_as_agent_failure(tmp_path):
    manager = _manager(tmp_path)

    result = manager.evaluate_metrics(None)
    assert len(result) == 1
    assert result[0]["category"] == "agent"

    manager.db.close()


def test_get_recent_alerts_returns_formatted_dicts(tmp_path):
    manager = _manager(tmp_path)
    manager.evaluate_metrics(_metrics(ram_util=95))

    recent = manager.get_recent_alerts(limit=5)
    assert len(recent) == 1
    assert recent[0]["category"] == "ram"
    assert recent[0]["acknowledged"] is False
    # HH:MM:SS string
    assert len(recent[0]["timestamp"]) == 8

    manager.db.close()


def test_load_alert_settings_defaults(tmp_path):
    missing = tmp_path / "missing.json"
    settings = load_alert_settings(missing)
    assert settings["interval_seconds"] == alerts.DEFAULT_INTERVAL_SECONDS
    assert settings["thresholds"] == alerts.DEFAULT_THRESHOLDS
    assert settings["cooldowns"] == alerts.DEFAULT_COOLDOWNS


def test_load_alert_settings_invalid_json_falls_back(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    settings = load_alert_settings(path)
    assert settings["thresholds"] == alerts.DEFAULT_THRESHOLDS
    assert settings["interval_seconds"] == alerts.DEFAULT_INTERVAL_SECONDS


def test_load_alert_settings_merges_and_clamps(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text(
        json.dumps({
            "interval_seconds": 1,
            "thresholds": {
                "cpu_temperature": 70,
                "gpu_temperature": -5,
                "bogus": 10,
            },
            "cooldowns": {"ram_utilization": 120},
            "unknown_section": True,
        }),
        encoding="utf-8",
    )

    settings = load_alert_settings(path)
    assert settings["interval_seconds"] == alerts.MIN_INTERVAL_SECONDS
    assert settings["thresholds"]["cpu_temperature"] == 70
    # Invalid values and unknown keys keep their defaults
    assert settings["thresholds"]["gpu_temperature"] == (
        alerts.DEFAULT_THRESHOLDS["gpu_temperature"]
    )
    assert "bogus" not in settings["thresholds"]
    assert settings["cooldowns"]["ram_utilization"] == 120


def test_load_alert_settings_clamps_max_interval(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps({"interval_seconds": 10**9}), encoding="utf-8")
    settings = load_alert_settings(path)
    assert settings["interval_seconds"] == alerts.MAX_INTERVAL_SECONDS


def test_alert_timestamps_use_current_time(tmp_path):
    manager = _manager(tmp_path)
    before = time.time()
    alert = manager.evaluate_metrics(_metrics(cpu_temp=90))[0]
    after = time.time()
    assert before <= alert["timestamp"] <= after
    manager.db.close()