"""Persistent timed-action scheduler for PC Remote.

Tasks are stored as JSON so scheduled shutdowns survive a bot restart. Each
task is a single whitelisted action (shutdown, restart, sleep, lock, profile)
with an absolute ``run_at`` timestamp. The scheduler runs its own asyncio loop
and executes due tasks in worker threads so blocking Win32 calls never stall
Telegram polling.

Actions are only ever taken from ``ALLOWED_ACTIONS``; nothing received from
Telegram is executed as a shell command.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ALLOWED_ACTIONS = {"shutdown", "restart", "sleep", "lock", "profile"}
DEFAULT_TICK_SECONDS = 5.0
# A task missed while the bot was offline is still executed when it is at most
# this old; older ones are dropped to avoid a surprising shutdown on startup.
DEFAULT_OVERDUE_GRACE_SECONDS = 120.0
ACTION_LABELS = {
    "shutdown": "Shutdown",
    "restart": "Restart",
    "sleep": "Sleep",
    "lock": "Lock workstation",
    "profile": "Launch profile",
}


def format_task_time(timestamp: float) -> str:
    """Format a Unix timestamp in local time for UI and logs."""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(timestamp)))
    except (TypeError, ValueError, OSError):
        return "unknown time"


def describe_task(task: Dict[str, Any]) -> str:
    """Return a one-line human-readable description of a task."""
    action = str(task.get("action", "unknown"))
    label = ACTION_LABELS.get(action, action)
    params = task.get("params") or {}
    if action == "profile" and params.get("profile"):
        label = f"Launch profile {params['profile']}"
    return f"{label} at {format_task_time(task.get('run_at', 0))}"


class TaskScheduler:
    """Manage, persist and execute delayed power/profile actions."""

    def __init__(
        self,
        storage_path: Path,
        bot: Any = None,
        user_ids: Optional[List[int]] = None,
        db: Any = None,
        executor: Optional[Callable[[Dict[str, Any]], Tuple[bool, str]]] = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        overdue_grace_seconds: float = DEFAULT_OVERDUE_GRACE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.storage_path = Path(storage_path)
        self.bot = bot
        self.user_ids = [int(uid) for uid in (user_ids or [])]
        self.db = db
        self.tick_seconds = max(0.5, float(tick_seconds))
        self.overdue_grace_seconds = float(overdue_grace_seconds)
        self._clock = clock
        self._executor = executor
        self._tasks: List[Dict[str, Any]] = []
        self._next_id = 1
        self._loop_task: Optional[asyncio.Task] = None
        self._load()

    # -- persistence ----------------------------------------------------

    def _load(self) -> None:
        """Load tasks from disk, dropping malformed and stale entries."""
        self._tasks = []
        if not self.storage_path.exists():
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Failed to load scheduled tasks: %s", e)
            return

        if not isinstance(raw, list):
            logger.warning("Ignoring malformed scheduled task file")
            return

        now = self._clock()
        dropped = 0
        seen_ids = set()
        for item in raw:
            task = self._normalize(item)
            if task is None:
                dropped += 1
                continue
            if task["id"] in seen_ids:
                dropped += 1
                continue
            if task["run_at"] < now - self.overdue_grace_seconds:
                dropped += 1
                continue
            seen_ids.add(task["id"])
            self._tasks.append(task)

        self._next_id = max([task["id"] for task in self._tasks], default=0) + 1
        if dropped:
            logger.info("Dropped %d stale or malformed scheduled task(s)", dropped)
            self._save()

    def _normalize(self, item: Any) -> Optional[Dict[str, Any]]:
        """Validate a stored task and return a clean copy, or None."""
        if not isinstance(item, dict):
            return None
        try:
            task_id = int(item["id"])
            action = str(item["action"])
            run_at = float(item["run_at"])
            created_at = float(item.get("created_at", run_at))
        except (KeyError, TypeError, ValueError):
            return None
        if task_id <= 0 or action not in ALLOWED_ACTIONS:
            return None
        params = item.get("params")
        if not isinstance(params, dict):
            params = {}
        return {
            "id": task_id,
            "action": action,
            "label": str(item.get("label") or ACTION_LABELS.get(action, action)),
            "run_at": run_at,
            "created_at": created_at,
            "params": params,
        }

    def _save(self) -> None:
        """Atomically write the task list to disk."""
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.storage_path.with_name(
                self.storage_path.name + ".tmp"
            )
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(self._tasks, handle, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.storage_path)
        except OSError as e:
            logger.error("Failed to save scheduled tasks: %s", e)

    # -- public API -----------------------------------------------------

    def add(
        self,
        action: str,
        delay_seconds: Optional[float] = None,
        run_at: Optional[float] = None,
        label: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create and persist a new task.

        Args:
            action: One of ``ALLOWED_ACTIONS``.
            delay_seconds: Delay from now; mutually exclusive with ``run_at``.
            run_at: Absolute Unix timestamp.
            label: Optional UI label.
            params: Extra parameters (only ``profile`` uses ``profile``).

        Raises:
            ValueError: On an unknown action or invalid schedule time.
        """
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"Unknown scheduled action: {action}")
        if (delay_seconds is None) == (run_at is None):
            raise ValueError("Provide exactly one of delay_seconds or run_at")

        if run_at is None:
            delay = float(delay_seconds)  # type: ignore[arg-type]
            if delay <= 0:
                raise ValueError("Delay must be positive")
            run_at = self._clock() + delay

        run_at = float(run_at)
        if run_at <= self._clock():
            raise ValueError("Scheduled time must be in the future")

        clean_params = dict(params or {})
        if action == "profile" and not str(clean_params.get("profile", "")).strip():
            raise ValueError("Profile action requires a profile name")

        task = {
            "id": self._next_id,
            "action": action,
            "label": str(label or ACTION_LABELS.get(action, action)),
            "run_at": run_at,
            "created_at": self._clock(),
            "params": clean_params,
        }
        self._next_id += 1
        self._tasks.append(task)
        self._tasks.sort(key=lambda item: item["run_at"])
        self._save()
        return dict(task)

    def add_delay(
        self,
        action: str,
        delay_seconds: float,
        label: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Convenience wrapper for ``add(..., delay_seconds=...)``."""
        return self.add(
            action,
            delay_seconds=delay_seconds,
            label=label,
            params=params,
        )

    def list_tasks(self) -> List[Dict[str, Any]]:
        """Return a copy of all pending tasks, soonest first."""
        return [dict(task) for task in self._tasks]

    def cancel(self, task_id: int) -> bool:
        """Cancel one task by id. Returns True when something was removed."""
        before = len(self._tasks)
        self._tasks = [task for task in self._tasks if task["id"] != int(task_id)]
        if len(self._tasks) == before:
            return False
        self._save()
        return True

    def cancel_all(self) -> int:
        """Cancel every pending task and return how many were removed."""
        removed = len(self._tasks)
        if removed:
            self._tasks = []
            self._save()
        return removed

    # -- execution ------------------------------------------------------

    async def run_due(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Execute all tasks whose time has come.

        Due tasks are removed and persisted before execution, so a crash in an
        action can never cause a shutdown loop.

        Returns:
            List of result dicts: task, ok, message.
        """
        current = self._clock() if now is None else float(now)
        due = [task for task in self._tasks if task["run_at"] <= current]
        if not due:
            return []

        due_ids = {task["id"] for task in due}
        self._tasks = [task for task in self._tasks if task["id"] not in due_ids]
        self._save()

        results: List[Dict[str, Any]] = []
        for task in due:
            logger.info("Executing scheduled task %s: %s", task["id"], describe_task(task))
            try:
                if self._executor is not None:
                    ok, message = await asyncio.to_thread(self._executor, task)
                else:
                    ok, message = await asyncio.to_thread(self._execute, task)
            except Exception as e:
                logger.exception("Scheduled task %s failed", task["id"])
                ok, message = False, f"Scheduled task failed: {e}"

            results.append({"task": task, "ok": bool(ok), "message": message})
            await self._notify(task, bool(ok), message)
        return results

    def _execute(self, task: Dict[str, Any]) -> Tuple[bool, str]:
        """Run one task's action (blocking; called in a worker thread)."""
        action = task["action"]
        try:
            if action == "shutdown":
                from power_manager import execute_shutdown

                return execute_shutdown(confirmed=True)
            if action == "restart":
                from power_manager import execute_restart

                return execute_restart(confirmed=True)
            if action == "sleep":
                from power_manager import sleep_system

                return sleep_system()
            if action == "lock":
                from power_manager import lock_workstation

                return lock_workstation()
            if action == "profile":
                from gaming_profiles import launch_profile

                profile = str(task.get("params", {}).get("profile", ""))
                started, failed = launch_profile(profile, self.db)
                ok = bool(started)
                message = (
                    f"Profile {profile}: launched {len(started)} application(s)"
                    + (f", failed {len(failed)}" if failed else "")
                    + "."
                )
                return ok, message
        except Exception as e:
            logger.exception("Action %s failed", action)
            return False, f"Action {action} failed: {e}"
        return False, f"Unsupported action: {action}"

    async def _notify(self, task: Dict[str, Any], ok: bool, message: str) -> None:
        """Tell configured users about a finished task."""
        if self.bot is None or not self.user_ids:
            return
        prefix = "DONE" if ok else "FAILED"
        text = (
            f"[SCHEDULER] {prefix}\n"
            f"{describe_task(task)}\n"
            f"{message}"
        )
        for user_id in self.user_ids:
            try:
                await self.bot.send_message(user_id, text)
            except Exception:
                logger.exception("Failed to send scheduler notice to %s", user_id)

    # -- lifecycle ------------------------------------------------------

    def start(self) -> asyncio.Task:
        """Start the scheduler loop (idempotent)."""
        if self._loop_task is not None and not self._loop_task.done():
            return self._loop_task
        self._loop_task = asyncio.create_task(self._run(), name="task-scheduler")
        logger.info(
            "Task scheduler started (interval=%.1fs, tasks=%d)",
            self.tick_seconds,
            len(self._tasks),
        )
        return self._loop_task

    async def _run(self) -> None:
        while True:
            try:
                await self.run_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler cycle failed")
            await asyncio.sleep(self.tick_seconds)

    async def stop(self) -> None:
        """Stop the scheduler loop if running."""
        task = self._loop_task
        self._loop_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Scheduler loop crashed")
        logger.info("Task scheduler stopped")