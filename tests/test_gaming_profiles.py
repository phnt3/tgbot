"""Tests for gaming_profiles (Phase 3): config loading, launch aggregation, DB logging.

All dangerous system operations are mocked - no real process is ever launched
and no real power action is triggered from tests.
"""
import os
from pathlib import Path

import pytest

import gaming_profiles
from gaming_profiles import (
    get_profile,
    launch_profile,
    load_profiles_config,
    PROFILES_EXAMPLE_PATH,
)


# ---------- config loading ----------

def test_load_profiles_from_example():
    profiles = load_profiles_config(PROFILES_EXAMPLE_PATH)
    assert set(profiles) == {"GAMING", "VR", "WORK", "NORMAL"}
    assert profiles["GAMING"]["apps"] == ["steam"]
    assert profiles["GAMING"]["optional"] == []
    assert profiles["VR"]["apps"] == ["steam"]


def test_load_profiles_malformed_entries_skipped(tmp_path):
    bad = tmp_path / "profiles.json"
    bad.write_text(
        '{"OK": {"description": "d", "apps": ["steam"]}, "BROKEN": "not-a-dict"}',
        encoding="utf-8",
    )
    profiles = load_profiles_config(bad)
    assert "OK" in profiles and "BROKEN" not in profiles
    assert profiles["OK"]["apps"] == ["steam"]


def test_get_profile_case_insensitive():
    profile = get_profile("gaming")
    assert profile is not None
    assert profile["name"] == "GAMING"
    assert "steam" in profile["apps"]


# ---------- launch aggregation (launch_application is mocked) ----------

@pytest.fixture
def fake_applications(tmp_path, monkeypatch):
    """Point the app allowlist at a controlled fake mapping."""
    import application_manager

    monkeypatch.setattr(application_manager, "load_applications_config", lambda: {
        "steam": r"C:\Fake\steam.exe",
        "discord": r"C:\Fake\discord.lnk",
        "missing": r"C:\Does\Not\Exist\app.exe",
    })


def test_launch_profile_aggregates_started_and_failed(monkeypatch, fake_applications):
    calls = []

    def fake_launch(name, db=None):
        calls.append(name)
        return (True, f"launched {name}")

    monkeypatch.setattr(gaming_profiles, "launch_application", fake_launch)
    monkeypatch.setattr(gaming_profiles, "get_profile", lambda key: {
        "name": "GAMING",
        "description": "test",
        "apps": ["steam", "missing"],
        "optional": ["discord"],
    })

    started, failed = launch_profile("GAMING")

    assert calls == ["steam", "missing", "discord"]  # required first, optional last
    assert [e["name"] for e in started] == ["steam", "missing", "discord"]
    assert failed == []
    assert [e["optional"] for e in started] == [False, False, True]


def test_launch_profile_failure_reported(monkeypatch, fake_applications):
    def fake_launch(name, db=None):
        if name == "missing":
            return (False, "not configured")
        return (True, "ok")

    monkeypatch.setattr(gaming_profiles, "launch_application", fake_launch)
    monkeypatch.setattr(gaming_profiles, "get_profile", lambda key: {
        "name": "VR", "description": "t", "apps": ["steam", "missing"], "optional": [],
    })

    started, failed = launch_profile("VR")
    assert [e["name"] for e in started] == ["steam"]
    assert [e["name"] for e in failed] == ["missing"]
    assert failed[0]["message"] == "not configured"


def test_launch_profile_unknown(monkeypatch):
    monkeypatch.setattr(gaming_profiles, "get_profile", lambda key: None)
    started, failed = launch_profile("WHAT")
    assert started == []
    assert failed and "Unknown profile" in failed[0]["message"]


def test_launch_profile_normal_launches_nothing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("NORMAL must not launch anything")

    monkeypatch.setattr(gaming_profiles, "launch_application", boom)
    started, failed = launch_profile("NORMAL")
    assert started == [] and failed == []


# ---------- launch_application: .lnk support, cwd, DB logging ----------

def test_launch_application_records_db_and_processes_exe(tmp_path, monkeypatch):
    """Real exe launch path created in tmp dir; Popen + DB insert are mocked."""
    import application_manager
    from database import Database

    exe = tmp_path / "myapp.exe"
    exe.write_bytes(b"MZ")

    monkeypatch.setattr(
        application_manager,
        "load_applications_config",
        lambda: {"myapp": str(exe)},
    )

    popen_calls = []
    monkeypatch.setattr("application_manager.subprocess.Popen", lambda args, cwd=None: popen_calls.append((args, cwd)))

    db = Database(tmp_path / "test.db")

    ok, msg = application_manager.launch_application("myapp", db=db)
    assert ok is True and "Successfully" in msg
    assert len(popen_calls) == 1
    args, cwd = popen_calls[0]
    assert args == [str(exe)]
    assert cwd == str(tmp_path)


def test_launch_application_lnk_uses_startfile(tmp_path, monkeypatch):
    import application_manager

    lnk = tmp_path / "shortcut.lnk"
    lnk.write_bytes(b"LNK")

    monkeypatch.setattr(
        application_manager,
        "load_applications_config",
        lambda: {"shortcut": str(lnk)},
    )

    started = []
    monkeypatch.setattr(
        application_manager.os,
        "startfile",
        lambda path: started.append(path),
    )

    ok, msg = application_manager.launch_application("shortcut", db=object())
    assert ok is True
    assert started == [str(lnk)]


def test_launch_application_duplicate_detection(tmp_path, monkeypatch):
    """When the process is already running, nothing is launched and DB says success."""
    import application_manager

    exe = tmp_path / "myapp.exe"
    exe.write_bytes(b"MZ")

    monkeypatch.setattr(
        application_manager,
        "load_applications_config",
        lambda: {"myapp": str(exe)},
    )

    def fake_process_iter(_info):
        class P:
            info = {"name": "myapp.exe"}

        return [P()]

    monkeypatch.setattr(application_manager.psutil, "process_iter", fake_process_iter)

    popen_calls = []
    monkeypatch.setattr("application_manager.subprocess.Popen", lambda *a, **k: popen_calls.append(a))

    ok, msg = application_manager.launch_application("myapp", db=object())
    assert ok is True
    assert "already running" in msg.lower()
    assert popen_calls == []


def test_launch_application_rejects_unknown_name():
    import application_manager

    ok, msg = application_manager.launch_application("totally_unknown_app_xyz")
    assert ok is False
