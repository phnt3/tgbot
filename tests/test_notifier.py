"""Tests for PC -> Telegram event notifications.

psutil, the process list and the clock are faked so every scenario is
deterministic and no real session, battery or application is involved.
"""

import notifier
from notifier import Notifier


class FakeUser:
    def __init__(self, name="alice", host="desktop", started=1700000000):
        self.name = name
        self.host = host
        self.started = started


class FakeBattery:
    def __init__(self, percent, plugged=False):
        self.percent = percent
        self.power_plugged = plugged


class FakeClock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _collect(items):
    """Flatten poll results into (category, message) pairs."""
    return [(item["category"], item["message"]) for item in items]


def test_first_poll_only_baselines_sessions(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(notifier.psutil, "users", lambda: [FakeUser()])
    monkeypatch.setattr(notifier.psutil, "sensors_battery", lambda: None)
    monkeypatch.setattr(notifier, "_running_executables", lambda: set())

    watcher = Notifier(watched_apps=[], clock=clock)

    assert watcher.poll() == []
    assert watcher.poll() == []


def test_new_login_session_is_reported(monkeypatch):
    sessions = [FakeUser()]
    monkeypatch.setattr(notifier.psutil, "users", lambda: list(sessions))
    monkeypatch.setattr(notifier.psutil, "sensors_battery", lambda: None)
    monkeypatch.setattr(notifier, "_running_executables", lambda: set())

    watcher = Notifier(watched_apps=[], clock=FakeClock())
    watcher.poll()

    sessions.append(FakeUser(name="bob", host="rdp-host", started=0))
    events = watcher.poll()

    assert len(events) == 1
    assert events[0]["category"] == "login"
    assert events[0]["level"] == "info"
    assert "bob" in events[0]["message"]
    assert "rdp-host" in events[0]["message"]


def test_battery_warning_fires_once_then_critical(monkeypatch):
    battery = {"value": FakeBattery(15)}
    monkeypatch.setattr(notifier.psutil, "users", lambda: [])
    monkeypatch.setattr(
        notifier.psutil, "sensors_battery", lambda: battery["value"]
    )
    monkeypatch.setattr(notifier, "_running_executables", lambda: set())

    watcher = Notifier(watched_apps=[], clock=FakeClock())

    warning = watcher.poll()
    assert len(warning) == 1
    assert warning[0]["level"] == "warning"
    assert warning[0]["category"] == "battery"
    assert "15%" in warning[0]["message"]

    assert watcher.poll() == []

    battery["value"] = FakeBattery(8)
    critical = watcher.poll()
    assert len(critical) == 1
    assert critical[0]["level"] == "critical"
    assert "8%" in critical[0]["message"]


def test_battery_rearms_after_charging(monkeypatch):
    battery = {"value": FakeBattery(15)}
    monkeypatch.setattr(notifier.psutil, "users", lambda: [])
    monkeypatch.setattr(
        notifier.psutil, "sensors_battery", lambda: battery["value"]
    )
    monkeypatch.setattr(notifier, "_running_executables", lambda: set())

    watcher = Notifier(watched_apps=[], clock=FakeClock())
    assert len(watcher.poll()) == 1

    battery["value"] = FakeBattery(100, plugged=True)
    assert watcher.poll() == []

    battery["value"] = FakeBattery(15)
    assert len(watcher.poll()) == 1


def test_battery_missing_or_above_threshold_is_silent(monkeypatch):
    battery = {"value": None}
    monkeypatch.setattr(notifier.psutil, "users", lambda: [])
    monkeypatch.setattr(
        notifier.psutil, "sensors_battery", lambda: battery["value"]
    )
    monkeypatch.setattr(notifier, "_running_executables", lambda: set())

    watcher = Notifier(watched_apps=[], clock=FakeClock())
    assert watcher.poll() == []

    battery["value"] = FakeBattery(80)
    assert watcher.poll() == []


def test_watched_application_exit_is_reported_once(monkeypatch):
    clock = FakeClock()
    processes = {"game.exe", "explorer.exe"}
    monkeypatch.setattr(notifier.psutil, "users", lambda: [])
    monkeypatch.setattr(notifier.psutil, "sensors_battery", lambda: None)
    monkeypatch.setattr(notifier, "_running_executables", lambda: set(processes))

    watcher = Notifier(
        watched_apps=["Game.exe"], app_cooldown_seconds=300, clock=clock
    )
    assert watcher.poll() == []

    processes.discard("game.exe")
    events = watcher.poll()

    assert len(events) == 1
    assert events[0]["category"] == "application"
    assert "game.exe" in events[0]["message"]
    assert watcher.poll() == []

    # Relaunch and exit again immediately: still inside the cooldown.
    processes.add("game.exe")
    assert watcher.poll() == []
    processes.discard("game.exe")
    assert watcher.poll() == []

    # After the cooldown the next exit is reported again.
    clock.advance(301)
    processes.add("game.exe")
    watcher.poll()
    processes.discard("game.exe")
    assert len(watcher.poll()) == 1


def test_crash_proxy_reports_once_per_outbreak(monkeypatch):
    clock = FakeClock()
    processes = {"explorer.exe"}
    monkeypatch.setattr(notifier.psutil, "users", lambda: [])
    monkeypatch.setattr(notifier.psutil, "sensors_battery", lambda: None)
    monkeypatch.setattr(notifier, "_running_executables", lambda: set(processes))

    watcher = Notifier(watched_apps=[], clock=clock)
    watcher.poll()

    processes.add("werfault.exe")
    events = watcher.poll()
    assert len(events) == 1
    assert events[0]["category"] == "crash"

    # Still running: stay quiet.
    assert watcher.poll() == []

    # WerFault disappears and appears again inside the cooldown: stay quiet.
    processes.discard("werfault.exe")
    watcher.poll()
    processes.add("werfault.exe")
    assert watcher.poll() == []

    # After the cooldown a new outbreak is reported again.
    processes.discard("werfault.exe")
    watcher.poll()
    clock.advance(601)
    processes.add("werfault.exe")
    assert len(watcher.poll()) == 1


def test_process_enumeration_failure_is_contained(monkeypatch):
    monkeypatch.setattr(notifier.psutil, "users", lambda: [])
    monkeypatch.setattr(notifier.psutil, "sensors_battery", lambda: None)

    def boom():
        raise RuntimeError("process list unavailable")

    monkeypatch.setattr(notifier, "_running_executables", boom)

    watcher = Notifier(watched_apps=["game.exe"], clock=FakeClock())
    assert watcher.poll() == []