"""Authentication for the local web panel.

Design:
- One password, stored as PBKDF2-HMAC-SHA256 (240k iterations, 16-byte salt)
  in ``data/web_auth.json``. The plaintext is never written anywhere.
- Sessions live in memory only, so a bot restart logs everybody out.
- Every session carries its own CSRF token. State-changing requests must send
  it in the ``X-CSRF-Token`` header (the cookie alone is not enough).
- Failed logins are rate-limited per client address with a short lockout.

The file is created by the first-run setup, which is only accepted from a
loopback address (i.e. while sitting at the PC itself).
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("web.auth")

PBKDF2_ITERATIONS = 240_000
SALT_BYTES = 16
KEY_BYTES = 32
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 200

SESSION_IDLE_SECONDS = 12 * 3600
SESSION_ABSOLUTE_SECONDS = 7 * 24 * 3600
SESSION_MAX_COUNT = 50

LOGIN_MAX_FAILURES = 5
LOGIN_FAILURE_WINDOW = 600.0
LOGIN_LOCK_SECONDS = 300.0

COOKIE_NAME = "pcpanel_session"


class PasswordError(ValueError):
    """Raised when a password does not meet the minimum policy."""


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def validate_password(password: Any) -> str:
    """Return the password when it is acceptable, else raise ``PasswordError``."""
    if not isinstance(password, str):
        raise PasswordError("Password must be a string")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordError(
            f"Password must be at most {MAX_PASSWORD_LENGTH} characters"
        )
    if password.strip() != password:
        raise PasswordError("Password must not start or end with spaces")
    return password


def _hash_password(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=KEY_BYTES
    )


class PasswordStore:
    """Read, verify and replace the panel password file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    # -- storage --------------------------------------------------------

    def _load(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Cannot read %s: %s", self.path, e)
            return None
        if not isinstance(raw, dict):
            return None
        if raw.get("algorithm") != "pbkdf2-sha256":
            return None
        try:
            salt = _b64decode(str(raw["salt"]))
            digest = _b64decode(str(raw["hash"]))
            iterations = int(raw.get("iterations", PBKDF2_ITERATIONS))
        except (KeyError, ValueError, TypeError):
            return None
        if not salt or not digest or iterations < 10_000:
            return None
        return {
            "algorithm": "pbkdf2-sha256",
            "iterations": iterations,
            "salt": raw["salt"],
            "hash": raw["hash"],
            "_salt": salt,
            "_digest": digest,
        }

    def exists(self) -> bool:
        """Return True when a usable password has been configured."""
        with self._lock:
            return self._load() is not None

    def set_password(self, password: str) -> None:
        """Hash and atomically store a new password."""
        cleaned = validate_password(password)
        salt = secrets.token_bytes(SALT_BYTES)
        digest = _hash_password(cleaned, salt, PBKDF2_ITERATIONS)
        payload = {
            "version": 1,
            "algorithm": "pbkdf2-sha256",
            "iterations": PBKDF2_ITERATIONS,
            "salt": _b64encode(salt),
            "hash": _b64encode(digest),
            "updated_at": time.time(),
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + ".tmp")
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(temp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        logger.info("Web panel password updated")

    def verify(self, password: Any) -> bool:
        """Constant-time check of a candidate password."""
        if not isinstance(password, str) or not password:
            return False
        if len(password) > MAX_PASSWORD_LENGTH:
            return False
        with self._lock:
            record = self._load()
        if record is None:
            return False
        candidate = _hash_password(
            password, record["_salt"], int(record["iterations"])
        )
        return hmac.compare_digest(candidate, record["_digest"])


class SessionStore:
    """In-memory session registry with idle and absolute expiry."""

    def __init__(
        self,
        idle_seconds: float = SESSION_IDLE_SECONDS,
        absolute_seconds: float = SESSION_ABSOLUTE_SECONDS,
        max_sessions: int = SESSION_MAX_COUNT,
    ) -> None:
        self.idle_seconds = float(idle_seconds)
        self.absolute_seconds = float(absolute_seconds)
        self.max_sessions = int(max_sessions)
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def create(self, client: str = "", user_agent: str = "") -> Dict[str, Any]:
        """Create and return a new session record."""
        now = time.time()
        record = {
            "token": secrets.token_urlsafe(32),
            "csrf": secrets.token_urlsafe(32),
            "client": str(client or ""),
            "user_agent": str(user_agent or "")[:200],
            "created": now,
            "last_seen": now,
        }
        with self._lock:
            self._purge(now)
            if len(self._sessions) >= self.max_sessions:
                oldest = min(
                    self._sessions.values(), key=lambda item: item["last_seen"]
                )
                self._sessions.pop(oldest["token"], None)
            self._sessions[record["token"]] = record
        return dict(record)

    def get(self, token: Optional[str], touch: bool = True) -> Optional[Dict[str, Any]]:
        """Return a live session, or ``None`` when missing or expired."""
        if not token:
            return None
        now = time.time()
        with self._lock:
            record = self._sessions.get(token)
            if record is None:
                return None
            if now - record["last_seen"] > self.idle_seconds:
                self._sessions.pop(token, None)
                return None
            if now - record["created"] > self.absolute_seconds:
                self._sessions.pop(token, None)
                return None
            if touch:
                record["last_seen"] = now
            return dict(record)

    def destroy(self, token: Optional[str]) -> bool:
        """Remove one session. Returns True when it existed."""
        if not token:
            return False
        with self._lock:
            return self._sessions.pop(token, None) is not None

    def destroy_all(self) -> int:
        """Remove every session (used after a password change)."""
        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
            return count

    def count(self) -> int:
        with self._lock:
            self._purge(time.time())
            return len(self._sessions)

    def _purge(self, now: float) -> None:
        expired = [
            token
            for token, record in self._sessions.items()
            if now - record["last_seen"] > self.idle_seconds
            or now - record["created"] > self.absolute_seconds
        ]
        for token in expired:
            self._sessions.pop(token, None)


class LoginLimiter:
    """Per-client failed-login limiter with an exponential lockout."""

    def __init__(
        self,
        max_failures: int = LOGIN_MAX_FAILURES,
        window_seconds: float = LOGIN_FAILURE_WINDOW,
        lock_seconds: float = LOGIN_LOCK_SECONDS,
    ) -> None:
        self.max_failures = int(max_failures)
        self.window_seconds = float(window_seconds)
        self.lock_seconds = float(lock_seconds)
        self._state: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def retry_after(self, client: str) -> float:
        """Seconds until this client may try again (0 when allowed)."""
        now = time.time()
        with self._lock:
            entry = self._state.get(client)
            if not entry:
                return 0.0
            locked_until = float(entry.get("locked_until", 0.0))
            if locked_until > now:
                return locked_until - now
            if locked_until:
                entry["locked_until"] = 0.0
            failures = [
                stamp
                for stamp in entry.get("failures", [])
                if now - stamp <= self.window_seconds
            ]
            entry["failures"] = failures
            if not failures:
                self._state.pop(client, None)
            return 0.0

    def record_failure(self, client: str) -> float:
        """Record a failed attempt; return the lockout in seconds if triggered."""
        now = time.time()
        with self._lock:
            entry = self._state.setdefault(client, {"failures": [], "locked_until": 0.0})
            failures = [
                stamp
                for stamp in entry["failures"]
                if now - stamp <= self.window_seconds
            ]
            failures.append(now)
            entry["failures"] = failures
            if len(failures) >= self.max_failures:
                entry["failures"] = []
                entry["locked_until"] = now + self.lock_seconds
                return self.lock_seconds
            return 0.0

    def record_success(self, client: str) -> None:
        with self._lock:
            self._state.pop(client, None)

    def reset(self) -> None:
        with self._lock:
            self._state.clear()