"""JSON API for the local web panel.

Every handler runs in an HTTP worker thread and therefore opens its own
short-lived :class:`Database` connection (the bot's connection belongs to the
event loop thread and must not be shared).

Slow operations (speed test, traceroute, screenshot, process sampling) run in
these worker threads only, so the bot and the rest of the panel stay
responsive.
"""

from __future__ import annotations

import json
import logging
import math
import mimetypes
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote

import psutil

import application_manager
import charts
import clipboard_manager
import file_manager
import gaming_profiles
import media_control
import monitor
import network_info
import power_manager
import process_manager
import screenshot_manager
import settings_store
from database import Database

logger = logging.getLogger("web.api")

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_SEARCH_RESULTS = 50
CHART_METRICS = {"cpu", "ram", "gpu", "disk"}
CHART_RANGES = {"5m", "1h", "24h"}
MEDIA_KEYS = {
    "vol_up",
    "vol_down",
    "mute",
    "play_pause",
    "next",
    "prev",
    "stop",
}
POWER_ACTIONS = {"lock", "sleep", "hibernate", "display_off", "shutdown", "restart", "cancel"}


@dataclass
class Response:
    """A small transport-independent HTTP response."""

    status: int = 200
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: Dict[str, str] = field(default_factory=dict)
    file_path: Optional[Path] = None


def _json_safe(value: Any) -> Any:
    """Replace non-finite floats so browsers can parse the JSON.

    ``settings_store`` stores "never repeat" alert cooldowns as ``math.inf``.
    Python happily writes ``Infinity``, but that is not valid JSON and makes
    ``JSON.parse`` fail in the browser. ``null`` carries the same meaning.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def json_response(payload: Any, status: int = 200) -> Response:
    return Response(
        status=status,
        body=json.dumps(
            _json_safe(payload), ensure_ascii=False, default=str
        ).encode("utf-8"),
    )


class ApiError(Exception):
    """A request problem that should be shown to the user as JSON."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)


@dataclass
class Request:
    """Everything a handler needs to know about one API call."""

    method: str
    path: str
    query: Dict[str, List[str]]
    body: bytes = b""
    content_type: str = ""
    session: Optional[Dict[str, Any]] = None
    client: str = ""

    def q(self, name: str, default: Optional[str] = None) -> Optional[str]:
        values = self.query.get(name) or []
        return values[0] if values else default

    def json(self) -> Dict[str, Any]:
        if not self.body:
            return {}
        try:
            payload = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ApiError(400, "Некорректный JSON") from e
        if not isinstance(payload, dict):
            raise ApiError(400, "Ожидался JSON-объект")
        return payload


