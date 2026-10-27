"""Handler-level tests for the in-bot settings screens.

Each test runs against a temporary settings database, so the live
``data/pc_remote.db`` is never touched.
"""

import asyncio
import copy
import json
import math

import pytest

import handlers.settings_ui as ui
import settings_store as store
from tests.fakes import FakeCallback, FakeMessage, button_datas

CHAT = 1

STATE = (ui._PENDING_TEXT, ui._PICKER, ui._PENDING_IMPORT, ui._PENDING_RESET)


def _clear_state() -> None:
    for mapping in STATE:
        mapping.clear()


@pytest.fixture
def settings_db(tmp_path, monkeypatch):
    """Temporary settings database with deterministic generic defaults."""
    db_path = tmp_path / "settings.db"
    monkeypatch.setattr(store, "default_db_path", lambda: db_path)

    seeds = {
        store.APPLICATIONS: {
            "Alpha": r"C:\Games\alpha.exe",
            "Beta": r"C:\Games\beta.lnk",
        },
        store.PROFILES: {
            "NORMAL": {"description": "status", "apps": [], "optional": []},
            "GAMING": {"description": "play", "apps": ["Alpha"], "optional": ["Beta"]},
        },
        store.ALERTS: {
            "thresholds": {"cpu_temperature": 90, "ram_utilization": 85},
            "cooldowns": {"cpu_temperature": 300},
            "interval_seconds": 60,
        },
        store.NOTIFICATIONS: {
            "battery_warning": 20,
            "battery_critical": 10,
            "notify_login": True,
        },
    }
    monkeypatch.setattr(
        store,
        "_SEEDS",
        {key: (lambda value=value: copy.deepcopy(value)) for key, value in seeds.items()},
    )
    store.ensure_defaults()
    monkeypatch.setattr(ui, "is_user_authorized", lambda user_id: True)

    _clear_state()
    try:
        yield seeds
    finally:
        _clear_state()


def _press(data):
    """Route one callback as chat ``CHAT`` and return the fake callback."""
    callback = FakeCallback(CHAT)
    asyncio.run(ui.handle_callback(callback, data))
    return callback


def _text(text, chat_id=CHAT):
    message = FakeMessage(chat_id)
    message.text = text
    asyncio.run(ui.handle_text(message))
    return message


def _last(callback):
    return callback.message.sent[-1]


def _last_text(callback):
    return _last(callback)["text"]


def _last_buttons(callback):
    return button_datas(_last(callback)["reply_markup"])


def _notifications():
    from notifier import load_notification_settings

    return load_notification_settings()


def _alerts():
    from alerts import load_alert_settings

    return load_alert_settings()


def _profiles():
    from gaming_profiles import load_profiles_config

    return load_profiles_config()


def _apps():
    from application_manager import load_applications_config

    return load_applications_config()


# --- main screen -----------------------------------------------------------


def test_main_screen_summarises_settings(settings_db):
    callback = _press("set_main")

    text = _last_text(callback)
    assert "=== SETTINGS ===" in text
    assert "Applications: 2" in text
    assert "Profiles: 1" in text
    assert "Notifications:" in text

    buttons = _last_buttons(callback)
    assert "set_notif" in buttons
    assert "set_alerts" in buttons
    assert "set_apps" in buttons
    assert "set_pf" in buttons
    assert "set_bk" in buttons


def test_main_screen_clears_pending_prompt(settings_db):
    _press("set_nbx_battery_warning")
    assert CHAT in ui._PENDING_TEXT

    _press("set_main")

    assert CHAT not in ui._PENDING_TEXT


def test_unknown_callback_reports_expired_screen(settings_db):
    callback = _press("set_unknown")

    assert callback.answers
    assert callback.answers[-1]["show_alert"] is True
    assert "expired" in callback.answers[-1]["text"]


# --- notifications ---------------------------------------------------------


def test_toggle_notification_flips_value(settings_db):
    assert _notifications()["notify_login"] is True

    callback = _press("set_nt_login")

    assert _notifications()["notify_login"] is False
    assert "Login / unlock: OFF" in _last_text(callback)


def test_notification_step_button_and_exact_text(settings_db):
    _press("set_nb_battery_warning_d")
    assert _notifications()["battery_warning"] == 15

    _press("set_nbx_battery_warning")
    assert ui._PENDING_TEXT[CHAT]["kind"] == "notif_field"

    message = _text("35")
    assert message.sent
    assert _notifications()["battery_warning"] == 35
    assert CHAT not in ui._PENDING_TEXT


