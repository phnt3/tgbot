"""Tests for the web panel password, session and login-limit helpers.

These tests never touch ``data/web_auth.json``: every ``PasswordStore`` points
at a temporary directory.
"""

import json

import pytest

from web.auth import (
    LoginLimiter,
    PasswordError,
    PasswordStore,
    SessionStore,
    validate_password,
)


@pytest.fixture
def store(tmp_path):
    return PasswordStore(tmp_path / "data" / "web_auth.json")


# -- password policy ----------------------------------------------------


def test_validate_password_accepts_normal_password():
    assert validate_password("correct horse") == "correct horse"


@pytest.mark.parametrize("value", ["", "short", "x" * 201])
def test_validate_password_rejects_bad_lengths(value):
    with pytest.raises(PasswordError):
        validate_password(value)


def test_validate_password_rejects_padding():
    with pytest.raises(PasswordError):
        validate_password(" padded pass ")


def test_validate_password_requires_string():
    with pytest.raises(PasswordError):
        validate_password(None)


# -- password storage ---------------------------------------------------


def test_password_store_starts_empty(store):
    assert store.exists() is False
    assert store.verify("anything") is False


def test_set_password_writes_only_a_hash(store):
    store.set_password("sup3r-secret")

    assert store.exists() is True
    raw = store.path.read_text(encoding="utf-8")
    assert "sup3r-secret" not in raw

    data = json.loads(raw)
    assert data["algorithm"] == "pbkdf2-sha256"
    assert data["iterations"] >= 240_000
    assert data["salt"] and data["hash"]

    assert store.verify("sup3r-secret") is True
    assert store.verify("wrong-secret") is False
    assert store.verify("") is False
    assert store.verify(None) is False


def test_set_password_rejects_short_value_without_creating_file(store):
    with pytest.raises(PasswordError):
        store.set_password("short")
    assert not store.path.exists()


def test_password_store_ignores_corrupt_file(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")

    assert store.exists() is False
    assert store.verify("whatever") is False


def test_password_store_rejects_weak_parameters(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "algorithm": "pbkdf2-sha256",
                "iterations": 100,
                "salt": "AAAA",
                "hash": "AAAA",
            }
        ),
        encoding="utf-8",
    )

    assert store.exists() is False


# -- sessions -----------------------------------------------------------


def test_session_create_and_destroy():
    sessions = SessionStore()
    record = sessions.create(client="10.0.0.2", user_agent="test-agent")

    assert record["token"] and record["csrf"]
    found = sessions.get(record["token"])
    assert found is not None
    assert found["client"] == "10.0.0.2"
    assert found["csrf"] == record["csrf"]

    assert sessions.get("missing") is None
    assert sessions.get(None) is None
    assert sessions.destroy(record["token"]) is True
    assert sessions.get(record["token"]) is None
    assert sessions.destroy(record["token"]) is False


def test_session_expires_after_idle_timeout():
    sessions = SessionStore(idle_seconds=-1, absolute_seconds=3600)
    record = sessions.create()
    assert sessions.get(record["token"]) is None


def test_session_expires_after_absolute_timeout():
    sessions = SessionStore(idle_seconds=3600, absolute_seconds=-1)
    record = sessions.create()
    assert sessions.get(record["token"]) is None


def test_session_store_evicts_oldest():
    sessions = SessionStore(max_sessions=2)
    first = sessions.create()
    second = sessions.create()
    third = sessions.create()

    assert sessions.count() == 2
    assert sessions.get(first["token"]) is None
    assert sessions.get(second["token"]) is not None
    assert sessions.get(third["token"]) is not None


def test_destroy_all_clears_sessions():
    sessions = SessionStore()
    sessions.create()
    sessions.create()

    assert sessions.destroy_all() == 2
    assert sessions.count() == 0


# -- login limiter ------------------------------------------------------


def test_limiter_locks_after_repeated_failures():
    limiter = LoginLimiter(max_failures=3, window_seconds=600, lock_seconds=30)

    assert limiter.retry_after("1.2.3.4") == 0
    assert limiter.record_failure("1.2.3.4") == 0
    assert limiter.record_failure("1.2.3.4") == 0
    assert limiter.record_failure("1.2.3.4") == 30

    assert limiter.retry_after("1.2.3.4") > 0
    assert limiter.retry_after("5.6.7.8") == 0


def test_limiter_success_resets_failures():
    limiter = LoginLimiter(max_failures=2)
    limiter.record_failure("ip")
    limiter.record_success("ip")

    assert limiter.retry_after("ip") == 0
    assert limiter.record_failure("ip") == 0


def test_limiter_lock_can_expire_immediately():
    limiter = LoginLimiter(max_failures=1, lock_seconds=-1)
    limiter.record_failure("ip")

    assert limiter.retry_after("ip") == 0


def test_limiter_forgets_failures_outside_the_window():
    limiter = LoginLimiter(max_failures=2, window_seconds=-1, lock_seconds=60)
    assert limiter.record_failure("ip") == 0
    assert limiter.retry_after("ip") == 0