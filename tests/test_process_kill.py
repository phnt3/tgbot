"""Tests for the Phase 4 process termination flow.

All psutil objects and termination calls are faked: no real process is ever
touched by these tests.
"""

import asyncio
import time

import pytest

import handlers
import process_manager
from fakes import FakeCallback, FakeMessage, button_datas


ALLOWED = {"game.exe", "notepad++.exe"}


def fake_terminable(name, allowlist=None):
    """Hermetic allowlist decision for handler tests."""
    allow = ALLOWED if allowlist is None else allowlist
    return bool(name) and name.lower() in allow


def test_terminable_allowlist_marks_critical_processes_unsafe(monkeypatch):
    # Even if a critical process somehow appears in the allowlist, it must
    # never be terminable.
    monkeypatch.setattr(
        process_manager, "get_terminable_allowlist",
        lambda: {"lsass.exe", "game.exe"},
    )
    assert process_manager.is_process_terminable("lsass.exe") is False
    assert process_manager.is_process_terminable("game.exe") is True
    assert process_manager.is_process_terminable("chrome.exe") is False


def test_request_kill_refuses_critical_process(monkeypatch):
    monkeypatch.setattr(
        process_manager, "get_process_details",
        lambda pid: {"name": "lsass.exe", "pid": pid, "memory_mb": 12.0},
    )
    monkeypatch.setattr(
        process_manager, "get_terminable_allowlist",
        lambda: {"lsass.exe"},  # worst case: misconfigured allowlist
    )
    monkeypatch.setattr(
        process_manager, "safe_terminate_process",
        lambda *a, **k: pytest.fail("critical process must never be terminated"),
    )

    callback = FakeCallback()
    asyncio.run(handlers.request_process_kill(callback, 4))
    assert handlers._PENDING_CONFIRMATIONS == {}
    assert "Refused" in callback.message.sent[-1]["text"]


def test_request_kill_refuses_non_allowlisted_process(monkeypatch):
    monkeypatch.setattr(
        process_manager, "get_process_details",
        lambda pid: {"name": "chrome.exe", "pid": pid, "memory_mb": 500.0},
    )
    monkeypatch.setattr(
        process_manager, "is_process_terminable",
        lambda name, allowlist=None: fake_terminable(name, allowlist),
    )

    callback = FakeCallback()
    asyncio.run(handlers.request_process_kill(callback, 7))
    assert handlers._PENDING_CONFIRMATIONS == {}
    assert "not on the termination allowlist" in callback.message.sent[-1]["text"]


def test_request_kill_arms_confirmation_for_allowlisted_process(monkeypatch):
    monkeypatch.setattr(
        process_manager, "get_process_details",
        lambda pid: {"name": "game.exe", "pid": pid, "memory_mb": 1024.0},
    )
    monkeypatch.setattr(
        process_manager, "is_process_terminable",
        lambda name, allowlist=None: fake_terminable(name, allowlist),
    )

    callback = FakeCallback()
    asyncio.run(handlers.request_process_kill(callback, 4242))

    assert "kill:4242" in handlers._PENDING_CONFIRMATIONS
    datas = button_datas(callback.message.sent[-1]["reply_markup"])
    assert "confirm_killp_4242" in datas
    assert "deny_killp_4242" in datas
    text = callback.message.sent[-1]["text"]
    assert "game.exe" in text


def test_confirm_without_request_does_not_kill(monkeypatch):
    calls = []
    monkeypatch.setattr(
        process_manager, "safe_terminate_process",
        lambda *a, **k: calls.append(a) or (True, "killed"),
    )

    callback = FakeCallback()
    asyncio.run(handlers.confirm_process_kill(callback, 4242))
    assert calls == []
    assert "expired" in callback.message.sent[-1]["text"].lower()


def test_expired_kill_confirmation_does_not_kill(monkeypatch):
    calls = []
    monkeypatch.setattr(
        process_manager, "safe_terminate_process",
        lambda *a, **k: calls.append(a) or (True, "killed"),
    )
    handlers._PENDING_CONFIRMATIONS["kill:4242"] = time.time() - 1

    callback = FakeCallback()
    asyncio.run(handlers.confirm_process_kill(callback, 4242))
    assert calls == []


def test_confirm_kill_executes_once(monkeypatch):
    calls = []

    def fake_kill(pid, reason="", db=None):
        calls.append((pid, reason))
        return True, "terminated"

    monkeypatch.setattr(process_manager, "get_process_details",
                        lambda pid: {"name": "game.exe", "pid": pid, "memory_mb": 1.0})
    monkeypatch.setattr(
        process_manager, "is_process_terminable",
        lambda name, allowlist=None: fake_terminable(name, allowlist),
    )
    monkeypatch.setattr(process_manager, "safe_terminate_process", fake_kill)

    callback = FakeCallback()
    asyncio.run(handlers.request_process_kill(callback, 4242))
    asyncio.run(handlers.confirm_process_kill(callback, 4242))
    assert len(calls) == 1
    assert calls[0][0] == 4242
    assert "kill:4242" not in handlers._PENDING_CONFIRMATIONS

    # Replaying the confirm button (or a stale message) must not kill again
    asyncio.run(handlers.confirm_process_kill(callback, 4242))
    assert len(calls) == 1


