"""Tests for the Phase 4 power flow: confirmation, expiry, one-shot execution.

Nothing here touches the real machine: every power_manager operation is
monkeypatched, so a test bug can never shut down or restart the PC.
"""

import asyncio
import time

import pytest

import handlers
import power_manager
from fakes import FakeCallback, button_datas


def test_confirmation_is_one_shot():
    key = "restart"
    handlers._PENDING_CONFIRMATIONS.clear()
    handlers._request_confirmation(key)
    assert handlers._consume_confirmation(key) is True
    # A second press must not be accepted (double-click / stale button)
    assert handlers._consume_confirmation(key) is False


def test_confirmation_expires():
    key = "shutdown"
    handlers._PENDING_CONFIRMATIONS.clear()
    handlers._request_confirmation(key)
    # Force the window into the past
    handlers._PENDING_CONFIRMATIONS[key] = time.time() - 1
    assert handlers._consume_confirmation(key) is False


def test_clear_confirmations_removes_keys():
    handlers._PENDING_CONFIRMATIONS.clear()
    handlers._request_confirmation("restart")
    handlers._request_confirmation("kill:123")
    handlers._clear_confirmations("restart", "kill:123")
    assert handlers._PENDING_CONFIRMATIONS == {}


def test_restart_requires_confirmation_then_executes_once(monkeypatch):
    calls = []

    def fake_restart(confirmed=False):
        calls.append(confirmed)
        return True, "restart scheduled"

    def forbidden_shutdown(confirmed=False):
        pytest.fail("execute_shutdown must not run for a restart")

    monkeypatch.setattr(power_manager, "execute_restart", fake_restart)
    monkeypatch.setattr(power_manager, "execute_shutdown", forbidden_shutdown)

    callback = FakeCallback()

    # First press only asks for confirmation; nothing is executed
    asyncio.run(handlers.handle_power_action(callback, "action_restart"))
    assert calls == []
    assert "restart" in handlers._PENDING_CONFIRMATIONS
    datas = button_datas(callback.message.sent[-1]["reply_markup"])
    assert "confirm_restart" in datas
    assert "deny_restart" in datas

    # Confirm executes exactly once
    asyncio.run(handlers.handle_power_confirmation(callback, "confirm_restart"))
    assert calls == [True]
    assert "restart" not in handlers._PENDING_CONFIRMATIONS

    # Replaying the same confirm button must be refused
    asyncio.run(handlers.handle_power_confirmation(callback, "confirm_restart"))
    assert calls == [True]


def test_shutdown_confirm_executes_shutdown_only(monkeypatch):
    calls = []

    def fake_shutdown(confirmed=False):
        calls.append(confirmed)
        return True, "shutdown scheduled"

    monkeypatch.setattr(power_manager, "execute_shutdown", fake_shutdown)
    monkeypatch.setattr(
        power_manager, "execute_restart",
        lambda confirmed=False: pytest.fail("restart must not run"),
    )

    callback = FakeCallback()
    asyncio.run(handlers.handle_power_action(callback, "action_shutdown"))
    assert calls == []
    assert "shutdown" in handlers._PENDING_CONFIRMATIONS

    asyncio.run(handlers.handle_power_confirmation(callback, "confirm_shutdown"))
    assert calls == [True]


def test_deny_clears_pending_and_does_not_execute(monkeypatch):
    calls = []
    monkeypatch.setattr(
        power_manager, "execute_restart",
        lambda confirmed=False: calls.append(confirmed) or (True, "done"),
    )

    callback = FakeCallback()
    asyncio.run(handlers.handle_power_action(callback, "action_restart"))
    assert "restart" in handlers._PENDING_CONFIRMATIONS

    asyncio.run(handlers.handle_power_confirmation(callback, "deny_restart"))
    assert calls == []
    assert "restart" not in handlers._PENDING_CONFIRMATIONS
    assert "cancelled" in callback.message.sent[-1]["text"].lower()


def test_expired_confirmation_does_not_execute(monkeypatch):
    calls = []
    monkeypatch.setattr(
        power_manager, "execute_restart",
        lambda confirmed=False: calls.append(confirmed) or (True, "done"),
    )

    handlers._PENDING_CONFIRMATIONS.clear()
    handlers._PENDING_CONFIRMATIONS["restart"] = time.time() - 1

    callback = FakeCallback()
    asyncio.run(handlers.handle_power_confirmation(callback, "confirm_restart"))
    assert calls == []
    assert "expired" in callback.message.sent[-1]["text"].lower()


def test_lock_runs_immediately(monkeypatch):
    calls = []
    monkeypatch.setattr(
        power_manager, "lock_workstation",
        lambda: calls.append(True) or (True, "workstation locked"),
    )

    callback = FakeCallback()
    asyncio.run(handlers.handle_power_action(callback, "action_lock"))
    assert calls == [True]
    assert "Lock: OK" in callback.message.sent[-1]["text"]


def test_hibernate_runs_immediately(monkeypatch):
    calls = []
    monkeypatch.setattr(
        power_manager, "hibernate_system",
        lambda: calls.append(True) or (True, "hibernating"),
    )
    monkeypatch.setattr(
        power_manager, "lock_workstation",
        lambda: pytest.fail("lock must not run for hibernate"),
    )

    callback = FakeCallback()
    asyncio.run(handlers.handle_power_action(callback, "action_hibernate"))
    assert calls == [True]
    assert "Hibernate: OK" in callback.message.sent[-1]["text"]


def test_display_off_runs_immediately(monkeypatch):
    calls = []
    monkeypatch.setattr(
        power_manager, "display_off",
        lambda: calls.append(True) or (True, "monitor off"),
    )

    callback = FakeCallback()
    asyncio.run(handlers.handle_power_action(callback, "action_display_off"))
    assert calls == [True]
    assert "Display off: OK" in callback.message.sent[-1]["text"]


def test_power_screen_shows_new_actions(monkeypatch):
    monkeypatch.setattr(
        power_manager,
        "get_power_status",
        lambda: {
            "can_hibernate": True,
            "can_display_off": True,
            "has_pending_shutdown": False,
        },
    )
    message = FakeCallback().message

    asyncio.run(handlers.show_power_management(message))

    body = message.sent[-1]["text"]
    assert "Hibernate: available" in body
    assert "Display off: available" in body
    datas = button_datas(message.sent[-1]["reply_markup"])
    assert "action_hibernate" in datas
    assert "action_display_off" in datas


def test_cancel_shutdown_clears_pending_confirmations(monkeypatch):
    calls = []
    monkeypatch.setattr(
        power_manager, "cancel_shutdown",
        lambda: calls.append(True) or (True, "shutdown aborted"),
    )

    handlers._request_confirmation("restart")
    handlers._request_confirmation("shutdown")

    callback = FakeCallback()
    asyncio.run(handlers.handle_power_action(callback, "action_cancel"))
    assert calls == [True]
    assert "restart" not in handlers._PENDING_CONFIRMATIONS
    assert "shutdown" not in handlers._PENDING_CONFIRMATIONS


def test_unknown_power_action_is_rejected():
    callback = FakeCallback()
    asyncio.run(handlers.handle_power_action(callback, "action_selfdestruct"))
    assert callback.answers[-1]["text"] == "Unknown power action"