"""Integration tests for the local web panel HTTP server.

The server binds to a temporary loopback port and uses a temporary database
and data directory, so the live ``data/`` files are never touched.
"""

import http.client
import json
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from urllib.parse import urlencode, urlsplit

import pytest

import settings_store
from web.auth import COOKIE_NAME, LoginLimiter
from web.server import MAX_BODY_BYTES, PanelServer


# -- helpers ------------------------------------------------------------


class Client:
    """Minimal cookie/CSRF-aware HTTP client built on urllib."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.cookies = {}
        self.csrf = None

    def _headers(self):
        headers = {}
        if self.cookies:
            headers["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in self.cookies.items()
            )
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        return headers

    def _store_cookies(self, headers) -> None:
        for header in headers.get_all("Set-Cookie") or []:
            jar = SimpleCookie()
            jar.load(header)
            for name, morsel in jar.items():
                if str(morsel["max-age"]) == "0":
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = morsel.value

    def request(self, method, path, *, json_body=None, raw=False):
        headers = self._headers()
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                status = response.status
                payload = response.read()
                response_headers = response.headers
        except urllib.error.HTTPError as error:
            status = error.code
            payload = error.read()
            response_headers = error.headers

        self._store_cookies(response_headers)
        if raw:
            return status, payload, response_headers
        try:
            body = json.loads(payload)
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("csrf"):
            self.csrf = body["csrf"]
        return status, body, response_headers

    def setup(self, password="panel-secret-1"):
        return self.request(
            "POST", "/api/setup", json_body={"password": password}
        )

    def login(self, password="panel-secret-1"):
        return self.request(
            "POST", "/api/login", json_body={"password": password}
        )


@pytest.fixture
def panel_server(tmp_path, monkeypatch):
    """Start an isolated panel and yield ``(server, base_url)``."""
    seeds = {
        settings_store.APPLICATIONS: {"Alpha": r"C:\Games\alpha.exe"},
        settings_store.PROFILES: {
            "GAMING": {
                "description": "play",
                "apps": ["Alpha"],
                "optional": [],
            }
        },
        settings_store.ALERTS: {
            "thresholds": {"cpu_temperature": 90},
            "cooldowns": {"cpu_temperature": 300},
            "interval_seconds": 60,
        },
        settings_store.NOTIFICATIONS: {
            "battery_warning": 20,
            "notify_login": True,
        },
    }
    monkeypatch.setattr(
        settings_store,
        "_SEEDS",
        {
            key: (lambda value=value: json.loads(json.dumps(value)))
            for key, value in seeds.items()
        },
    )

    server = PanelServer(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "panel.db",
        host="127.0.0.1",
        port=0,
    )
    assert server.start() is True
    port = server.httpd.server_address[1]
    try:
        yield server, f"http://127.0.0.1:{port}"
    finally:
        server.stop()


def test_static_index_is_public_and_hardened(panel_server):
    _, base = panel_server
    status, payload, headers = Client(base).request("GET", "/", raw=True)

    assert status == 200
    assert b'<div id="auth"' in payload
    assert headers.get("Content-Type").startswith("text/html")
    assert "default-src 'self'" in headers.get("Content-Security-Policy", "")
    assert headers.get("X-Frame-Options") == "DENY"
    assert headers.get("X-Content-Type-Options") == "nosniff"


def test_static_head_returns_headers_without_body(panel_server):
    _, base = panel_server
    status, payload, headers = Client(base).request("HEAD", "/", raw=True)

    assert status == 200
    assert payload == b""
    assert int(headers.get("Content-Length")) > 0


def test_unknown_paths_are_json_404(panel_server):
    _, base = panel_server
    client = Client(base)

    status, body, _ = client.request("GET", "/does-not-exist")
    assert status == 404
    assert body["ok"] is False

    status, body, _ = client.request("POST", "/")
    assert status == 405
    assert body["ok"] is False


def test_fresh_install_requests_setup(panel_server):
    _, base = panel_server
    status, body, _ = Client(base).request("GET", "/api/session")

    assert status == 200
    assert body["ok"] is True
    assert body["authenticated"] is False
    assert body["needs_setup"] is True
    assert body["setup_allowed"] is True
    assert body["csrf"] is None


def test_login_before_setup_returns_conflict(panel_server):
    _, base = panel_server
    status, body, _ = Client(base).login("anything-goes")

    assert status == 409
    assert body["needs_setup"] is True


def test_setup_creates_password_and_session(panel_server):
    server, base = panel_server
    client = Client(base)

    status, body, headers = client.request(
        "POST", "/api/setup", json_body={"password": "panel-secret-1"}
    )
    assert status == 200
    assert body["ok"] is True
    assert body["csrf"] == client.csrf
    cookie_header = headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie_header
    assert "SameSite=Strict" in cookie_header

    assert server.passwords.exists() is True

    status, session, _ = client.request("GET", "/api/session")
    assert status == 200
    assert session["authenticated"] is True
    assert session["needs_setup"] is False
    assert session["setup_allowed"] is False
    assert session["csrf"] == client.csrf


def test_setup_rejects_short_password(panel_server):
    server, base = panel_server
    client = Client(base)

    status, body, _ = client.request(
        "POST", "/api/setup", json_body={"password": "short"}
    )
    assert status == 400
    assert body["ok"] is False
    assert server.passwords.exists() is False


def test_setup_cannot_run_twice(panel_server):
    _, base = panel_server
    client = Client(base)
    client.setup()

    status, body, _ = Client(base).request(
        "POST", "/api/setup", json_body={"password": "another-secret"}
    )
    assert status == 409
    assert body["ok"] is False


def test_setup_from_lan_is_forbidden(panel_server, monkeypatch):
    import web.server as server_module

    monkeypatch.setattr(server_module, "_is_loopback", lambda address: False)
    server, base = panel_server
    client = Client(base)

    status, session, _ = client.request("GET", "/api/session")
    assert session["setup_allowed"] is False

    status, body, _ = client.request(
        "POST", "/api/setup", json_body={"password": "panel-secret-1"}
    )
    assert status == 403
    assert server.passwords.exists() is False


def test_login_rejects_bad_password_and_accepts_good_one(panel_server):
    server, base = panel_server
    server.passwords.set_password("panel-secret-1")
    client = Client(base)

    status, body, _ = client.login("wrong-secret")
    assert status == 401
    assert body["ok"] is False
    assert COOKIE_NAME not in client.cookies

    status, body, _ = client.login("panel-secret-1")
    assert status == 200
    assert body["ok"] is True
    assert body["csrf"] == client.csrf
    assert server.sessions.count() == 1


def test_login_rate_limit_locks_the_client(panel_server):
    server, base = panel_server
    server.passwords.set_password("panel-secret-1")
    server.limiter = LoginLimiter(max_failures=2, lock_seconds=60)
    client = Client(base)

    assert client.login("wrong-secret")[0] == 401
    assert client.login("wrong-secret")[0] == 401

    status, body, headers = client.login("panel-secret-1")
    assert status == 429
    assert body["ok"] is False
    assert body["retry_after"] > 0
    assert int(headers.get("Retry-After")) > 0


def test_api_requires_a_session(panel_server):
    _, base = panel_server
    client = Client(base)

    for path in ("/api/settings", "/api/files", "/api/processes"):
        status, body, _ = client.request("GET", path)
        assert status == 401
        assert body["login"] is True


def test_logout_requires_csrf_and_clears_the_cookie(panel_server):
    _, base = panel_server
    client = Client(base)
    client.setup()
    csrf = client.csrf

    client.csrf = None
    status, body, _ = client.request("POST", "/api/logout")
    assert status == 403
    assert body["ok"] is False

    client.csrf = csrf
    status, body, _ = client.request("POST", "/api/logout")
    assert status == 200
    assert body["ok"] is True
    assert COOKIE_NAME not in client.cookies

    status, session, _ = client.request("GET", "/api/session")
    assert session["authenticated"] is False


def test_password_change_rotates_sessions(panel_server):
    server, base = panel_server
    client = Client(base)
    client.setup("panel-secret-1")
    old_token = client.cookies[COOKIE_NAME]

    status, body, _ = client.request(
        "POST",
        "/api/password",
        json_body={"current": "panel-secret-1", "new": "panel-secret-2"},
    )
    assert status == 200
    assert body["ok"] is True
    assert body["csrf"] == client.csrf
    assert client.cookies[COOKIE_NAME] != old_token

    status, session, _ = client.request("GET", "/api/session")
    assert session["authenticated"] is True

    stale = Client(base)
    stale.cookies[COOKIE_NAME] = old_token
    status, body, _ = stale.request("GET", "/api/settings")
    assert status == 401

    fresh = Client(base)
    assert fresh.login("panel-secret-1")[0] == 401
    assert fresh.login("panel-secret-2")[0] == 200


def test_password_change_requires_the_current_password(panel_server):
    _, base = panel_server
    client = Client(base)
    client.setup("panel-secret-1")

    status, body, _ = client.request(
        "POST",
        "/api/password",
        json_body={"current": "wrong", "new": "panel-secret-2"},
    )
    assert status == 400
    assert body["ok"] is False


def test_settings_round_trip(panel_server):
    _, base = panel_server
    client = Client(base)
    client.setup()

    sections = {
        "applications": {"TestApp": r"C:\Tools\test.exe"},
        "profiles": {
            "TEST": {
                "description": "test profile",
                "apps": ["TestApp"],
                "optional": [],
            }
        },
        "alerts": {
            "thresholds": {"cpu": 90},
            "cooldowns": {"cpu": 300},
            "interval_seconds": 30,
        },
        "notifications": {
            "battery_warning": 25,
            "battery_critical": 10,
            "notify_login": False,
        },
    }

    status, body, _ = client.request(
        "POST", "/api/settings", json_body={"sections": sections}
    )
    assert status == 200
    assert body["ok"] is True
    assert body["sections"]["applications"] == {"TestApp": r"C:\Tools\test.exe"}
    assert body["sections"]["notifications"]["battery_warning"] == 25
    assert body["sections"]["notifications"]["notify_login"] is False

    status, body, _ = client.request("GET", "/api/settings")
    assert status == 200
    assert body["sections"]["applications"] == {"TestApp": r"C:\Tools\test.exe"}
    assert body["sections"]["notifications"]["battery_warning"] == 25
    assert set(body["available"]) == {
        "applications",
        "profiles",
        "alerts",
        "notifications",
    }


def test_settings_reject_invalid_payload(panel_server):
    _, base = panel_server
    client = Client(base)
    client.setup()

    status, body, _ = client.request(
        "POST",
        "/api/settings",
        json_body={"sections": {"applications": ["not", "a", "dict"]}},
    )
    assert status == 400
    assert body["ok"] is False

    status, body, _ = client.request(
        "POST", "/api/settings", json_body={"sections": {}}
    )
    assert status == 400


def test_files_without_path_lists_drives(panel_server):
    _, base = panel_server
    client = Client(base)
    client.setup()

    status, body, _ = client.request("GET", "/api/files")
    assert status == 200
    assert body["kind"] == "drives"
    assert isinstance(body["drives"], list)


def test_file_download_streams_the_bytes(panel_server, tmp_path):
    _, base = panel_server
    client = Client(base)
    client.setup()

    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"hello-panel")

    status, payload, headers = client.request(
        "GET",
        "/api/files/download?" + urlencode({"path": str(sample)}),
        raw=True,
    )
    assert status == 200
    assert payload == b"hello-panel"
    assert headers.get("Content-Type") == "application/octet-stream"
    assert headers.get("Content-Length") == "11"
    assert "attachment" in headers.get("Content-Disposition", "")
    assert "sample.bin" in headers.get("Content-Disposition", "")


def test_missing_download_is_404(panel_server, tmp_path):
    _, base = panel_server
    client = Client(base)
    client.setup()

    status, body, _ = client.request(
        "GET",
        "/api/files/download?"
        + urlencode({"path": str(tmp_path / "missing.bin")}),
    )
    assert status == 404
    assert body["ok"] is False


def test_oversized_body_is_rejected_before_reading(panel_server):
    server, base = panel_server
    client = Client(base)
    client.setup()

    parts = urlsplit(base)
    connection = http.client.HTTPConnection(
        parts.hostname, parts.port, timeout=15
    )
    try:
        connection.putrequest("POST", "/api/files/upload")
        connection.putheader(
            "Cookie", f"{COOKIE_NAME}={client.cookies[COOKIE_NAME]}"
        )
        connection.putheader("X-CSRF-Token", client.csrf)
        connection.putheader("Content-Type", "multipart/form-data; boundary=x")
        connection.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
        connection.endheaders()

        response = connection.getresponse()
        payload = response.read()
    finally:
        connection.close()

    assert response.status == 413
    assert json.loads(payload)["ok"] is False


def test_scheduler_endpoint_uses_the_scheduler_object(panel_server):
    server, base = panel_server

    class FakeScheduler:
        def list_tasks(self):
            return [
                {
                    "id": "task-1",
                    "action": "lock",
                    "label": "Блокировка",
                    "run_at": time.time() + 60,
                    "params": {},
                }
            ]

    server.api.scheduler = FakeScheduler()

    client = Client(base)
    client.setup()

    status, body, _ = client.request("GET", "/api/scheduler")
    assert status == 200
    assert body["ok"] is True
    assert body["tasks"][0]["id"] == "task-1"
    assert body["actions"]


def test_alerts_acknowledge_returns_ok(panel_server):
    _, base = panel_server
    client = Client(base)
    client.setup()

    status, body, _ = client.request(
        "POST", "/api/alerts/ack", json_body={}
    )
    assert status == 200
    assert body["ok"] is True
    assert isinstance(body["updated"], int)


def test_panel_disabled_in_config_does_not_bind(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "web_panel.json").write_text(
        json.dumps({"enabled": False, "host": "127.0.0.1", "port": 8090}),
        encoding="utf-8",
    )
    server = PanelServer(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "panel.db",
        host="127.0.0.1",
        port=0,
    )

    assert server.start() is False
    assert server.httpd is None
    server.stop()