def test_pid_recycling_is_refused_at_confirm_time(monkeypatch):
    """The PID may belong to a different process by the time Confirm is pressed."""
    calls = []

    state = {"name": "game.exe"}
    monkeypatch.setattr(process_manager, "get_process_details",
                        lambda pid: {"name": state["name"], "pid": pid, "memory_mb": 1.0})
    monkeypatch.setattr(
        process_manager, "is_process_terminable",
        lambda name, allowlist=None: fake_terminable(name, allowlist),
    )
    monkeypatch.setattr(
        process_manager, "safe_terminate_process",
        lambda *a, **k: calls.append(a) or (True, "killed"),
    )

    callback = FakeCallback()
    asyncio.run(handlers.request_process_kill(callback, 4242))

    # PID got recycled into an unrelated process
    state["name"] = "something_else.exe"

    asyncio.run(handlers.confirm_process_kill(callback, 4242))
    assert calls == []
    assert "Refused" in callback.message.sent[-1]["text"]


def test_deny_kill_clears_confirmation(monkeypatch):
    monkeypatch.setattr(
        process_manager, "get_process_details",
        lambda pid: {"name": "game.exe", "pid": pid, "memory_mb": 1.0},
    )
    monkeypatch.setattr(
        process_manager, "is_process_terminable",
        lambda name, allowlist=None: fake_terminable(name, allowlist),
    )

    callback = FakeCallback()
    asyncio.run(handlers.request_process_kill(callback, 4242))
    assert "kill:4242" in handlers._PENDING_CONFIRMATIONS

    asyncio.run(handlers.handle_power_confirmation(callback, "deny_killp_4242"))
    assert "kill:4242" not in handlers._PENDING_CONFIRMATIONS


def test_show_process_info_lists_only_allowlisted_kill_buttons(monkeypatch):
    monkeypatch.setattr(
        process_manager, "get_processes_by_cpu",
        lambda limit=10: [
            {"name": "game.exe", "pid": 1, "cpu_percent": 55.0},
            {"name": "lsass.exe", "pid": 2, "cpu_percent": 3.0},
        ],
    )
    monkeypatch.setattr(
        process_manager, "get_processes_by_ram",
        lambda limit=10: [
            {"name": "game.exe", "pid": 1, "ram_mb": 100.0},
            {"name": "chrome.exe", "pid": 3, "ram_mb": 900.0},
        ],
    )
    monkeypatch.setattr(
        process_manager, "get_terminable_allowlist",
        lambda: {"game.exe"},
    )

    message = FakeMessage()
    asyncio.run(handlers.show_process_info(message))

    sent = message.sent[-1]
    datas = button_datas(sent["reply_markup"])
    assert "killp_1" in datas
    assert "killp_2" not in datas  # critical
    assert "killp_3" not in datas  # not allowlisted
    # Dedupe: game.exe appears in both lists but only one button
    assert datas.count("killp_1") == 1
    assert "[K]" in sent["text"]


def test_show_process_info_caps_kill_buttons(monkeypatch):
    entries = [
        {"name": f"app{i}.exe", "pid": 100 + i, "ram_mb": float(i)}
        for i in range(12)
    ]
    monkeypatch.setattr(process_manager, "get_processes_by_cpu", lambda limit=10: [])
    monkeypatch.setattr(process_manager, "get_processes_by_ram", lambda limit=10: entries)
    monkeypatch.setattr(
        process_manager, "get_terminable_allowlist",
        lambda: {entry["name"] for entry in entries},
    )

    message = FakeMessage()
    asyncio.run(handlers.show_process_info(message))

    datas = button_datas(message.sent[-1]["reply_markup"])
    kill_datas = [d for d in datas if d and d.startswith("killp_")]
    assert len(kill_datas) == 8
    assert datas[-1] == "menu_main"


class FakeProc:
    def __init__(self, name: str) -> None:
        self._name = name
        self.terminated = False
        self.killed = False

    def name(self) -> str:
        return self._name

    def status(self) -> str:
        return "running"

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None):
        return 0


def test_safe_terminate_refuses_critical_process(monkeypatch):
    proc = FakeProc("lsass.exe")
    monkeypatch.setattr(process_manager.psutil, "Process", lambda pid: proc)

    ok, message = process_manager.safe_terminate_process(4)
    assert ok is False
    assert proc.terminated is False
    assert proc.killed is False


def test_safe_terminate_refuses_non_allowlisted_process(monkeypatch):
    proc = FakeProc("chrome.exe")
    monkeypatch.setattr(process_manager.psutil, "Process", lambda pid: proc)
    monkeypatch.setattr(
        process_manager, "get_terminable_allowlist",
        lambda: {"game.exe"},
    )

    ok, message = process_manager.safe_terminate_process(5)
    assert ok is False
    assert proc.terminated is False
    assert "allowlist" in message