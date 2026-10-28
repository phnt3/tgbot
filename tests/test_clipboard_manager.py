"""Tests for the Win32 clipboard bridge.

Every native helper is monkeypatched: these tests never read or change the
real clipboard of the machine running them.
"""

import pytest

import clipboard_manager
from clipboard_manager import (
    MAX_TEXT_CHARS,
    clear_clipboard,
    read_clipboard_text,
    write_clipboard_text,
)


@pytest.fixture
def native(monkeypatch):
    """Fake the Win32 helpers and record the calls they receive."""
    calls = {"opened": 0, "closed": 0, "text": None}

    monkeypatch.setattr(clipboard_manager, "clipboard_available", lambda: True)
    monkeypatch.setattr(
        clipboard_manager,
        "_open_clipboard",
        lambda: calls.__setitem__("opened", calls["opened"] + 1) or True,
    )
    monkeypatch.setattr(
        clipboard_manager,
        "_close_clipboard",
        lambda: calls.__setitem__("closed", calls["closed"] + 1),
    )
    return calls


def test_read_returns_text_and_closes_clipboard(native, monkeypatch):
    monkeypatch.setattr(clipboard_manager, "_get_text", lambda: "hello_world *")

    ok, text, truncated = read_clipboard_text()

    assert ok is True
    assert text == "hello_world *"
    assert truncated is False
    assert native == {"opened": 1, "closed": 1, "text": None}


def test_read_empty_clipboard_is_success(native, monkeypatch):
    monkeypatch.setattr(clipboard_manager, "_get_text", lambda: None)

    ok, text, truncated = read_clipboard_text()

    assert ok is True
    assert text == ""
    assert truncated is False


def test_read_truncates_at_hard_limit(native, monkeypatch):
    payload = "x" * (MAX_TEXT_CHARS + 10)
    monkeypatch.setattr(clipboard_manager, "_get_text", lambda: payload)

    ok, text, truncated = read_clipboard_text()

    assert ok is True
    assert len(text) == MAX_TEXT_CHARS
    assert truncated is True


def test_read_busy_clipboard_reports_error(monkeypatch):
    monkeypatch.setattr(clipboard_manager, "clipboard_available", lambda: True)
    monkeypatch.setattr(clipboard_manager, "_open_clipboard", lambda: False)
    monkeypatch.setattr(
        clipboard_manager,
        "_close_clipboard",
        lambda: pytest.fail("must not close a clipboard it never opened"),
    )

    ok, text, truncated = read_clipboard_text()

    assert ok is False
    assert "busy" in text.lower()
    assert truncated is False


def test_read_native_error_is_wrapped(native, monkeypatch):
    def boom():
        raise OSError("clipboard exploded")

    monkeypatch.setattr(clipboard_manager, "_get_text", boom)

    ok, text, truncated = read_clipboard_text()

    assert ok is False
    assert "failed" in text.lower()
    assert truncated is False
    assert native["closed"] == 1


def test_write_sets_exact_text(native, monkeypatch):
    written = []
    monkeypatch.setattr(
        clipboard_manager, "_set_text", lambda text: written.append(text) or True
    )

    ok, message = write_clipboard_text(" spaced\ttext\n")

    assert ok is True
    assert written == [" spaced\ttext\n"]
    assert "13 characters" in message
    assert native["closed"] == 1


def test_write_rejects_whitespace_only(native, monkeypatch):
    monkeypatch.setattr(
        clipboard_manager,
        "_set_text",
        lambda text: pytest.fail("empty text must not reach the clipboard"),
    )

    ok, message = write_clipboard_text("   \n\t")

    assert ok is False
    assert "empty" in message.lower()


def test_write_rejects_oversized_text(native, monkeypatch):
    monkeypatch.setattr(
        clipboard_manager,
        "_set_text",
        lambda text: pytest.fail("oversized text must not reach the clipboard"),
    )

    ok, message = write_clipboard_text("x" * (MAX_TEXT_CHARS + 1))

    assert ok is False
    assert "exceeds" in message.lower()


def test_write_reports_native_failure(native, monkeypatch):
    monkeypatch.setattr(clipboard_manager, "_set_text", lambda text: False)

    ok, message = write_clipboard_text("text")

    assert ok is False
    assert "failed" in message.lower()
    assert native["closed"] == 1


def test_clear_empties_and_closes(monkeypatch):
    state = {"emptied": 0, "closed": 0}

    class FakeUser32:
        def EmptyClipboard(self):
            state["emptied"] += 1
            return 1

    class FakeWindll:
        user32 = FakeUser32()

    class FakeCtypes:
        windll = FakeWindll()

    monkeypatch.setattr(clipboard_manager, "clipboard_available", lambda: True)
    monkeypatch.setattr(clipboard_manager, "_open_clipboard", lambda: True)
    monkeypatch.setattr(
        clipboard_manager,
        "_close_clipboard",
        lambda: state.__setitem__("closed", state["closed"] + 1),
    )
    monkeypatch.setattr(clipboard_manager, "ctypes", FakeCtypes)

    ok, message = clear_clipboard()

    assert ok is True
    assert "cleared" in message.lower()
    assert state == {"emptied": 1, "closed": 1}


def test_non_windows_reports_unavailable(monkeypatch):
    monkeypatch.setattr(clipboard_manager, "clipboard_available", lambda: False)

    assert read_clipboard_text()[0] is False
    assert write_clipboard_text("x")[0] is False
    assert clear_clipboard()[0] is False