def test_notification_text_out_of_range_rearms_prompt(settings_db):
    _press("set_nbx_battery_warning")
    _text("500")

    assert ui._PENDING_TEXT[CHAT]["kind"] == "notif_field"
    assert _notifications()["battery_warning"] == 20


def test_quiet_hours_toggle_and_start(settings_db):
    _press("set_nq_toggle")
    quiet = _notifications()["quiet_hours"]
    assert quiet["enabled"] is True

    _press("set_nq_start")
    _text("22:30")

    assert _notifications()["quiet_hours"]["start"] == "22:30"


def test_quiet_hours_invalid_time_rearms_prompt(settings_db):
    _press("set_nq_start")
    _text("25:99")

    assert ui._PENDING_TEXT[CHAT]["kind"] == "quiet_time"
    assert _notifications()["quiet_hours"]["start"] == "23:00"


# --- monitoring alerts -----------------------------------------------------


def test_alert_threshold_exact_text(settings_db):
    _press("set_atx_cpu_temperature")
    assert ui._PENDING_TEXT[CHAT]["kind"] == "alert_threshold"

    _text("95")

    assert _alerts()["thresholds"]["cpu_temperature"] == 95
    assert CHAT not in ui._PENDING_TEXT


def test_alert_interval_accepts_minutes(settings_db):
    _press("set_aix")
    _text("5 min")

    assert _alerts()["interval_seconds"] == 300


def test_cooldown_never_then_exact_zero_then_step(settings_db):
    _press("set_ac_cpu_temperature_never")
    assert math.isinf(_alerts()["cooldowns"]["cpu_temperature"])

    _press("set_acx_cpu_temperature")
    _text("0")
    assert _alerts()["cooldowns"]["cpu_temperature"] == 0

    _press("set_ac_cpu_temperature_u")
    assert _alerts()["cooldowns"]["cpu_temperature"] == 60


# --- applications ----------------------------------------------------------


def test_rename_application_updates_profile_references(settings_db):
    _press("set_ap_n_0")
    assert ui._PENDING_TEXT[CHAT]["kind"] == "app_rename"

    _text("Gamma")

    assert "Gamma" in _apps()
    assert "Alpha" not in _apps()
    profiles = _profiles()
    assert profiles["GAMING"]["apps"] == ["Gamma"]
    assert profiles["GAMING"]["optional"] == ["Beta"]


def test_remove_application_leaves_profile_references(settings_db):
    confirm = _press("set_ap_r_0")
    assert 'Remove "Alpha" from the launch allowlist?' in _last_text(confirm)
    assert "set_ap_ry_0" in _last_buttons(confirm)

    removed = _press("set_ap_ry_0")

    assert "Alpha" not in _apps()
    assert "Alpha" in _profiles()["GAMING"]["apps"]
    assert "Removed" in _last_text(removed)

    detail = _press("set_pfd_0")
    assert "not in the allowlist: Alpha" in _last_text(detail)


def test_remove_application_keeps_other_entries(settings_db):
    _press("set_ap_ry_1")

    assert list(_apps()) == ["Alpha"]


# --- profiles --------------------------------------------------------------


def test_create_profile_then_describe_it(settings_db):
    _press("set_pf_new")
    assert ui._PENDING_TEXT[CHAT]["kind"] == "profile_new"

    _text("RACING")

    profiles = _profiles()
    assert profiles["RACING"]["apps"] == []
    assert profiles["RACING"]["optional"] == []
    assert CHAT not in ui._PENDING_TEXT

    _press("set_pfe_1")
    _text("fast laps")

    assert _profiles()["RACING"]["description"] == "fast laps"


def test_profile_description_dash_clears(settings_db):
    _press("set_pfe_0")
    _text("-")

    assert _profiles()["GAMING"]["description"] == ""


def test_profile_app_membership_cycles(settings_db):
    _press("set_pfc_0_0")
    profiles = _profiles()
    assert profiles["GAMING"]["apps"] == []
    # Moving a required app onto the optional list appends it.
    assert profiles["GAMING"]["optional"] == ["Beta", "Alpha"]

    _press("set_pfc_0_1")
    profiles = _profiles()
    assert profiles["GAMING"]["optional"] == ["Alpha"]
    assert profiles["GAMING"]["apps"] == []

    # Unused -> required.
    _press("set_pfc_0_1")
    assert _profiles()["GAMING"]["apps"] == ["Beta"]

    # Required -> optional.
    _press("set_pfc_0_1")
    profiles = _profiles()
    assert profiles["GAMING"]["apps"] == []
    assert profiles["GAMING"]["optional"] == ["Alpha", "Beta"]