def _fmt_time(timestamp: Any) -> str:
    try:
        return datetime.fromtimestamp(float(timestamp)).strftime("%d.%m %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


class PanelAPI:
    """Route and execute panel API calls."""

    def __init__(
        self,
        *,
        data_dir: Path,
        db_path: Path,
        scheduler: Any = None,
        panel_info: Optional[Dict[str, Any]] = None,
        telegram_status: Optional[Any] = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = Path(db_path)
        self.scheduler = scheduler
        self.panel_info = dict(panel_info or {})
        self.telegram_status = telegram_status
        self._speedtest_lock = threading.Lock()
        self.routes: Dict[Tuple[str, str], str] = {
            ("GET", "/api/overview"): "overview",
            ("GET", "/api/chart"): "chart",
            ("GET", "/api/files"): "files_list",
            ("GET", "/api/files/search"): "files_search",
            ("GET", "/api/files/download"): "files_download",
            ("POST", "/api/files/action"): "files_action",
            ("POST", "/api/files/upload"): "files_upload",
            ("GET", "/api/processes"): "processes",
            ("POST", "/api/processes/kill"): "processes_kill",
            ("GET", "/api/apps"): "apps",
            ("POST", "/api/apps/launch"): "apps_launch",
            ("GET", "/api/profiles"): "profiles",
            ("POST", "/api/profiles/launch"): "profiles_launch",
            ("GET", "/api/media"): "media",
            ("POST", "/api/media/key"): "media_key",
            ("GET", "/api/power"): "power",
            ("POST", "/api/power"): "power_action",
            ("GET", "/api/clipboard"): "clipboard_get",
            ("POST", "/api/clipboard"): "clipboard_set",
            ("GET", "/api/screenshot"): "screenshot",
            ("GET", "/api/network"): "network",
            ("POST", "/api/network/ping"): "network_ping",
            ("POST", "/api/network/dns"): "network_dns",
            ("POST", "/api/network/traceroute"): "network_traceroute",
            ("POST", "/api/network/diagnostics"): "network_diagnostics",
            ("POST", "/api/network/speedtest"): "network_speedtest",
            ("GET", "/api/scheduler"): "scheduler_list",
            ("POST", "/api/scheduler/add"): "scheduler_add",
            ("POST", "/api/scheduler/cancel"): "scheduler_cancel",
            ("GET", "/api/settings"): "settings_get",
            ("POST", "/api/settings"): "settings_save",
            ("GET", "/api/settings/export"): "settings_export",
            ("POST", "/api/settings/import"): "settings_import",
            ("POST", "/api/settings/reset"): "settings_reset",
            ("POST", "/api/alerts/ack"): "alerts_ack",
        }

    # -- plumbing -------------------------------------------------------

    def dispatch(self, request: Request) -> Response:
        handler_name = self.routes.get((request.method, request.path))
        if handler_name is None:
            raise ApiError(404, "Не найдено")
        handler = getattr(self, handler_name)
        try:
            return handler(request)
        except ApiError:
            raise
        except FileNotFoundError as e:
            raise ApiError(404, f"Не найдено: {e}") from e
        except PermissionError as e:
            raise ApiError(403, str(e) or "Доступ запрещён") from e
        except FileExistsError as e:
            raise ApiError(409, str(e)) from e
        except NotADirectoryError as e:
            raise ApiError(400, f"Это не папка: {e}") from e
        except ValueError as e:
            raise ApiError(400, str(e)) from e
        except OSError as e:
            raise ApiError(500, f"Ошибка файловой системы: {e}") from e

    @contextmanager
    def _database(self) -> Iterator[Database]:
        db = Database(self.db_path)
        try:
            yield db
        finally:
            try:
                db.close()
            except Exception:  # pragma: no cover - defensive
                pass

    def _state(self) -> Dict[str, Any]:
        return {
            "authenticated": True,
            "panel": self.panel_info,
            "telegram": self.telegram_status() if callable(self.telegram_status) else "unknown",
        }

    # -- overview -------------------------------------------------------

    def overview(self, request: Request) -> Response:
        metrics = monitor.get_all_metrics()
        alerts: List[Dict[str, Any]] = []
        launches: List[Dict[str, Any]] = []
        unread = 0
        try:
            with self._database() as db:
                for row in db.get_recent_alerts(limit=12):
                    ts, level, category, message, acknowledged = row
                    alerts.append(
                        {
                            "timestamp": ts,
                            "time": _fmt_time(ts),
                            "level": level,
                            "category": category,
                            "message": message,
                            "acknowledged": bool(acknowledged),
                        }
                    )
                    if not acknowledged:
                        unread += 1
                try:
                    unread = db.count_unacknowledged_alerts()
                except Exception:
                    pass
                for row in db.get_recent_app_launches(limit=8):
                    ts, name, success, error = row
                    launches.append(
                        {
                            "timestamp": ts,
                            "time": _fmt_time(ts),
                            "name": name,
                            "success": bool(success),
                            "error": error,
                        }
                    )
        except Exception as e:  # database must not break the dashboard
            logger.warning("Overview database read failed: %s", e)

        processes: List[Dict[str, Any]] = []
        try:
            snapshot = process_manager.get_process_snapshot()
            snapshot.sort(key=lambda item: item.get("memory_mb", 0), reverse=True)
            processes = [
                {
                    "pid": item.get("pid"),
                    "name": item.get("name"),
                    "cpu": round(float(item.get("cpu_percent") or 0), 1),
                    "memory_mb": round(float(item.get("memory_mb") or 0), 1),
                }
                for item in snapshot[:6]
            ]
        except Exception as e:
            logger.warning("Overview process snapshot failed: %s", e)

        scheduled = 0
        if self.scheduler is not None:
            try:
                scheduled = len(self.scheduler.list_tasks())
            except Exception as e:
                logger.warning("Scheduler read failed: %s", e)

        payload = dict(metrics or {})
        payload.update(
            {
                "ok": True,
                "alerts": alerts,
                "unread_alerts": unread,
                "launches": launches,
                "top_processes": processes,
                "scheduler_count": scheduled,
                "panel": self.panel_info,
                "telegram": self.telegram_status() if callable(self.telegram_status) else "unknown",
            }
        )
        return json_response(payload)

    # -- charts ---------------------------------------------------------

    def chart(self, request: Request) -> Response:
        metric = (request.q("metric") or "cpu").strip().lower()
        time_range = (request.q("range") or "5m").strip().lower()
        device = request.q("device")
        if metric not in CHART_METRICS:
            raise ApiError(400, "Неизвестный график")
        if time_range not in CHART_RANGES:
            raise ApiError(400, "Неизвестный диапазон")
        if metric == "disk" and not device:
            raise ApiError(400, "Не выбран диск")
        image = charts.render_chart(
            self.db_path, metric, time_range, device if metric == "disk" else None
        )
        if not image:
            return Response(204, b"", "image/png")
        return Response(200, image, "image/png", {"Cache-Control": "no-store"})

    # -- files ----------------------------------------------------------

    def files_list(self, request: Request) -> Response:
        raw = request.q("path")
        if not raw:
            drives = file_manager.list_drives()
            return json_response(
                {
                    "kind": "drives",
                    "drives": drives,
                    "hostname": os.environ.get("COMPUTERNAME", ""),
                }
            )

        data = file_manager.list_directory(Path(raw))
        entries = []
        for entry in data["entries"]:
            entries.append(
                {
                    "name": entry["name"],
                    "path": str(entry["path"]),
                    "is_dir": bool(entry["is_dir"]),
                    "size": int(entry["size"]),
                }
            )
        parent = data.get("parent")
        return json_response(
            {
                "kind": "dir",
                "path": str(data["path"]),
                "parent": str(parent) if parent else None,
                "entries": entries,
            }
        )

    def files_search(self, request: Request) -> Response:
        raw = request.q("path")
        query = (request.q("q") or "").strip()
        if not raw:
            raise ApiError(400, "Сначала выберите папку")
        if not query:
            raise ApiError(400, "Введите часть имени")
        result = file_manager.search_files(
            Path(raw), query, max_results=MAX_SEARCH_RESULTS
        )
        results = [
            {
                "name": item["name"],
                "path": str(item["path"]),
                "is_dir": bool(item["is_dir"]),
                "size": int(item["size"]),
            }
            for item in result.get("results", [])
        ]
        return json_response(
            {
                "kind": "search",
                "root": str(result.get("root", raw)),
                "query": query,
                "results": results,
                "truncated": bool(result.get("truncated")),
                "scanned_dirs": int(result.get("scanned_dirs", 0)),
            }
        )

    def files_download(self, request: Request) -> Response:
        raw = request.q("path")
        if not raw:
            raise ApiError(400, "Не указан файл")
        path = Path(raw)
        if not path.is_file():
            raise ApiError(404, "Файл не найден")
        stat = path.stat()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        disposition = f"attachment; filename*=UTF-8''{quote(path.name)}"
        return Response(
            status=200,
            body=b"",
            content_type=content_type,
            headers={
                "Content-Disposition": disposition,
                "Content-Length": str(stat.st_size),
            },
            file_path=path,
        )

    def files_action(self, request: Request) -> Response:
        data = request.json()
        action = str(data.get("action") or "").strip().lower()
        path = str(data.get("path") or "").strip()
        if not path:
            raise ApiError(400, "Не указан путь")

        if action == "mkdir":
            target = file_manager.make_directory(Path(path), str(data.get("name") or ""))
            message = f"Папка создана: {target.name}"
        elif action == "rename":
            target = file_manager.rename_path(Path(path), str(data.get("name") or ""))
            message = f"Переименовано в {target.name}"
        elif action == "copy":
            dest = str(data.get("dest") or "").strip()
            if not dest:
                raise ApiError(400, "Не указана папка назначения")
            target = file_manager.copy_path(Path(path), Path(dest))
            message = f"Скопировано в {target}"
        elif action == "move":
            dest = str(data.get("dest") or "").strip()
            if not dest:
                raise ApiError(400, "Не указана папка назначения")
            target = file_manager.move_path(Path(path), Path(dest))
            message = f"Перемещено в {target}"
        elif action == "delete":
            if not data.get("confirm"):
                raise ApiError(400, "Удаление требует подтверждения")
            stats = file_manager.delete_path(Path(path))
            message = (
                f"Удалено: {stats.get('deleted', 0)} объектов, "
                f"ошибок: {stats.get('errors', 0)}"
            )
        else:
            raise ApiError(400, "Неизвестное действие")

        return json_response({"ok": True, "message": message})

    def files_upload(self, request: Request) -> Response:
        fields, files = _parse_multipart(request.content_type, request.body)
        if not files:
            raise ApiError(400, "Не выбран файл")
        total = sum(len(payload) for _name, payload in files)
        if total > MAX_UPLOAD_BYTES:
            raise ApiError(413, "Файл слишком большой (максимум 200 МБ)")
        dest_raw = str(fields.get("path") or "").strip()
        if not dest_raw:
            raise ApiError(400, "Не указана папка назначения")
        dest = Path(dest_raw)
        if not dest.is_dir():
            raise ApiError(400, "Папка назначения не существует")
        saved = []
        for filename, payload in files:
            if not payload:
                continue
            target = file_manager.save_upload(filename, payload, dest)
            saved.append({"name": target.name, "path": str(target), "size": len(payload)})
        if not saved:
            raise ApiError(400, "Файл пустой")
        return json_response({"ok": True, "saved": saved})

    # -- processes ------------------------------------------------------

    def processes(self, request: Request) -> Response:
        sort = (request.q("sort") or "cpu").strip().lower()
        if sort not in {"cpu", "mem", "name"}:
            sort = "cpu"
        try:
            limit = int(request.q("limit") or 60)
        except ValueError:
            limit = 60
        limit = max(10, min(limit, 200))

        allowlist = process_manager.get_terminable_allowlist()
        entries: List[Dict[str, Any]] = []
        primed = []
        for proc in psutil.process_iter(["pid", "name", "username", "memory_info", "create_time"]):
            try:
                proc.cpu_percent(None)
                primed.append(proc)
            except (psutil.Error, OSError):
                continue
        time.sleep(0.25)
        for proc in primed:
            try:
                info = proc.info
                memory = info.get("memory_info")
                entries.append(
                    {
                        "pid": info.get("pid"),
                        "name": info.get("name") or "",
                        "user": (info.get("username") or "").split("\\")[-1],
                        "cpu": round(float(proc.cpu_percent(None)), 1),
                        "memory_mb": round(
                            float(getattr(memory, "rss", 0) or 0) / (1024 * 1024), 1
                        ),
                        "started": info.get("create_time") or 0,
                        "critical": process_manager.is_process_critical(
                            info.get("name") or ""
                        ),
                        "terminable": process_manager.is_process_terminable(
                            info.get("name") or "", allowlist
                        ),
                    }
                )
            except (psutil.Error, OSError):
                continue

        if sort == "mem":
            entries.sort(key=lambda item: item["memory_mb"], reverse=True)
        elif sort == "name":
            entries.sort(key=lambda item: item["name"].casefold())
        else:
            entries.sort(key=lambda item: item["cpu"], reverse=True)
        return json_response({"ok": True, "processes": entries[:limit], "sort": sort})

    def processes_kill(self, request: Request) -> Response:
        data = request.json()
        try:
            pid = int(data.get("pid"))
        except (TypeError, ValueError) as e:
            raise ApiError(400, "Некорректный PID") from e
        if not data.get("confirm"):
            raise ApiError(400, "Завершение требует подтверждения")
        with self._database() as db:
            ok, message = process_manager.safe_terminate_process(
                pid, reason="Web panel request", db=db
            )
        return json_response({"ok": ok, "message": message}, 200 if ok else 400)

    # -- applications ---------------------------------------------------

    def apps(self, request: Request) -> Response:
        config = application_manager.load_applications_config()
        items = []
        for name, path in config.items():
            try:
                executable = Path(path).name
                exists = Path(path).exists()
            except OSError:
                executable = ""
                exists = False
            items.append({"name": name, "file": executable, "exists": exists})
        items.sort(key=lambda item: item["name"].casefold())
        return json_response({"ok": True, "apps": items})

    def apps_launch(self, request: Request) -> Response:
        data = request.json()
        name = str(data.get("name") or "").strip()
        if not name:
            raise ApiError(400, "Не указано приложение")
        with self._database() as db:
            ok, message = application_manager.launch_application(name, db=db)
        return json_response({"ok": ok, "message": message}, 200 if ok else 400)

    # -- gaming profiles ------------------------------------------------

    def profiles(self, request: Request) -> Response:
        profiles = gaming_profiles.load_profiles_config()
        items = []
        for key, spec in profiles.items():
            items.append(
                {
                    "key": key,
                    "description": spec.get("description", ""),
                    "apps": list(spec.get("apps") or []),
                    "optional": list(spec.get("optional") or []),
                    "read_only": key.strip().upper() == "NORMAL",
                }
            )
        return json_response({"ok": True, "profiles": items})

    def profiles_launch(self, request: Request) -> Response:
        data = request.json()
        key = str(data.get("key") or "").strip()
        if not key:
            raise ApiError(400, "Не указан профиль")
        with self._database() as db:
            started, failed = gaming_profiles.launch_profile(key, db=db)
        return json_response(
            {
                "ok": not failed,
                "started": started,
                "failed": failed,
            },
            200 if not failed else 400,
        )

    # -- media / power / clipboard --------------------------------------

    def media(self, request: Request) -> Response:
        keys = [
            {"key": key, "label": label}
            for key, (label, _vk) in media_control.MEDIA_ACTIONS.items()
        ]
        return json_response(
            {"ok": True, "keys": keys, "available": os.name == "nt"}
        )

    def media_key(self, request: Request) -> Response:
        data = request.json()
        key = str(data.get("key") or "").strip()
        if key not in MEDIA_KEYS:
            raise ApiError(400, "Неизвестная мультимедиа-команда")
        ok, message = media_control.press_key(key)
        return json_response({"ok": ok, "message": message}, 200 if ok else 400)

    def power(self, request: Request) -> Response:
        status = power_manager.get_power_status()
        actions = [
            {"key": "lock", "label": "Заблокировать", "danger": False},
            {"key": "sleep", "label": "Спящий режим", "danger": False},
            {"key": "hibernate", "label": "Гибернация", "danger": False},
            {"key": "display_off", "label": "Выключить экран", "danger": False},
            {"key": "cancel", "label": "Отменить таймер", "danger": False},
            {"key": "shutdown", "label": "Выключить ПК", "danger": True},
            {"key": "restart", "label": "Перезагрузить", "danger": True},
        ]
        return json_response({"ok": True, "status": status, "actions": actions})

    def power_action(self, request: Request) -> Response:
        data = request.json()
        action = str(data.get("action") or "").strip().lower()
        if action not in POWER_ACTIONS:
            raise ApiError(400, "Неизвестное действие питания")

        if action == "shutdown":
            if not data.get("confirm"):
                raise ApiError(400, "Выключение требует подтверждения")
            ok, message = power_manager.execute_shutdown(confirmed=True)
        elif action == "restart":
            if not data.get("confirm"):
                raise ApiError(400, "Перезагрузка требует подтверждения")
            ok, message = power_manager.execute_restart(confirmed=True)
        elif action == "cancel":
            ok, message = power_manager.cancel_shutdown()
        elif action == "lock":
            ok, message = power_manager.lock_workstation()
        elif action == "sleep":
            ok, message = power_manager.sleep_system()
        elif action == "hibernate":
            ok, message = power_manager.hibernate_system()
        else:
            ok, message = power_manager.display_off()
        return json_response({"ok": ok, "message": message}, 200 if ok else 400)

    def clipboard_get(self, request: Request) -> Response:
        ok, text, truncated = clipboard_manager.read_clipboard_text()
        return json_response(
            {
                "ok": ok,
                "text": text,
                "truncated": truncated,
                "available": clipboard_manager.clipboard_available(),
            }
        )

    def clipboard_set(self, request: Request) -> Response:
        data = request.json()
        text = str(data.get("text") or "")
        if len(text) > 100_000:
            raise ApiError(413, "Текст слишком длинный")
        ok, message = clipboard_manager.write_clipboard_text(text)
        return json_response({"ok": ok, "message": message}, 200 if ok else 400)

    def screenshot(self, request: Request) -> Response:
        image, error = screenshot_manager.take_screenshot()
        if not image:
            raise ApiError(503, error or "Не удалось сделать снимок экрана")
        return Response(
            200,
            image,
            "image/png",
            {"Cache-Control": "no-store, max-age=0"},
        )

    # -- network --------------------------------------------------------

    def network(self, request: Request) -> Response:
        status = network_info.collect_network_status(include_public_ip=True)
        status["ok"] = True
        return json_response(status)

    def network_ping(self, request: Request) -> Response:
        host = _clean_host(request.json().get("host"), "1.1.1.1")
        return json_response({"ok": True, "result": network_info.ping_host(host)})

    def network_dns(self, request: Request) -> Response:
        host = _clean_host(request.json().get("host"), "example.com")
        return json_response({"ok": True, "result": network_info.dns_lookup(host)})

    def network_traceroute(self, request: Request) -> Response:
        host = _clean_host(request.json().get("host"), "1.1.1.1")
        hops = network_info.traceroute(host, max_hops=8)
        return json_response({"ok": True, "host": host, "hops": hops})

    def network_diagnostics(self, request: Request) -> Response:
        return json_response({"ok": True, "result": network_info.collect_lan_diagnostics()})

    def network_speedtest(self, request: Request) -> Response:
        if not self._speedtest_lock.acquire(blocking=False):
            raise ApiError(409, "Тест скорости уже выполняется")
        try:
            result = network_info.run_speedtest()
        finally:
            self._speedtest_lock.release()
        return json_response({"ok": True, "result": result})

    # -- scheduler ------------------------------------------------------

    def scheduler_list(self, request: Request) -> Response:
        if self.scheduler is None:
            return json_response({"ok": False, "tasks": [], "message": "Планировщик недоступен"})
        tasks = []
        for task in self.scheduler.list_tasks():
            run_at = float(task.get("run_at") or 0)
            tasks.append(
                {
                    "id": task.get("id"),
                    "action": task.get("action"),
                    "label": task.get("label"),
                    "run_at": run_at,
                    "run_at_text": _fmt_time(run_at),
                    "remaining": max(0, int(run_at - time.time())),
                    "params": task.get("params") or {},
                }
            )
        return json_response(
            {"ok": True, "tasks": tasks, "actions": sorted(SCHEDULE_ACTIONS)}
        )

    def scheduler_add(self, request: Request) -> Response:
        if self.scheduler is None:
            raise ApiError(503, "Планировщик недоступен")
        data = request.json()
        action = str(data.get("action") or "").strip().lower()
        if action not in SCHEDULE_ACTIONS:
            raise ApiError(400, "Неизвестное действие планировщика")
        params: Dict[str, Any] = {}
        if action == "profile":
            profile = str(data.get("profile") or "").strip()
            if not profile:
                raise ApiError(400, "Не указан профиль")
            params["profile"] = profile
        label = str(data.get("label") or "").strip() or None

        run_at_raw = data.get("run_at")
        delay_raw = data.get("delay_seconds")
        run_at = None
        delay = None
        if run_at_raw not in (None, ""):
            try:
                run_at = float(run_at_raw)
            except (TypeError, ValueError) as e:
                raise ApiError(400, "Некорректное время") from e
        elif delay_raw not in (None, ""):
            try:
                delay = float(delay_raw)
            except (TypeError, ValueError) as e:
                raise ApiError(400, "Некорректная задержка") from e
        else:
            raise ApiError(400, "Укажите время или задержку")

        try:
            task = self.scheduler.add(
                action, delay_seconds=delay, run_at=run_at, label=label, params=params
            )
        except ValueError as e:
            raise ApiError(400, str(e)) from e
        return json_response({"ok": True, "task": task})

    def scheduler_cancel(self, request: Request) -> Response:
        if self.scheduler is None:
            raise ApiError(503, "Планировщик недоступен")
        data = request.json()
        try:
            task_id = int(data.get("id"))
        except (TypeError, ValueError) as e:
            raise ApiError(400, "Некорректный номер задачи") from e
        if not self.scheduler.cancel(task_id):
            raise ApiError(404, "Задача не найдена")
        return json_response({"ok": True, "message": "Задача отменена"})

    # -- settings -------------------------------------------------------

    def settings_get(self, request: Request) -> Response:
        try:
            sections = settings_store.all_settings(self.db_path)
            missing = [key for key in settings_store.SECTIONS if key not in sections]
            if missing:
                defaults = settings_store.export_payload(self.db_path)["settings"]
                for key in missing:
                    sections[key] = defaults.get(key)
        except Exception as e:
            raise ApiError(500, f"Не удалось прочитать настройки: {e}") from e
        payload = {
            "ok": True,
            "sections": sections,
            "available": list(settings_store.SECTIONS),
        }
        return json_response(payload)

    def settings_save(self, request: Request) -> Response:
        data = request.json()
        sections = data.get("sections")
        if not isinstance(sections, dict) or not sections:
            raise ApiError(400, "Нет настроек для сохранения")
        try:
            cleaned = settings_store.import_payload({"settings": sections}, self.db_path)
        except ValueError as e:
            raise ApiError(400, str(e)) from e
        return json_response({"ok": True, "sections": cleaned})

    def settings_export(self, request: Request) -> Response:
        return json_response(settings_store.export_payload(self.db_path))

    def settings_import(self, request: Request) -> Response:
        data = request.json()
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
        try:
            cleaned = settings_store.import_payload(payload, self.db_path)
        except ValueError as e:
            raise ApiError(400, str(e)) from e
        return json_response({"ok": True, "sections": cleaned})

    def settings_reset(self, request: Request) -> Response:
        created = settings_store.reset_defaults(self.db_path)
        return json_response({"ok": True, "created": created})

    # -- alerts ---------------------------------------------------------

    def alerts_ack(self, request: Request) -> Response:
        data = request.json()
        timestamp = data.get("timestamp")
        with self._database() as db:
            if timestamp in (None, ""):
                updated = db.acknowledge_alerts()
            else:
                try:
                    updated = db.acknowledge_alerts(float(timestamp))
                except (TypeError, ValueError) as e:
                    raise ApiError(400, "Некорректная метка времени") from e
        return json_response({"ok": True, "updated": updated})


SCHEDULE_ACTIONS = {"shutdown", "restart", "sleep", "lock", "profile"}
HOST_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,252})$")


