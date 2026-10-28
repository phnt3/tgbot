"""HTTP server for the local web panel.

The server is intentionally small and standard-library only:

* static files are served from an explicit whitelist;
* every ``/api`` call except login/setup needs a live session cookie;
* state-changing calls additionally need the per-session CSRF header;
* downloads are streamed from disk instead of being buffered in memory.

A bind failure (for example the port is already used) is reported to the
caller; the bot keeps running either way.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import shutil
import socket
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from .api import MAX_UPLOAD_BYTES, ApiError, PanelAPI, Request, Response, json_response
from .auth import (
    COOKIE_NAME,
    PasswordError,
    PasswordStore,
    LoginLimiter,
    SessionStore,
    validate_password,
)
from .panel_config import load_panel_config, panel_urls

logger = logging.getLogger("web.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"

STATIC_FILES: Dict[str, Tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
}

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "font-src 'self' data:; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}

MAX_BODY_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024

def _guess_lan_ip() -> str:
    """Best-effort local LAN address (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(str(address)).is_loopback
    except ValueError:
        return address in {"localhost", "::1"}


class PanelServer:
    """Owns the HTTP server, auth state and API router."""

    def __init__(
        self,
        *,
        data_dir: Any,
        db_path: Any,
        scheduler: Optional[Any] = None,
        telegram_status: Optional[Any] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = Path(db_path)
        self.scheduler = scheduler
        self.telegram_status = telegram_status

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config = load_panel_config(self.data_dir / "web_panel.json")
        self.host = str(host if host is not None else self.config["host"])
        self.port = int(port if port is not None else self.config["port"])

        self.passwords = PasswordStore(self.data_dir / "web_auth.json")
        self.sessions = SessionStore()
        self.limiter = LoginLimiter()

        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.started_at = 0.0

        lan_ip = _guess_lan_ip()
        urls = panel_urls(self.host, self.port)
        if self.host in {"0.0.0.0", "::"}:
            urls["lan"] = f"http://{lan_ip}:{self.port}/"
        self.panel_info: Dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "urls": urls,
            "lan_ip": lan_ip,
            "enabled": bool(self.config.get("enabled", True)),
        }

        self.api = PanelAPI(
            data_dir=self.data_dir,
            db_path=self.db_path,
            scheduler=scheduler,
            panel_info=self.panel_info,
            telegram_status=telegram_status,
        )

    # -- lifecycle ------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.httpd is not None

    def start(self) -> bool:
        """Bind and serve in a daemon thread. Returns False when disabled."""
        if self.httpd is not None:
            return True
        if not self.config.get("enabled", True):
            logger.info("Web panel is disabled in web_panel.json")
            return False

        server = _PanelHTTPServer((self.host, self.port), PanelRequestHandler)
        server.panel = self
        self.httpd = server
        self.started_at = time.time()
        self.panel_info["started_at"] = self.started_at

        self.thread = threading.Thread(
            target=server.serve_forever,
            name="web-panel",
            daemon=True,
        )
        self.thread.start()
        logger.info(
            "Web panel listening on %s:%s (local %s, LAN %s)",
            self.host,
            self.port,
            self.panel_info["urls"]["local"],
            self.panel_info["urls"]["lan"],
        )
        return True

    def stop(self) -> None:
        server, self.httpd = self.httpd, None
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # pragma: no cover - defensive
                pass
            server.server_close()
        thread, self.thread = self.thread, None
        if thread is not None:
            thread.join(timeout=5)

    # -- helpers used by the handler -----------------------------------

    def telegram_state(self) -> str:
        if callable(self.telegram_status):
            try:
                return str(self.telegram_status())
            except Exception:  # pragma: no cover - defensive
                return "unknown"
        return "unknown"

    def client_session(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        return self.sessions.get(token)


class _PanelHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    panel: PanelServer

    def handle_error(self, request: Any, client_address: Any) -> None:
        logger.debug("Unhandled panel connection error from %s", client_address)


class PanelRequestHandler(BaseHTTPRequestHandler):
    """Translate HTTP requests into panel responses."""

    server_version = "PCRemotePanel/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 120

    # -- entry points ---------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._handle("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # -- plumbing -------------------------------------------------------

    @property
    def panel(self) -> PanelServer:
        return self.server.panel  # type: ignore[attr-defined]

    def _client(self) -> str:
        return str(self.client_address[0])

    def _loopback(self) -> bool:
        return _is_loopback(self._client())

    def _handle(self, method: str) -> None:
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query, keep_blank_values=True)

            if path == "/api" or path.startswith("/api/"):
                self._handle_api(method, path, query)
            elif method in {"GET", "HEAD"}:
                self._handle_static(path, head=method == "HEAD")
            else:
                self._send_json(
                    {"ok": False, "error": "Метод не поддерживается"}, 405
                )
        except ApiError as e:
            self._send_json({"ok": False, "error": e.message}, e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logger.exception("Panel request failed: %s %s", method, self.path)
            self._send_json(
                {"ok": False, "error": "Внутренняя ошибка панели"}, 500
            )

    def _handle_static(self, path: str, head: bool = False) -> None:
        entry = STATIC_FILES.get(path)
        if entry is None:
            self._send_json({"ok": False, "error": "Не найдено"}, 404)
            return
        filename, content_type = entry
        target = (STATIC_DIR / filename).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR:
            self._send_json({"ok": False, "error": "Не найдено"}, 404)
            return
        try:
            payload = target.read_bytes()
        except OSError:
            logger.error("Missing static file: %s", target)
            self._send_json(
                {"ok": False, "error": "Панель не собрана: нет " + filename}, 500
            )
            return
        self._write(200, payload, content_type, {}, head=head)

    def _handle_api(
        self, method: str, path: str, query: Dict[str, Any]
    ) -> None:
        panel = self.panel
        token = self._cookie_token()
        session = panel.client_session(token)

        if path == "/api/session":
            self._send_json(self._session_payload(session))
            return

        if path == "/api/login":
            self._require_method(method, "POST")
            self._handle_login()
            return

        if path == "/api/setup":
            self._require_method(method, "POST")
            self._handle_setup()
            return

        if path == "/api/logout":
            self._require_method(method, "POST")
            if session is not None:
                supplied = self.headers.get("X-CSRF-Token", "")
                if not hmac.compare_digest(str(supplied), str(session["csrf"])):
                    self._send_json(
                        {"ok": False, "error": "Неверный CSRF-токен"}, 403
                    )
                    return
            panel.sessions.destroy(token)
            self._send_json(
                {"ok": True},
                cookies=self._clear_cookie(),
            )
            return

        if session is None:
            self._send_json(
                {"ok": False, "error": "Требуется вход", "login": True},
                401,
            )
            return

        if method in {"POST", "PUT", "DELETE"}:
            supplied = self.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(str(supplied), str(session["csrf"])):
                self._send_json(
                    {"ok": False, "error": "Неверный CSRF-токен"}, 403
                )
                return

        if path == "/api/password":
            self._require_method(method, "POST")
            self._handle_password(session, token)
            return

        body = self._read_body(path)
        request = Request(
            method=method,
            path=path,
            query=query,
            body=body,
            content_type=self.headers.get("Content-Type", ""),
            session=session,
            client=self._client(),
        )
        response = panel.api.dispatch(request)
        self._send_response(response)

    def _require_method(self, method: str, expected: str) -> None:
        if method != expected:
            raise ApiError(405, "Метод не поддерживается")

    # -- auth -----------------------------------------------------------

    def _session_payload(self, session: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        panel = self.panel
        has_password = panel.passwords.exists()
        authenticated = session is not None
        return {
            "ok": True,
            "authenticated": authenticated,
            "needs_setup": not has_password,
            "setup_allowed": (not has_password) and self._loopback(),
            "csrf": session["csrf"] if session else None,
            "panel": panel.panel_info,
            "telegram": panel.telegram_state(),
            "version": 1,
        }

    def _handle_login(self) -> None:
        panel = self.panel
        client = self._client()
        retry = panel.limiter.retry_after(client)
        if retry > 0:
            self._send_json(
                {
                    "ok": False,
                    "error": f"Слишком много попыток. Подождите {int(retry) + 1} с.",
                    "retry_after": int(retry) + 1,
                },
                429,
                headers={"Retry-After": str(int(retry) + 1)},
            )
            return

        if not panel.passwords.exists():
            self._send_json(
                {
                    "ok": False,
                    "error": "Пароль ещё не задан. Откройте панель на самом ПК.",
                    "needs_setup": True,
                },
                409,
            )
            return

        data = self._read_json()
        password = data.get("password")
        if not panel.passwords.verify(password):
            lock = panel.limiter.record_failure(client)
            message = "Неверный пароль"
            if lock:
                message = f"Слишком много попыток. Вход заблокирован на {int(lock)} с."
            self._send_json({"ok": False, "error": message}, 401)
            return

        panel.limiter.record_success(client)
        session = panel.sessions.create(
            client=client, user_agent=self.headers.get("User-Agent", "")
        )
        self._send_json(
            {
                "ok": True,
                "csrf": session["csrf"],
                "panel": panel.panel_info,
            },
            cookies=self._session_cookie(session["token"]),
        )

    def _handle_setup(self) -> None:
        panel = self.panel
        if panel.passwords.exists():
            self._send_json(
                {"ok": False, "error": "Пароль уже задан"}, 409
            )
            return
        if not self._loopback():
            self._send_json(
                {
                    "ok": False,
                    "error": "Первый пароль можно задать только с этого ПК",
                },
                403,
            )
            return

        data = self._read_json()
        try:
            password = validate_password(data.get("password"))
        except PasswordError as e:
            self._send_json({"ok": False, "error": self._password_message(e)}, 400)
            return

        panel.passwords.set_password(password)
        session = panel.sessions.create(
            client=self._client(), user_agent=self.headers.get("User-Agent", "")
        )
        self._send_json(
            {
                "ok": True,
                "csrf": session["csrf"],
                "panel": panel.panel_info,
            },
            cookies=self._session_cookie(session["token"]),
        )

    def _handle_password(
        self, session: Dict[str, Any], token: Optional[str]
    ) -> None:
        panel = self.panel
        data = self._read_json()
        if not panel.passwords.verify(data.get("current")):
            self._send_json(
                {"ok": False, "error": "Текущий пароль неверен"}, 400
            )
            return
        try:
            password = validate_password(data.get("new"))
        except PasswordError as e:
            self._send_json({"ok": False, "error": self._password_message(e)}, 400)
            return
        if data.get("current") == password:
            self._send_json(
                {"ok": False, "error": "Новый пароль совпадает с текущим"}, 400
            )
            return

        panel.passwords.set_password(password)
        panel.sessions.destroy_all()
        fresh = panel.sessions.create(
            client=self._client(), user_agent=self.headers.get("User-Agent", "")
        )
        self._send_json(
            {
                "ok": True,
                "csrf": fresh["csrf"],
                "message": "Пароль обновлён, другие сеансы завершены",
            },
            cookies=self._session_cookie(fresh["token"]),
        )

    @staticmethod
    def _password_message(error: Exception) -> str:
        text = str(error)
        replacements = {
            "Password must be at least 8 characters": "Пароль должен быть не короче 8 символов",
            "Password must be at most 200 characters": "Пароль слишком длинный",
            "Password must not start or end with spaces": "Пароль не должен начинаться или заканчиваться пробелом",
            "Password must be a string": "Некорректный пароль",
        }
        return replacements.get(text, text)

    # -- cookies --------------------------------------------------------

    def _cookie_token(self) -> Optional[str]:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:
            return None
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _session_cookie(self, token: str) -> Dict[str, str]:
        from .auth import SESSION_ABSOLUTE_SECONDS

        value = (
            f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={int(SESSION_ABSOLUTE_SECONDS)}"
        )
        return {"Set-Cookie": value}

    def _clear_cookie(self) -> Dict[str, str]:
        return {
            "Set-Cookie": (
                f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
            )
        }

    # -- request body ---------------------------------------------------

    def _read_json(self) -> Dict[str, Any]:
        body = self._read_body(None, limit=MAX_JSON_BYTES)
        if not body:
            return {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ApiError(400, "Некорректный JSON") from e
        if not isinstance(payload, dict):
            raise ApiError(400, "Ожидался JSON-объект")
        return payload

    def _read_body(self, path: Optional[str], limit: int = MAX_BODY_BYTES) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as e:
            raise ApiError(400, "Некорректная длина запроса") from e
        if length < 0:
            raise ApiError(400, "Некорректная длина запроса")
        if length > limit:
            raise ApiError(413, "Слишком большой запрос")
        if length == 0:
            return b""
        chunks = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 256 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining:
            raise ApiError(400, "Запрос оборвался")
        return b"".join(chunks)

    # -- responses ------------------------------------------------------

    def _send_json(
        self,
        payload: Any,
        status: int = 200,
        *,
        cookies: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        response = json_response(payload, status)
        if cookies:
            response.headers.update(cookies)
        if headers:
            response.headers.update(headers)
        self._send_response(response)

    def _send_response(self, response: Response) -> None:
        if response.file_path is not None:
            self._stream_file(response)
            return
        self._write(
            response.status,
            response.body,
            response.content_type,
            response.headers,
        )

    def _stream_file(self, response: Response) -> None:
        path = Path(response.file_path)
        try:
            stat = path.stat()
            handle = open(path, "rb")
        except OSError as e:
            self._send_json(
                {"ok": False, "error": f"Не удалось открыть файл: {e}"}, 404
            )
            return
        with handle:
            self.send_response(response.status)
            self._common_headers()
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(stat.st_size))
            for key, value in response.headers.items():
                if key.lower() == "content-length":
                    continue
                self.send_header(key, value)
            self.end_headers()
            try:
                shutil.copyfileobj(handle, self.wfile, length=256 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _write(
        self,
        status: int,
        body: bytes,
        content_type: str,
        headers: Dict[str, str],
        *,
        head: bool = False,
    ) -> None:
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            if key.lower() == "content-length":
                continue
            self.send_header(key, value)
        if status >= 400 or "/api/" in self.path:
            self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        if not head and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _common_headers(self) -> None:
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.send_header("X-Robots-Tag", "noindex, nofollow")