"""Tests for the database-backed settings store.

Every call passes an explicit temporary database path, so these tests can
never read or write the live ``data/pc_remote.db``.
"""

import pytest

import settings_store as store


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "settings.db"


@pytest.fixture
def seeds(monkeypatch):
    """Deterministic defaults instead of the machine's config files."""
    values = {
        store.APPLICATIONS: {"Alpha": r"C:\Games\alpha.exe"},
        store.PROFILES: {
            "GAMING": {"description": "play", "apps": ["Alpha"], "optional": []}
        },
        store.ALERTS: {
            "thresholds": {"cpu_temperature": 90},
            "cooldowns": {"cpu_temperature": 300},
            "interval_seconds": 60,
        },
        store.NOTIFICATIONS: {"battery_warning": 20, "notify_login": True},
    }
    monkeypatch.setattr(
        store, "_SEEDS", {key: (lambda value=value: dict(value)) for key, value in values.items()}
    )
    return values


def test_get_setting_returns_default_when_absent(db_path):
    assert store.get_setting("applications", "fallback", db_path) == "fallback"
    assert store.get_setting("applications", store.MISSING, db_path) is store.MISSING


def test_set_and_get_round_trip_nested_values(db_path):
    value = {
        "Alpha": r"C:\Games\alpha.exe",
        "Notice": "Привет",
        "nested": {"list": [1, 2, None], "flag": False},
    }
    store.set_setting(store.APPLICATIONS, value, db_path)

    assert store.get_setting(store.APPLICATIONS, store.MISSING, db_path) == value


def test_set_setting_overwrites_existing_section(db_path):
    store.set_setting(store.NOTIFICATIONS, {"battery_warning": 20}, db_path)
    store.set_setting(store.NOTIFICATIONS, {"battery_warning": 35}, db_path)

    assert store.get_setting(store.NOTIFICATIONS, store.MISSING, db_path) == {
        "battery_warning": 35
    }


def test_none_is_stored_and_distinguished_from_missing(db_path):
    store.set_setting(store.ALERTS, None, db_path)

    assert store.get_setting(store.ALERTS, store.MISSING, db_path) is None


def test_delete_setting_removes_only_that_section(db_path):
    store.set_setting(store.ALERTS, {"interval_seconds": 60}, db_path)
    store.set_setting(store.NOTIFICATIONS, {"notify_login": True}, db_path)

    store.delete_setting(store.ALERTS, db_path)

    assert store.get_setting(store.ALERTS, store.MISSING, db_path) is store.MISSING
    assert store.get_setting(store.NOTIFICATIONS, store.MISSING, db_path) == {
        "notify_login": True
    }


def test_all_settings_returns_every_stored_section(db_path):
    store.set_setting("custom", {"a": 1}, db_path)
    store.set_setting(store.PROFILES, {}, db_path)

    assert store.all_settings(db_path) == {"custom": {"a": 1}, store.PROFILES: {}}


def test_get_or_seed_creates_and_then_reuses_value(db_path):
    calls = []

    def seed():
        calls.append(True)
        return {"created": True}

    assert store.get_or_seed(store.APPLICATIONS, seed, db_path) == {"created": True}
    assert store.get_or_seed(store.APPLICATIONS, seed, db_path) == {"created": True}
    assert calls == [True]


def test_get_or_seed_keeps_existing_section(db_path):
    store.set_setting(store.APPLICATIONS, {"Alpha": r"C:\Games\alpha.exe"}, db_path)

    def seed():  # pragma: no cover - must not run
        raise AssertionError("seed should not run")

    assert store.get_or_seed(store.APPLICATIONS, seed, db_path) == {
        "Alpha": r"C:\Games\alpha.exe"
    }


def test_ensure_defaults_seeds_missing_sections_once(db_path, seeds):
    created = store.ensure_defaults(db_path)

    assert created == {section: True for section in store.SECTIONS}
    assert store.all_settings(db_path) == seeds
    assert store.ensure_defaults(db_path) == {}


def test_ensure_defaults_keeps_user_changes(db_path, seeds):
    store.set_setting(store.NOTIFICATIONS, {"battery_warning": 35}, db_path)

    created = store.ensure_defaults(db_path)

    assert store.NOTIFICATIONS not in created
    assert store.get_setting(store.NOTIFICATIONS, store.MISSING, db_path) == {
        "battery_warning": 35
    }


def test_reset_defaults_replaces_user_changes(db_path, seeds):
    store.ensure_defaults(db_path)
    store.set_setting(store.APPLICATIONS, {"Changed": r"C:\Games\changed.exe"}, db_path)

    created = store.reset_defaults(db_path)

    assert set(created) == set(store.SECTIONS)
    assert store.get_setting(store.APPLICATIONS, store.MISSING, db_path) == seeds[
        store.APPLICATIONS
    ]