def _clean_host(value: Any, default: str) -> str:
    """Basic validation for user-supplied hostnames and addresses."""
    host = str(value if value is not None else "").strip() or default
    if not HOST_PATTERN.match(host):
        raise ApiError(400, "Некорректный адрес")
    return host


# -- multipart ----------------------------------------------------------


def _parse_multipart(
    content_type: str, body: bytes
) -> Tuple[Dict[str, str], List[Tuple[str, bytes]]]:
    """Parse ``multipart/form-data`` with the standard library only."""
    if "multipart/form-data" not in (content_type or "").lower():
        raise ApiError(400, "Ожидался multipart/form-data")
    if not body:
        raise ApiError(400, "Пустой запрос")

    from email.parser import BytesParser
    from email.policy import default as default_policy

    header = (
        b"Content-Type: "
        + (content_type or "").encode("latin-1", "replace")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
    )
    message = BytesParser(policy=default_policy).parsebytes(header + body)
    if not message.is_multipart():
        raise ApiError(400, "Некорректная форма загрузки")

    fields: Dict[str, str] = {}
    files: List[Tuple[str, bytes]] = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is not None:
            files.append((filename, payload))
        elif name:
            try:
                fields[str(name)] = payload.decode("utf-8")
            except UnicodeDecodeError:
                fields[str(name)] = payload.decode("latin-1", "replace")
    return fields, files