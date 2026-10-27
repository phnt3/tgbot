"""Tests for the security helpers and the authorization decorator.

Real aiogram update objects are used only to exercise ``isinstance`` checks;
their ``answer()`` calls are not delivered anywhere because no Bot is bound.
"""

import asyncio
from datetime import datetime

import pytest
from aiogram import types

import security


def _message(user_id: int) -> types.Message:
    return types.Message(
        message_id=1,
        date=datetime.now(),
        chat=types.Chat(id=user_id, type="private"),
        from_user=types.User(id=user_id, is_bot=False, first_name="Test"),
    )


def _callback(user_id: int) -> types.CallbackQuery:
    return types.CallbackQuery(
        id="cb-1",
        from_user=types.User(id=user_id, is_bot=False, first_name="Test"),
        chat_instance="instance",
        data="menu_main",
    )


# ---------- validate_callback_data ----------

@pytest.mark.parametrize(
    "data,prefix,expected",
    [
        ("app_3", "app_", True),
        ("app_3", "app", True),
        ("menu_main", "app_", False),
        ("app_3", "menu_", False),
        ("prefix_only", "prefix_only", True),
        ("", "app_", False),
        ("anything", "", True),
        ("x" * 100, "", True),
        ("x" * 101, "", False),
    ],
)
def test_validate_callback_data(data, prefix, expected):
    assert security.validate_callback_data(data, prefix) is expected


# ---------- validate_application_name ----------

@pytest.mark.parametrize(
    "name",
    [
        "Steam",
        "Visual Studio Code",
        "C++",
        "app.exe",
        "my-app_2",
        "A B C",
    ],
)
def test_application_name_accepts_allowlist_style_names(name):
    assert security.validate_application_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "app;rm -rf",
        "app|calc",
        "app/path",
        r"app\path",
        "app>file",
        "$(whoami)",
    ],
)
def test_application_name_rejects_injection_patterns(name):
    assert security.validate_application_name(name) is False


# ---------- validate_path_safe ----------

@pytest.mark.parametrize(
    "path",
    [
        r"C:\Program Files (x86)\Steam\steam.exe",
        r"D:\Games\my-game_2\game.exe",
        r"C:\Apps\app.lnk",
        r"\\server\share\app.exe",
        "/usr/local/bin/app",
        r"Q:\Games\Example [Build 1]\game.exe",
    ],
)
def test_path_safe_accepts_normal_paths(path):
    assert security.validate_path_safe(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "",
        "   ",
        r"C:\Apps\app.exe; calc",
        r"C:\Apps\app.exe & calc",
        r"C:\Apps\app.exe | calc",
        r"C:\Apps\..\Windows\system32\cmd.exe",
        r"C:\Apps\app>out.txt",
        r"C:\Apps\app<in.txt",
        r"C:\Apps\app`.exe",
        r"C:\Apps\app$name.exe",
        "$(whoami)",
    ],
)
def test_path_safe_rejects_dangerous_paths(path):
    assert security.validate_path_safe(path) is False


# ---------- authorize_handler ----------

def test_authorize_handler_allows_configured_user(monkeypatch):
    monkeypatch.setattr(security, "is_user_authorized", lambda uid: uid == 42)
    calls = []

    async def handler(update):
        calls.append(update)
        return "handled"

    wrapped = security.authorize_handler(handler)
    message = _message(42)

    assert asyncio.run(wrapped(message)) == "handled"
    assert calls == [message]


def test_authorize_handler_rejects_unknown_message_user(monkeypatch):
    monkeypatch.setattr(security, "is_user_authorized", lambda uid: False)
    calls = []

    async def handler(update):
        calls.append(update)
        return "handled"

    wrapped = security.authorize_handler(handler)

    assert asyncio.run(wrapped(_message(7))) is None
    assert calls == []


def test_authorize_handler_rejects_unknown_callback_user(monkeypatch):
    monkeypatch.setattr(security, "is_user_authorized", lambda uid: False)
    calls = []

    async def handler(update):
        calls.append(update)
        return "handled"

    wrapped = security.authorize_handler(handler)

    assert asyncio.run(wrapped(_callback(7))) is None
    assert calls == []


def test_authorize_handler_allows_callback(monkeypatch):
    monkeypatch.setattr(security, "is_user_authorized", lambda uid: uid == 42)
    calls = []

    async def handler(update):
        calls.append(update)
        return "handled"

    wrapped = security.authorize_handler(handler)
    callback = _callback(42)

    assert asyncio.run(wrapped(callback)) == "handled"
    assert calls == [callback]


def test_authorize_handler_rejects_non_telegram_update(monkeypatch):
    monkeypatch.setattr(security, "is_user_authorized", lambda uid: True)
    calls = []

    async def handler(update):
        calls.append(update)
        return "handled"

    wrapped = security.authorize_handler(handler)

    assert asyncio.run(wrapped(object())) is None
    assert calls == []


def test_authorize_handler_forwards_extra_arguments(monkeypatch):
    monkeypatch.setattr(security, "is_user_authorized", lambda uid: True)
    seen = {}

    async def handler(update, *args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return "ok"

    wrapped = security.authorize_handler(handler)

    result = asyncio.run(wrapped(_message(42), 1, 2, key="value"))

    assert result == "ok"
    assert seen == {"args": (1, 2), "kwargs": {"key": "value"}}