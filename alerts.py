"""Alert evaluation for PC Remote.

The manager is stateful: a threshold crossing produces one alert, further
cycles above the threshold only repeat after the configured cooldown, and a
recovery alert is emitted only when an actually-active breach clears.

Alert evaluation is pure with respect to the machine: it either consumes
metrics passed in by the caller (``evaluate_metrics``) or samples them once
via ``check_once``. Every alert is persisted in SQLite and returned so the
background task can push it to Telegram.

Settings can be overridden with ``config/alerts.json``:

    {
      "interval_seconds": 60,
      "thresholds": {"cpu_temperature": 80, "gpu_temperature": 80,
                     "ram_utilization": 85, "disk_utilization": 85},
      "cooldowns":  {"cpu_temperature": 300, "gpu_temperature": 300,
                     "ram_utilization": 600, "disk_utilization": 600}
    }
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from database import Database
from monitor import get_all_metrics

logger = logging.getLogger(__name__)


# Default alert thresholds
DEFAULT_THRESHOLDS: Dict[str, float] = {
    "cpu_temperature": 80,      # °C - alert when CPU temp exceeds this
    "gpu_temperature": 80,      # °C - alert when GPU temp exceeds this
    "ram_utilization": 85,      # % - alert when RAM usage exceeds this
    "disk_utilization": 85,     # % - alert when disk usage exceeds this
}

# Cooldown periods in seconds (repeat alerts while a breach stays active)
DEFAULT_COOLDOWNS: Dict[str, float] = {
    "cpu_temperature": 300,     # 5 minutes
    "gpu_temperature": 300,     # 5 minutes
    "ram_utilization": 600,     # 10 minutes
    "disk_utilization": 600,    # 10 minutes
}

DEFAULT_INTERVAL_SECONDS = 60
MIN_INTERVAL_SECONDS = 15
MAX_INTERVAL_SECONDS = 86400

ALERT_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "alerts.json"


def _positive_float(value: Any) -> Optional[float]:
    """Return ``value`` as a positive float, or None when invalid."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def load_alert_settings(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load alert settings, merging an optional JSON file over the defaults.

    Unknown keys and invalid values are ignored instead of crashing the bot,
    so a mistyped config can never disable monitoring.

    Args:
        path: Optional path to a JSON settings file.

    Returns:
        Dict with "thresholds", "cooldowns" and "interval_seconds".
    """
    settings: Dict[str, Any] = {
        "thresholds": dict(DEFAULT_THRESHOLDS),
        "cooldowns": dict(DEFAULT_COOLDOWNS),
        "interval_seconds": DEFAULT_INTERVAL_SECONDS,
    }

    config_path = Path(path) if path is not None else ALERT_CONFIG_PATH
    if not config_path.is_file():
        return settings

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("Ignoring invalid alert config %s: %s", config_path, e)
        return settings

    if not isinstance(raw, dict):
        logger.warning("Ignoring alert config %s: top level must be an object", config_path)
        return settings

    for section in ("thresholds", "cooldowns"):
        values = raw.get(section)
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if key not in settings[section]:
                continue
            number = _positive_float(value)
            if number is None:
                logger.warning("Ignoring invalid %s value for %s: %r", section, key, value)
                continue
            settings[section][key] = number

    interval = _positive_float(raw.get("interval_seconds"))
    if interval is not None:
        settings["interval_seconds"] = int(
            min(MAX_INTERVAL_SECONDS, max(MIN_INTERVAL_SECONDS, interval))
        )

    return settings


class AlertManager:
    """Stateful threshold monitor with cooldowns and recovery notifications."""

    def __init__(
        self,
        db: Database,
        thresholds: Optional[Dict[str, float]] = None,
        cooldowns: Optional[Dict[str, float]] = None,
    ) -> None:
        """Initialize the alert manager.

        Args:
            db: Database instance for persisting alerts.
            thresholds: Optional custom thresholds overriding defaults.
            cooldowns: Optional custom cooldowns overriding defaults.
        """
        self.db = db
        self.thresholds = dict(DEFAULT_THRESHOLDS)
        if thresholds:
            self.thresholds.update(
                {k: v for k, v in thresholds.items() if k in DEFAULT_THRESHOLDS}
            )
        self.cooldowns = dict(DEFAULT_COOLDOWNS)
        if cooldowns:
            self.cooldowns.update(
                {k: v for k, v in cooldowns.items() if k in DEFAULT_COOLDOWNS}
            )

        # Keys currently in a breached state, e.g. "cpu_temperature" or
        # "disk_utilization:C:".
        self._active: Set[str] = set()
        # Last time an alert was emitted per key (cooldown bookkeeping).
        self._last_alert: Dict[str, float] = {}

    # -- public API -----------------------------------------------------

    def check_once(self) -> List[Dict[str, Any]]:
        """Sample the machine once and evaluate all thresholds.

        Returns:
            Newly created alerts (each also persisted to the database).
        """
        try:
            metrics = get_all_metrics()
        except Exception as e:
            logger.exception("Failed to collect metrics for alert check")
            return self._agent_error(str(e))
        return self.evaluate_metrics(metrics)

    def evaluate_metrics(self, metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Evaluate already-collected metrics.

        Args:
            metrics: Dict produced by ``monitor.get_all_metrics()``.

        Returns:
            Newly created alerts (each also persisted to the database).
        """
        if not isinstance(metrics, dict):
            return self._agent_error("invalid metrics payload")

        now = time.time()
        alerts: List[Dict[str, Any]] = []

        # Metrics are available again: clear a previous agent-failure alert.
        if "agent_unreachable" in self._active:
            self._active.discard("agent_unreachable")
            alerts.append(
                self._record(
                    now,
                    level="info",
                    category="recovery",
                    message="Monitoring agent is reachable again",
                )
            )

        cpu = metrics.get("cpu") or {}
        gpu = metrics.get("gpu") or {}
        ram = metrics.get("ram") or {}
        disks = metrics.get("disk") or {}

        alert = self._evaluate_metric(
            key="cpu_temperature",
            category="cpu",
            label="CPU temperature",
            value=cpu.get("temperature"),
            threshold=self.thresholds["cpu_temperature"],
            cooldown=self.cooldowns["cpu_temperature"],
            now=now,
            value_format="{:.1f} °C",
            threshold_format="{} °C",
            critical_margin=10,
        )
        if alert:
            alerts.append(alert)

        alert = self._evaluate_metric(
            key="gpu_temperature",
            category="gpu",
            label="GPU temperature",
            value=gpu.get("temperature"),
            threshold=self.thresholds["gpu_temperature"],
            cooldown=self.cooldowns["gpu_temperature"],
            now=now,
            value_format="{:.1f} °C",
            threshold_format="{} °C",
            critical_margin=10,
        )
        if alert:
            alerts.append(alert)

        alert = self._evaluate_metric(
            key="ram_utilization",
            category="ram",
            label="RAM utilization",
            value=ram.get("utilization_percent"),
            threshold=self.thresholds["ram_utilization"],
            cooldown=self.cooldowns["ram_utilization"],
            now=now,
            value_format="{:.1f}%",
            threshold_format="{}%",
            critical_margin=None,
        )
        if alert:
            alerts.append(alert)

        if isinstance(disks, dict):
            for drive, info in disks.items():
                if not isinstance(info, dict):
                    continue
                alert = self._evaluate_metric(
                    key=f"disk_utilization:{drive}",
                    category="disk",
                    label=f"Disk {drive} utilization",
                    value=info.get("utilization_percent"),
                    threshold=self.thresholds["disk_utilization"],
                    cooldown=self.cooldowns["disk_utilization"],
                    now=now,
                    value_format="{:.1f}%",
                    threshold_format="{}%",
                    critical_margin=None,
                )
                if alert:
                    alerts.append(alert)

        return alerts

    def get_recent_alerts(
        self, limit: int = 50, acknowledged: bool = False
    ) -> List[Dict[str, Any]]:
        """Get recent alerts from the database.

        Args:
            limit: Maximum number of alerts to return.
            acknowledged: If True, only acknowledged alerts.

        Returns:
            List of alert dictionaries.
        """
        try:
            from datetime import datetime

            rows = self.db.get_recent_alerts(
                limit=limit, acknowledged_only=acknowledged
            )
            return [
                {
                    "timestamp": (
                        datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                        if ts
                        else "N/A"
                    ),
                    "level": level,
                    "category": category,
                    "message": message,
                    "acknowledged": bool(ack),
                }
                for ts, level, category, message, ack in rows
            ]
        except Exception as e:
            logger.error("Error retrieving recent alerts: %s", e)
            return []

    # -- internals ------------------------------------------------------

    def _agent_error(self, reason: str) -> List[Dict[str, Any]]:
        """Emit at most one alert for a run of metric-collection failures."""
        now = time.time()
        key = "agent_unreachable"
        if key in self._active:
            last = self._last_alert.get(key, 0.0)
            if now - last < self.cooldowns.get("cpu_temperature", 300):
                return []
        self._active.add(key)
        self._last_alert[key] = now
        return [
            self._record(
                now,
                level="warning",
                category="agent",
                message=(
                    "Monitoring agent could not read system metrics: "
                    f"{reason[:120]}"
                ),
            )
        ]

    def _evaluate_metric(
        self,
        key: str,
        category: str,
        label: str,
        value: Any,
        threshold: float,
        cooldown: float,
        now: float,
        value_format: str,
        threshold_format: str,
        critical_margin: Optional[float],
    ) -> Optional[Dict[str, Any]]:
        """Compare one metric with its threshold and update alert state.

        Args:
            key: Unique state key (also the cooldown key).
            category: Alert category stored in the database.
            label: Human-readable metric name.
            value: Raw value from metrics (may be None or "N/A").
            threshold: Upper bound before an alert is raised.
            cooldown: Seconds between repeated alerts for an active breach.
            now: Current Unix timestamp.
            value_format: Format string for the measured value.
            threshold_format: Format string for the threshold.
            critical_margin: If set, values above threshold+margin are critical.

        Returns:
            A new alert dict, or None if nothing new happened.
        """
        if value is None or value == "N/A":
            return None
        try:
            measured = float(value)
        except (TypeError, ValueError):
            return None

        value_text = value_format.format(measured)
        threshold_text = threshold_format.format(threshold)

        if measured > threshold:
            already_active = key in self._active
            last = self._last_alert.get(key, 0.0)
            if already_active and now - last < cooldown:
                return None
            self._active.add(key)
            self._last_alert[key] = now

            level = "warning"
            if critical_margin is not None and measured > threshold + critical_margin:
                level = "critical"

            suffix = " (still high)" if already_active else ""
            return self._record(
                now,
                level=level,
                category=category,
                message=(
                    f"{label} too high: {value_text} "
                    f"(threshold: {threshold_text}){suffix}"
                ),
            )

        # Value is within bounds. Emit recovery only for a real transition.
        if key in self._active:
            self._active.discard(key)
            self._last_alert.pop(key, None)
            return self._record(
                now,
                level="info",
                category="recovery",
                message=(
                    f"{label} recovered: {value_text} "
                    f"(threshold: {threshold_text})"
                ),
            )
        return None

    def _record(
        self,
        now: float,
        level: str,
        category: str,
        message: str,
    ) -> Dict[str, Any]:
        """Persist an alert and return its dict form for push notification."""
        alert = {
            "timestamp": now,
            "level": level,
            "category": category,
            "message": message,
        }
        try:
            self.db.insert_alert(level=level, category=category, message=message)
        except Exception as e:
            # Notification is still useful even if persistence failed.
            logger.error("Failed to persist alert: %s", e)
        return alert