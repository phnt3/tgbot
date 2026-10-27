"""Tests for configuration loading and authorization.

The real ``.env`` file is never touched: ``load_dotenv`` and ``os.getenv`` are
replaced with in-memory fakes, so the tests also work on a machine without
production credentials.
"""

import warnings
from pathlib import Path

import pytest

import config


def _patch_env(monkeypatch, values):
    """Replace dotenv loading and environment lookups with a dict."""
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: None)

    def fake_getenv(name, default=None):
        return values.get(name, default)

    monkeypatch.setattr(config.os, "getenv", fake_getenv)


def test_missing_bot_token_raises(monkeypatch):
    _patch_env(monkeypatch, {"ALLOWED_USER_IDS": "1"})

    with pytest.raises(ValueError, match="BOT_TOKEN"):
        config.load_config()


def test_parses_user_ids_with_whitespace_and_empty_entries(monkeypatch):
    _patch_env(
        monkeypatch,
        {
            "BOT_TOKEN": "123:abcdef",
            "ALLOWED_USER_IDS": "  111 , 222 ,, ,333  ",
        },
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # an unexpected warning fails the test
        loaded = config.load_config()

    assert loaded["BOT_TOKEN"] == "123:abcdef"
    assert loaded["ALLOWED_USER_IDS"] == [111, 222, 333]
    assert loaded["BASE_DIR"] == Path(config.__file__).resolve().parent
    assert loaded["DATA_DIR"] == loaded["BASE_DIR"] / "data"
    assert loaded["CONFIG_DIR"] == loaded["BASE_DIR"] / "config"
    assert loaded["LOG_DIR"] == loaded["BASE_DIR"] / "logs"


def test_invalid_user_id_raises(monkeypatch):
    _patch_env(
        monkeypatch,
        {"BOT_TOKEN": "token", "ALLOWED_USER_IDS": "111, not-a-number"},
    )

    with pytest.raises(ValueError, match="Invalid ALLOWED_USER_IDS"):
        config.load_config()


def test_empty_allowlist_warns(monkeypatch):
    _patch_env(monkeypatch, {"BOT_TOKEN": "token", "ALLOWED_USER_IDS": "   "})

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        loaded = config.load_config()

    assert loaded["ALLOWED_USER_IDS"] == []
    assert any(issubclass(item.category, UserWarning) for item in caught)


def test_missing_allowlist_value_defaults_to_empty_and_warns(monkeypatch):
    _patch_env(monkeypatch, {"BOT_TOKEN": "token"})

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        loaded = config.load_config()

    assert loaded["ALLOWED_USER_IDS"] == []
    assert any(issubclass(item.category, UserWarning) for item in caught)


def test_is_user_authorized_uses_loaded_allowlist(monkeypatch):
    monkeypatch.setitem(config.config, "ALLOWED_USER_IDS", [42, 99])

    assert config.is_user_authorized(42) is True
    assert config.is_user_authorized(99) is True
    assert config.is_user_authorized(7) is False
    assert config.is_user_authorized(0) is False


def test_is_user_authorized_with_empty_allowlist(monkeypatch):
    monkeypatch.setitem(config.config, "ALLOWED_USER_IDS", [])

    assert config.is_user_authorized(123) is False