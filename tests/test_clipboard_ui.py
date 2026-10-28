"""Handler tests for the clipboard screens.

Native clipboard access is always faked, so the real clipboard is untouched.
"""

import asyncio

import pytest

from handlers import clipboard_ui
from tests.fakes import FakeCallback, FakeMessage, button_datas


@pytest.fixture(autouse=True)
def authorized(monkeypatch):
    monkeypatch.setattr("security.is_user_authorized", lambda user_id: True)
    clipboard_ui.clear_state()


def run(coro):
    return asyncio.run(coro)


def texts(message):
    return [item["text"] for item in message.sent]


def test_show_clipboard_lists_actions(monkeypatch):
    monkeypatch.setattr(clipboard_ui, "clipboard_available", lambda: True)
    message = FakeMessage()

    run(clipboard_ui.show_clipboard(message))

    body = texts(message)[0]
    assert "CLIPBOARD" in body
    assert button_datas(message.sent[0]["reply_markup"]) == [
        "clip_read",
        "clip_write",
        "clip_clear",
        "menu_main",
    ]


def test_show_clipboard_reports_unavailable_platform(monkeypatch):
    monkeypatch.setattr(clipboard_ui, "clipboard_available", lambda: False)
    message = FakeMessage()

    run(clipboard_ui.show_clipboard(message))

    assert "only available on Windows" in texts(message)[0]


def test_read_displays_text_verbatim(monkeypatch):
    monkeypatch.setattr(
        clipboard_ui,
        "read_clipboard_text",
        lambda: (True, "under_score * star [brackets]", False),
    )
    message = FakeMessage()

    run(clipboard_ui.read_clipboard(message))

    body = texts(message)[0]
    assert "under_score * star [brackets]" in body
    assert "29 characters" in body
    assert button_datas(message.sent[0]["reply_markup"]) == [
        "clip_open",
        "menu_main",
    ]


def test_read_reports_empty_clipboard(monkeypatch):
    monkeypatch.setattr(
        clipboard_ui, "read_clipboard_text", lambda: (True, "", False)
    )
    message = FakeMessage()

    run(clipboard_ui.read_clipboard(message))

    assert "clipboard is empty" in texts(message)[0].lower()


def test_read_reports_failure(monkeypatch):
    monkeypatch.setattr(
        clipboard_ui,
        "read_clipboard_text",
        lambda: (False, "The clipboard is busy.", False),
    )
    message = FakeMessage()

    run(clipboard_ui.read_clipboard(message))

    assert "failed" in texts(message)[0].lower()
    assert "busy" in texts(message)[0].lower()


def test_read_caps_display_and_marks_truncation(monkeypatch):
    payload = "a" * (clipboard_ui.DISPLAY_LIMIT + 100)
    monkeypatch.setattr(
        clipboard_ui, "read_clipboard_text", lambda: (True, payload, False)
    )
    message = FakeMessage()

    run(clipboard_ui.read_clipboard(message))

    body = texts(message)[0]
    assert "a" * clipboard_ui.DISPLAY_LIMIT in body
    assert "showing 3,500 of 3,600 characters" in body


def test_write_prompt_then_text_updates_clipboard(monkeypatch):
    written = []
    monkeypatch.setattr(
        clipboard_ui,
        "write_clipboard_text",
        lambda text: written.append(text) or (True, "Clipboard updated."),
    )
    message = FakeMessage()

    run(clipboard_ui.ask_write(message))
    assert clipboard_ui.write_pending(message.chat.id) is True
    assert button_datas(message.sent[0]["reply_markup"]) == ["clip_cancel"]

    message.text = "new _clipboard_ *text*"
    consumed = run(clipboard_ui.handle_text(message))

    assert consumed is True
    assert written == ["new _clipboard_ *text*"]
    assert clipboard_ui.write_pending(message.chat.id) is False
    assert "OK" in texts(message)[-1]


def test_write_rejects_empty_text_and_keeps_prompt(monkeypatch):
    monkeypatch.setattr(
        clipboard_ui,
        "write_clipboard_text",
        lambda text: pytest.fail("empty text must not be written"),
    )
    message = FakeMessage()
    run(clipboard_ui.ask_write(message))

    message.text = "   "
    consumed = run(clipboard_ui.handle_text(message))

    assert consumed is True
    assert clipboard_ui.write_pending(message.chat.id) is True
    assert "not changed" in texts(message)[-1]


def test_write_failure_is_reported(monkeypatch):
    monkeypatch.setattr(
        clipboard_ui,
        "write_clipboard_text",
        lambda text: (False, "Failed to write to the clipboard."),
    )
    message = FakeMessage()
    run(clipboard_ui.ask_write(message))
    message.text = "text"

    run(clipboard_ui.handle_text(message))

    assert "FAILED" in texts(message)[-1]
    assert clipboard_ui.write_pending(message.chat.id) is False


def test_unrelated_text_is_not_consumed(monkeypatch):
    called = []
    monkeypatch.setattr(
        clipboard_ui,
        "write_clipboard_text",
        lambda text: called.append(text) or (True, "ok"),
    )
    message = FakeMessage()
    message.text = "hello"

    assert run(clipboard_ui.handle_text(message)) is False
    assert called == []


def test_expired_write_prompt_is_ignored(monkeypatch):
    monkeypatch.setattr(
        clipboard_ui,
        "write_clipboard_text",
        lambda text: pytest.fail("expired prompt must not write"),
    )
    message = FakeMessage()
    run(clipboard_ui.ask_write(message))

    clipboard_ui._PENDING_WRITE[message.chat.id] = 0.0
    message.text = "late text"

    assert run(clipboard_ui.handle_text(message)) is False
    assert clipboard_ui.write_pending(message.chat.id) is False


def test_unauthorized_text_is_not_consumed(monkeypatch):
    monkeypatch.setattr("security.is_user_authorized", lambda user_id: False)
    message = FakeMessage()
    run(clipboard_ui.ask_write(message))
    message.text = "secret"

    assert run(clipboard_ui.handle_text(message)) is False
    assert clipboard_ui.write_pending(message.chat.id) is True


def test_cancel_clears_prompt_and_returns_to_screen(monkeypatch):
    monkeypatch.setattr(clipboard_ui, "clipboard_available", lambda: True)
    callback = FakeCallback()
    run(clipboard_ui.ask_write(callback.message))
    assert clipboard_ui.write_pending(callback.message.chat.id) is True

    run(clipboard_ui.handle_callback(callback, "clip_cancel"))

    assert clipboard_ui.write_pending(callback.message.chat.id) is False
    assert "CLIPBOARD" in texts(callback.message)[-1]


def test_clear_empties_clipboard_and_cancels_prompt(monkeypatch):
    cleared = []
    monkeypatch.setattr(
        clipboard_ui,
        "clear_clipboard",
        lambda: cleared.append(True) or (True, "Clipboard cleared."),
    )
    callback = FakeCallback()
    run(clipboard_ui.ask_write(callback.message))

    run(clipboard_ui.handle_callback(callback, "clip_clear"))

    assert cleared == [True]
    assert clipboard_ui.write_pending(callback.message.chat.id) is False
    assert "OK" in texts(callback.message)[-1]


def test_unknown_clipboard_action_is_rejected(monkeypatch):
    callback = FakeCallback()

    run(clipboard_ui.handle_callback(callback, "clip_selfdestruct"))

    assert callback.answers[-1]["text"] == "Unknown clipboard action"