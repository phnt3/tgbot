"""Periodic background monitoring for PC Remote.

The background task is the only component that writes metric history on a
schedule and pushes alerts to Telegram. It runs inside the bot's event loop:

- metric sampling (psutil/NVML) is blocking, so it runs in a worker thread;
- SQLite access stays on the event-loop thread because the shared
  ``Database`` connection is not thread-safe;
- alert evaluation is stateful (see ``alerts.py``) and only returns alerts
  that should actually be sent.
"""

import asyncio
import logging
import time
from typing import Any, Dict, Iterable, List, Optional

from aiogram import Bot

from alerts import AlertManager, load_alert_settings
from database import Database
from monitor import get_all_metrics

logger = logging.getLogger(__name__)

# Trim history roughly once an hour at the default 60 s interval.
PRUNE_EVERY_CYCLES = 60
DEFAULT_HISTORY_ROWS = 10000


def _number(value: Any) -> Optional[float]:
    """Convert a monitor value to float, mapping unavailable values to None."""
    if value is None or value == "N/A":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class BackgroundMonitor:
    """Samples metrics periodically, records history and pushes alerts."""

    def __init__(
        self,
        bot: Bot,
        db: Database,
        alert_manager: Optional[AlertManager] = None,
        user_ids: Optional[Iterable[int]] = None,
        interval_seconds: Optional[int] = None,
        history_rows: int = DEFAULT_HISTORY_ROWS,
    ) -> None:
        """Initialize the background monitor.

        Args:
            bot: Aiogram bot used to deliver alerts.
            db: Shared database instance (event-loop thread only).
            alert_manager: Optional pre-configured alert manager.
            user_ids: Telegram user IDs that receive alerts.
            interval_seconds: Sampling interval; defaults to alerts.json.
            history_rows: Maximum rows kept per history table.
        """
        settings = load_alert_settings()

        self.bot = bot
        self.db = db
        self.alert_manager = alert_manager or AlertManager(
            db,
            thresholds=settings["thresholds"],
            cooldowns=settings["cooldowns"],
        )
        self.user_ids = [int(uid) for uid in (user_ids or [])]
        self.interval_seconds = int(
            interval_seconds
            if interval_seconds is not None
            else settings["interval_seconds"]
        )
        if self.interval_seconds < 1:
            self.interval_seconds = 1
        self.history_rows = int(history_rows)
        self._task: Optional[asyncio.Task] = None
        self._cycles = 0

    # -- lifecycle ------------------------------------------------------

    def start(self) -> asyncio.Task:
        """Start the periodic task (idempotent).

        Returns:
            The running asyncio task.
        """
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(
            self.run(), name="pc-remote-background-monitor"
        )
        return self._task

    async def stop(self) -> None:
        """Cancel the periodic task and wait for it to finish."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("Background monitor stopped")

    async def run(self) -> None:
        """Run sample/record/alert cycles until cancelled."""
        logger.info(
            "Background monitor started (interval=%ss, users=%d)",
            self.interval_seconds,
            len(self.user_ids),
        )
        try:
            while True:
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Background monitor cycle failed")
                await asyncio.sleep(self.interval_seconds)
        except asyncio.CancelledError:
            logger.info("Background monitor task cancelled")
            raise

    # -- one cycle ------------------------------------------------------

    async def run_once(self) -> List[Dict[str, Any]]:
        """Collect metrics once, record history and send new alerts.

        Returns:
            Alerts created during this cycle (already persisted/sent).
        """
        metrics = await asyncio.to_thread(get_all_metrics)
        self.record_metrics(metrics)
        alerts = self.alert_manager.evaluate_metrics(metrics)

        for alert in alerts:
            await self.send_alert(alert)

        self._cycles += 1
        if self._cycles % PRUNE_EVERY_CYCLES == 0:
            await self._prune_history()

        return alerts

    def record_metrics(self, metrics: Dict[str, Any]) -> None:
        """Persist one metrics snapshot to the history tables.

        Args:
            metrics: Dict produced by ``monitor.get_all_metrics()``.
        """
        if not isinstance(metrics, dict):
            return

        now = time.time()
        cpu = metrics.get("cpu") or {}
        gpu = metrics.get("gpu") or {}
        ram = metrics.get("ram") or {}
        disks = metrics.get("disk") or {}

        self._safe_insert(
            "cpu",
            lambda: self.db.insert_cpu_history(
                _number(cpu.get("utilization")),
                _number(cpu.get("temperature")),
                _number(cpu.get("frequency")),
                timestamp=now,
            ),
        )
        self._safe_insert(
            "gpu",
            lambda: self.db.insert_gpu_history(
                _number(gpu.get("utilization")),
                _number(gpu.get("temperature")),
                _number(gpu.get("frequency")),
                _number(gpu.get("memory_utilization")),
                _number(gpu.get("vram_used_mb")),
                _number(gpu.get("vram_total_mb")),
                _number(gpu.get("power_watts")),
                timestamp=now,
            ),
        )
        self._safe_insert(
            "ram",
            lambda: self.db.insert_ram_history(
                _number(ram.get("total_mb")),
                _number(ram.get("used_mb")),
                _number(ram.get("available_mb")),
                _number(ram.get("utilization_percent")),
                timestamp=now,
            ),
        )

        if isinstance(disks, dict):
            for drive, info in disks.items():
                if not isinstance(info, dict):
                    continue
                self._safe_insert(
                    f"disk {drive}",
                    lambda drive=drive, info=info: self.db.insert_disk_history(
                        str(drive),
                        _number(info.get("total_mb")),
                        _number(info.get("available_mb")),
                        _number(info.get("utilization_percent")),
                        _number(info.get("temperature")),
                        timestamp=now,
                    ),
                )

    async def send_alert(self, alert: Dict[str, Any]) -> None:
        """Deliver one alert to every configured user.

        Args:
            alert: Alert dict returned by the alert manager.
        """
        text = self.format_alert(alert)
        for user_id in self.user_ids:
            try:
                await self.bot.send_message(user_id, text)
            except Exception:
                logger.exception("Failed to send alert to user %s", user_id)

    @staticmethod
    def format_alert(alert: Dict[str, Any]) -> str:
        """Render an alert as plain text (no Telegram entities)."""
        level = str(alert.get("level", "info")).upper()
        category = str(alert.get("category", "system"))
        message = str(alert.get("message", "System condition changed"))
        return f"[{level}] {category}\n{message}"

    # -- internals ------------------------------------------------------

    def _safe_insert(self, label: str, insert) -> None:
        """Run one history insert, logging instead of aborting the cycle."""
        try:
            insert()
        except Exception as e:
            logger.error("Failed to record %s history: %s", label, e)

    async def _prune_history(self) -> None:
        """Trim old history rows without blocking the event loop for long."""
        try:
            deleted = self.db.prune_history(max_rows=self.history_rows)
            if deleted:
                logger.info("Pruned %d old history rows", deleted)
        except Exception as e:
            logger.error("Failed to prune history: %s", e)