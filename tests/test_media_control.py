"""Tests for the whitelisted media/volume keys."""

import media_control
from media_control import MEDIA_ACTIONS, press_key


def test_every_action_is_whitelisted_and_labelled():
    assert set(MEDIA_ACTIONS) == {
        "vol_up",
        "vol_down",
        "mute",
        "play_pause",
        "next",
        "prev",
        "stop",
    }
    assert all(label for label, _ in MEDIA_ACTIONS.values())


def test_press_key_rejects_unknown_action_without_touching_the_os():
    ok, message = press_key("format_disk")
    assert ok is False
    assert "Unknown media action" in message


def test_press_key_sends_one_press_and_release(monkeypatch):
    calls = []
    monkeypatch.setattr(media_control, "_press_vk", lambda vk: calls.append(vk))

    ok, message = press_key("play_pause")

    assert ok is True
    assert calls == [media_control.VK_MEDIA_PLAY_PAUSE]
    assert "Play/Pause" in message


def test_press_key_reports_os_errors(monkeypatch):
    def boom(vk):
        raise OSError("no user32")

    monkeypatch.setattr(media_control, "_press_vk", boom)

    ok, message = press_key("mute")

    assert ok is False
    assert "Mute toggle" in message