def test_rename_profile(settings_db):
    _press("set_pfn_0")
    assert ui._PENDING_TEXT[CHAT]["kind"] == "profile_rename"

    _text("PLAY")

    profiles = _profiles()
    assert "PLAY" in profiles
    assert "GAMING" not in profiles
    assert profiles["PLAY"]["apps"] == ["Alpha"]


def test_delete_profile_requires_confirmation(settings_db):
    confirm = _press("set_pfr_0")
    assert "Delete profile \"GAMING\"?" in _last_text(confirm)
    assert "set_pfry_0" in _last_buttons(confirm)

    deleted = _press("set_pfry_0")

    assert "GAMING" not in _profiles()
    assert "deleted" in _last_text(deleted)


def test_delete_profile_rejects_stale_index(settings_db):
    _press("set_pfry_9")

    assert "GAMING" in _profiles()


# --- backup, export, import, reset ----------------------------------------


def test_export_sends_json_document(settings_db):
    callback = _press("set_bk_export")

    entry = _last(callback)
    document = entry["document"]
    assert document.filename == "pc_remote_settings.json"
    payload = json.loads(document.data.decode("utf-8"))
    assert set(payload["settings"]) == {
        store.APPLICATIONS,
        store.PROFILES,
        store.ALERTS,
        store.NOTIFICATIONS,
    }


def test_import_requires_arming(settings_db):
    with pytest.raises(ValueError):
        ui.import_settings(CHAT, b"{}")


def test_import_invalid_json_disarms_and_changes_nothing(settings_db):
    _press("set_bk_import")
    assert ui.import_armed(CHAT)

    with pytest.raises(ValueError):
        ui.import_settings(CHAT, b"not json")

    assert not ui.import_armed(CHAT)
    assert "Alpha" in _apps()


def test_import_valid_backup_replaces_settings(settings_db):
    payload = store.export_payload()
    payload["settings"][store.APPLICATIONS] = {"Gamma": r"C:\Games\gamma.exe"}
    payload["settings"][store.PROFILES] = {
        "SOLO": {"description": "one", "apps": ["Gamma"], "optional": []}
    }

    _press("set_bk_import")
    imported = ui.import_settings(CHAT, json.dumps(payload).encode("utf-8"))

    assert imported[store.APPLICATIONS] == {"Gamma": r"C:\Games\gamma.exe"}
    assert list(_apps()) == ["Gamma"]
    assert "SOLO" in _profiles()
    assert "GAMING" not in _profiles()


def test_import_rejects_invalid_section_atomically(settings_db):
    payload = store.export_payload()
    payload["settings"][store.APPLICATIONS] = {"Bad Name!": r"C:\Games\bad.exe"}

    _press("set_bk_import")
    with pytest.raises(ValueError):
        ui.import_settings(CHAT, json.dumps(payload).encode("utf-8"))

    assert list(_apps()) == ["Alpha", "Beta"]
    assert "GAMING" in _profiles()


def test_reset_without_confirmation_shows_backup_screen(settings_db):
    message = FakeMessage(CHAT)

    asyncio.run(ui.reset_settings(message, CHAT))

    assert "BACKUP & RESTORE" in message.sent[-1]["text"]
    assert "GAMING" in _profiles()


def test_reset_after_confirmation_restores_defaults(settings_db):
    _press("set_bk_reset")
    assert CHAT in ui._PENDING_RESET

    callback = _press("set_bk_reset_yes")

    assert CHAT not in ui._PENDING_RESET
    assert "reset to defaults" in _last_text(callback)
    assert "GAMING" in _profiles()
    assert _notifications()["battery_warning"] == 20


def test_press_cancels_pending_import(settings_db):
    _press("set_bk_import")
    assert ui.import_armed(CHAT)

    _press("set_bk")

    assert not ui.import_armed(CHAT)


# --- text prompts ----------------------------------------------------------


def test_text_without_prompt_is_not_consumed(settings_db):
    message = FakeMessage(CHAT)
    message.text = "hello"

    assert asyncio.run(ui.handle_text(message)) is False
    assert not message.sent


def test_unauthorized_text_is_ignored(settings_db, monkeypatch):
    monkeypatch.setattr(ui, "is_user_authorized", lambda user_id: False)
    _press("set_nbx_battery_warning")

    message = FakeMessage(CHAT)
    message.text = "40"

    assert asyncio.run(ui.handle_text(message)) is False
    assert _notifications()["battery_warning"] == 20
    assert CHAT in ui._PENDING_TEXT