def test_export_payload_contains_format_and_all_sections(db_path, seeds):
    store.ensure_defaults(db_path)

    payload = store.export_payload(db_path)

    assert payload["format"] == store.EXPORT_FORMAT
    assert payload["version"] == store.EXPORT_VERSION
    assert payload["exported_at"]
    assert payload["settings"] == seeds


def test_export_payload_falls_back_to_seeds_for_missing_sections(db_path, seeds):
    payload = store.export_payload(db_path)

    assert payload["settings"] == seeds


def test_validate_import_rejects_non_object_payload():
    with pytest.raises(ValueError, match="JSON object"):
        store.validate_import(["applications"])


def test_validate_import_rejects_payload_without_sections():
    with pytest.raises(ValueError, match="no settings sections"):
        store.validate_import({"format": store.EXPORT_FORMAT})

    with pytest.raises(ValueError, match="no settings sections"):
        store.validate_import({"settings": {}})


def test_validate_import_rejects_invalid_application_name():
    with pytest.raises(ValueError, match="invalid application name"):
        store.validate_import(
            {"settings": {"applications": {"Bad;Name": r"C:\Games\app.exe"}}}
        )


def test_validate_import_rejects_unsafe_application_path():
    with pytest.raises(ValueError, match="unsafe path"):
        store.validate_import(
            {"settings": {"applications": {"Alpha": r"C:\Games\a.exe & calc"}}}
        )


def test_validate_import_rejects_non_object_applications():
    with pytest.raises(ValueError, match="applications must be an object"):
        store.validate_import({"settings": {"applications": ["Alpha"]}})


def test_validate_import_rejects_invalid_profile_entries():
    with pytest.raises(ValueError, match="invalid profile name"):
        store.validate_import(
            {"settings": {"profiles": {"Bad;Profile": {"apps": [], "optional": []}}}}
        )

    with pytest.raises(ValueError, match="must be an object"):
        store.validate_import({"settings": {"profiles": {"GAMING": ["Alpha"]}}})

    with pytest.raises(ValueError, match="apps must be lists"):
        store.validate_import(
            {"settings": {"profiles": {"GAMING": {"apps": "Alpha"}}}}
        )

    with pytest.raises(ValueError, match="invalid app key"):
        store.validate_import(
            {"settings": {"profiles": {"GAMING": {"apps": ["Bad;App"]}}}}
        )


def test_validate_import_normalizes_profiles_and_accepts_bare_mapping():
    cleaned = store.validate_import(
        {
            "profiles": {
                "GAMING": {
                    "description": "play",
                    "apps": [" Alpha "],
                    "optional": ["Beta"],
                    "unknown": "ignored",
                }
            }
        }
    )

    assert cleaned == {
        "profiles": {
            "GAMING": {
                "description": "play",
                "apps": ["Alpha"],
                "optional": ["Beta"],
            }
        }
    }


def test_validate_import_normalizes_alerts_and_notifications():
    cleaned = store.validate_import(
        {
            "settings": {
                "alerts": {"interval_seconds": 10},
                "notifications": {"battery_warning": 1000},
            }
        }
    )

    assert cleaned["alerts"]["interval_seconds"] >= 1
    assert 5 <= cleaned["notifications"]["battery_warning"] <= 100


def test_import_payload_writes_every_section(db_path):
    payload = {
        "format": store.EXPORT_FORMAT,
        "version": store.EXPORT_VERSION,
        "settings": {
            "applications": {"Alpha": r"C:\Games\alpha.exe"},
            "profiles": {"GAMING": {"description": "", "apps": ["Alpha"], "optional": []}},
        },
    }

    imported = store.import_payload(payload, db_path)

    assert set(imported) == {"applications", "profiles"}
    assert store.get_setting(store.APPLICATIONS, store.MISSING, db_path) == {
        "Alpha": r"C:\Games\alpha.exe"
    }


def test_import_payload_is_atomic(db_path):
    store.set_setting(store.NOTIFICATIONS, {"battery_warning": 35}, db_path)
    payload = {
        "settings": {
            "notifications": {"battery_warning": 15},
            "applications": {"Alpha": r"C:\Games\a.exe ; calc"},
        }
    }

    with pytest.raises(ValueError, match="unsafe path"):
        store.import_payload(payload, db_path)

    # The valid notification section in the same file must not be applied.
    assert store.get_setting(store.NOTIFICATIONS, store.MISSING, db_path) == {
        "battery_warning": 35
    }