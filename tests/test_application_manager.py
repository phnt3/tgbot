"""Tests for the application allowlist and the safe launcher.

Safety net: by default ``subprocess.Popen`` and ``os.startfile`` are patched to
raise, so a test can never start a real program. Tests that exercise successful
launches install their own recorder instead.
"""

import json

import pytest

import application_manager as am


class FakeDB:
    def __init__(self, fail=False):
        self.fail = fail
        self.launches = []

    def insert_app_launch(self, application_name, success=1, error_message=None):
        if self.fail:
            raise RuntimeError("database unavailable")
        self.launches.append((application_name, success, error_message))


def _forbid_launch(*args, **kwargs):
    raise AssertionError("test attempted to launch a real process")


@pytest.fixture(autouse=True)
def safe_process_environment(monkeypatch):
    """Stub out every side effect of a launch."""
    monkeypatch.setattr(am.psutil, "process_iter", lambda attrs=None: [])
    monkeypatch.setattr(am.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(am.subprocess, "Popen", _forbid_launch)
    monkeypatch.setattr(am.os, "startfile", _forbid_launch, raising=False)


def _write_config(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------- allowlist loading ----------

def test_load_applications_filters_invalid_entries(tmp_path):
    path = _write_config(
        tmp_path / "applications.json",
        {
            "GoodApp": r"C:\Apps\good.exe",
            "Visual Studio Code": r"C:\Program Files\Microsoft VS Code\Code.exe",
            "Bracket Game": r"Q:\Games\Example [Build 1]\game.exe",
            "Bad;Name": r"C:\Apps\bad.exe",
            "Traversal": r"C:\Apps\..\evil.exe",
            "Shell": r"C:\Apps\app.exe & calc",
        },
    )

    loaded = am.load_applications_config(path)

    assert loaded == {
        "GoodApp": r"C:\Apps\good.exe",
        "Visual Studio Code": r"C:\Program Files\Microsoft VS Code\Code.exe",
        "Bracket Game": r"Q:\Games\Example [Build 1]\game.exe",
    }


def test_load_applications_missing_file_returns_empty(tmp_path):
    assert am.load_applications_config(tmp_path / "missing.json") == {}


def test_load_applications_malformed_json_returns_empty(tmp_path):
    path = tmp_path / "applications.json"
    path.write_text("{not valid json", encoding="utf-8")

    assert am.load_applications_config(path) == {}


def test_load_applications_non_dict_json_returns_empty(tmp_path):
    path = tmp_path / "applications.json"
    path.write_text("[]", encoding="utf-8")

    assert am.load_applications_config(path) == {}


def test_load_applications_falls_back_to_example(tmp_path, monkeypatch):
    fallback = _write_config(
        tmp_path / "applications.example.json",
        {"ExampleApp": r"C:\Apps\example.exe"},
    )
    monkeypatch.setattr(
        am, "DEFAULT_APPLICATIONS_CONFIG", tmp_path / "does-not-exist.json"
    )
    monkeypatch.setattr(am, "FALLBACK_APPLICATIONS_CONFIG", fallback)
    # Keep the test away from the live database.
    monkeypatch.setattr(am, "_stored_applications", lambda: None)

    assert am.load_applications_config() == {
        "ExampleApp": r"C:\Apps\example.exe"
    }


# ---------- path lookup ----------

def test_find_application_exact_match(tmp_path):
    exe = tmp_path / "app.exe"
    exe.write_bytes(b"MZ")

    assert am.find_application_path("myapp", {"myapp": str(exe)}) == str(exe)


def test_find_application_case_insensitive_match(tmp_path):
    exe = tmp_path / "app.exe"
    exe.write_bytes(b"MZ")

    assert am.find_application_path("MyApp", {"myapp": str(exe)}) == str(exe)
    assert am.find_application_path("MYAPP", {"myapp": str(exe)}) == str(exe)


def test_find_application_unknown_or_missing_path(tmp_path):
    missing = tmp_path / "missing.exe"
    exe = tmp_path / "app.exe"
    exe.write_bytes(b"MZ")

    assert am.find_application_path("other", {"myapp": str(exe)}) is None
    assert am.find_application_path("myapp", {"myapp": str(missing)}) is None


def test_find_application_custom_config_is_not_shared(tmp_path):
    exe = tmp_path / "app.exe"
    exe.write_bytes(b"MZ")

    am.find_application_path("myapp", {"myapp": str(exe)})

    # An empty custom mapping must not fall back to the real allowlist
    assert am.find_application_path("myapp", {"other": str(exe)}) is None


# ---------- launching ----------

def test_launch_rejects_invalid_application_name():
    ok, message = am.launch_application("app;calc")

    assert ok is False
    assert "Invalid application name" in message


def test_launch_rejects_raw_path_as_application_name():
    ok, message = am.launch_application(r"C:\Windows\notepad.exe")

    assert ok is False
    assert "Invalid application name" in message


def test_launch_unknown_application(tmp_path):
    ok, message = am.launch_application("unknown", {"other": str(tmp_path)})

    assert ok is False
    assert "not configured or not found" in message


def test_launch_rejects_unsafe_path(tmp_path):
    # ';' is legal in a Windows filename but rejected by the path validator
    exe = tmp_path / "bad;app.exe"
    exe.write_bytes(b"MZ")

    ok, message = am.launch_application("BadApp", {"BadApp": str(exe)})

    assert ok is False
    assert "safety validation" in message


def test_launch_exe_success_records_event(tmp_path, monkeypatch):
    exe = tmp_path / "myapp.exe"
    exe.write_bytes(b"MZ")
    calls = []
    monkeypatch.setattr(
        am.subprocess,
        "Popen",
        lambda args, cwd=None: calls.append((args, cwd)),
    )
    db = FakeDB()

    ok, message = am.launch_application("myapp", {"myapp": str(exe)}, db=db)

    assert ok is True
    assert "Successfully launched" in message
    assert calls == [([str(exe)], str(tmp_path))]
    assert db.launches == [("myapp", 1, None)]


def test_launch_lnk_success_uses_startfile(tmp_path, monkeypatch):
    lnk = tmp_path / "shortcut.lnk"
    lnk.write_bytes(b"LNK")
    started = []
    monkeypatch.setattr(am.os, "startfile", lambda path: started.append(path))
    db = FakeDB()

    ok, message = am.launch_application("shortcut", {"shortcut": str(lnk)}, db=db)

    assert ok is True
    assert "Successfully launched" in message
    assert started == [str(lnk)]
    assert db.launches == [("shortcut", 1, None)]


def test_launch_already_running_skips_popen(tmp_path, monkeypatch):
    exe = tmp_path / "myapp.exe"
    exe.write_bytes(b"MZ")

    class RunningProcess:
        info = {"name": "myapp.exe"}

    # The autouse fixture would raise if Popen were reached; still override
    # process_iter to report the app as already running.
    monkeypatch.setattr(
        am.psutil, "process_iter", lambda attrs=None: [RunningProcess()]
    )
    db = FakeDB()

    ok, message = am.launch_application("myapp", {"myapp": str(exe)}, db=db)

    assert ok is True
    assert "already running" in message
    assert db.launches == []


def test_launch_duplicate_detection_ignores_psutil_errors(tmp_path, monkeypatch):
    exe = tmp_path / "myapp.exe"
    exe.write_bytes(b"MZ")

    def broken_process_iter(attrs=None):
        raise am.psutil.AccessDenied(pid=1)

    monkeypatch.setattr(am.psutil, "process_iter", broken_process_iter)
    calls = []
    monkeypatch.setattr(
        am.subprocess, "Popen", lambda args, cwd=None: calls.append(args)
    )

    ok, _ = am.launch_application("myapp", {"myapp": str(exe)}, db=FakeDB())

    assert ok is True
    assert calls == [[str(exe)]]


def test_launch_permission_error_is_reported_and_logged(tmp_path, monkeypatch):
    exe = tmp_path / "myapp.exe"
    exe.write_bytes(b"MZ")

    def denied(*args, **kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr(am.subprocess, "Popen", denied)
    db = FakeDB()

    ok, message = am.launch_application("myapp", {"myapp": str(exe)}, db=db)

    assert ok is False
    assert "Permission denied" in message
    assert db.launches == [("myapp", 0, "Permission denied")]


def test_launch_missing_executable_error_is_reported_and_logged(
    tmp_path, monkeypatch
):
    exe = tmp_path / "myapp.exe"
    exe.write_bytes(b"MZ")

    def missing(*args, **kwargs):
        raise FileNotFoundError("gone")

    monkeypatch.setattr(am.subprocess, "Popen", missing)
    db = FakeDB()

    ok, message = am.launch_application("myapp", {"myapp": str(exe)}, db=db)

    assert ok is False
    assert "not found" in message
    assert db.launches == [("myapp", 0, "Executable not found")]


def test_launch_generic_error_is_reported_and_logged(tmp_path, monkeypatch):
    exe = tmp_path / "myapp.exe"
    exe.write_bytes(b"MZ")

    def broken(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(am.subprocess, "Popen", broken)
    db = FakeDB()

    ok, message = am.launch_application("myapp", {"myapp": str(exe)}, db=db)

    assert ok is False
    assert "Failed to launch" in message
    assert db.launches == [("myapp", 0, "unexpected")]


def test_launch_succeeds_when_db_logging_fails(tmp_path, monkeypatch):
    exe = tmp_path / "myapp.exe"
    exe.write_bytes(b"MZ")
    calls = []
    monkeypatch.setattr(
        am.subprocess, "Popen", lambda args, cwd=None: calls.append(args)
    )

    ok, message = am.launch_application(
        "myapp", {"myapp": str(exe)}, db=FakeDB(fail=True)
    )

    assert ok is True
    assert "Successfully launched" in message
    assert calls == [[str(exe)]]


# ---------- profile launcher wrapper ----------

def test_launch_gaming_profile_reports_started_and_failed(monkeypatch):
    import gaming_profiles

    def fake_launch_profile(profile_name, db=None):
        return (
            [{"name": "steam", "ok": True, "message": "", "optional": False}],
            [{"name": "discord", "ok": False, "message": "x", "optional": True}],
        )

    monkeypatch.setattr(gaming_profiles, "launch_profile", fake_launch_profile)

    ok, message = am.launch_gaming_profile("GAMING")

    assert ok is True
    assert "Started: steam" in message
    assert "Failed: discord" in message


def test_launch_gaming_profile_all_started(monkeypatch):
    import gaming_profiles

    monkeypatch.setattr(
        gaming_profiles,
        "launch_profile",
        lambda profile_name, db=None: (
            [{"name": "steam", "ok": True, "message": "", "optional": False}],
            [],
        ),
    )

    ok, message = am.launch_gaming_profile("GAMING")

    assert ok is True
    assert "Started: steam" in message
    assert "Failed" not in message