"""Charts and visualization module for PC Remote.

Generates PNG charts from SQLite measurement history for three time ranges:
last 5 minutes, last hour and last 24 hours. Rows are selected by timestamp
(not by raw row count) so the ranges stay meaningful at any sampling rate.

Matplotlib is configured with the non-interactive ``Agg`` backend and all
rendering is serialized with a lock, because pyplot keeps global state and is
not thread-safe. The bot calls :func:`render_chart` from a worker thread with
a short-lived database connection.
"""

import logging
import threading
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

from database import Database

logger = logging.getLogger(__name__)

# The maximum number of rows plotted per chart. At a 60 s sampling interval,
# 24 hours is 1440 rows; the cap mainly protects against heavy manual polling.
MAX_POINTS = 1500

# time_range -> (human label, width in seconds, x-axis format)
TIME_RANGES: Dict[str, Tuple[str, int, str]] = {
    "5m": ("Last 5 minutes", 300, "%H:%M:%S"),
    "1h": ("Last hour", 3600, "%H:%M"),
    "24h": ("Last 24 hours", 86400, "%H:%M"),
}

DEFAULT_RETENTION = {
    "cpu_history": 10000,
    "gpu_history": 10000,
    "ram_history": 10000,
    "disk_history": 10000,
}

# pyplot is process-global; render one chart at a time.
_RENDER_LOCK = threading.Lock()

try:
    plt.style.use("seaborn-v0_8")
except OSError:  # pragma: no cover - depends on the installed matplotlib
    try:
        plt.style.use("ggplot")
    except OSError:
        pass


def _to_float(value: Any) -> Optional[float]:
    """Convert a database value to float, mapping missing values to None."""
    if value is None or value == "N/A":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _plot_values(values: List[Optional[float]]) -> np.ndarray:
    """Convert optional values to a float array with NaN for gaps."""
    return np.array([np.nan if v is None else v for v in values], dtype=float)


def _dates(timestamps: List[float]) -> List[datetime]:
    """Convert Unix timestamps to timezone-naive local datetimes."""
    return [datetime.fromtimestamp(ts) for ts in timestamps]


class Charts:
    """Generates charts from persistent system measurement data."""

    def __init__(self, db: Database, retention: Optional[Dict[str, int]] = None) -> None:
        """Initialize the Charts class.

        Args:
            db: Database instance with measurement data.
            retention: Optional per-table retention limits (informational;
                pruning itself is handled by ``Database.prune_history``).
        """
        self.db = db
        self.retention = retention or DEFAULT_RETENTION

    # -- fetching -------------------------------------------------------

    def _range_window(self, time_range: str) -> Tuple[str, float]:
        """Return the display label and timestamp cutoff for a range."""
        label, seconds, _ = TIME_RANGES.get(time_range, TIME_RANGES["5m"])
        return label, time.time() - seconds

    def _fetch_cpu_history(
        self, time_range: str = "5m"
    ) -> Tuple[List[float], List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
        """Fetch CPU history within a time range, oldest first.

        Returns:
            Tuple of (timestamps, utilizations, temperatures, frequencies).
        """
        try:
            _, since = self._range_window(time_range)
            records = self.db.get_cpu_history(limit=MAX_POINTS, since=since)
            records = list(reversed(records))
            timestamps = [row[0] for row in records]
            utilizations = [_to_float(row[1]) for row in records]
            temperatures = [_to_float(row[2]) for row in records]
            frequencies = [_to_float(row[3]) for row in records]
            return timestamps, utilizations, temperatures, frequencies
        except Exception as e:
            logger.error("Error fetching CPU history: %s", e)
            return [], [], [], []

    def _fetch_gpu_history(
        self, time_range: str = "5m"
    ) -> Tuple[
        List[float],
        List[Optional[float]],
        List[Optional[float]],
        List[Optional[float]],
        List[Optional[float]],
        List[Optional[float]],
        List[Optional[float]],
        List[Optional[float]],
    ]:
        """Fetch GPU history within a time range, oldest first.

        Returns:
            Tuple of (timestamps, utilizations, temperatures, frequencies,
            memory_utilizations, vram_used_mb, vram_total_mb, power_watts).
        """
        try:
            _, since = self._range_window(time_range)
            records = self.db.get_gpu_history(limit=MAX_POINTS, since=since)
            records = list(reversed(records))
            timestamps = [row[0] for row in records]
            utilizations = [_to_float(row[1]) for row in records]
            temperatures = [_to_float(row[2]) for row in records]
            frequencies = [_to_float(row[3]) for row in records]
            memory_utils = [_to_float(row[4]) for row in records]
            vram_used = [_to_float(row[5]) for row in records]
            vram_total = [_to_float(row[6]) for row in records]
            powers = [_to_float(row[7]) for row in records]
            return (
                timestamps,
                utilizations,
                temperatures,
                frequencies,
                memory_utils,
                vram_used,
                vram_total,
                powers,
            )
        except Exception as e:
            logger.error("Error fetching GPU history: %s", e)
            return [], [], [], [], [], [], [], []

    def _fetch_ram_history(
        self, time_range: str = "5m"
    ) -> Tuple[
        List[float],
        List[Optional[float]],
        List[Optional[float]],
        List[Optional[float]],
        List[Optional[float]],
    ]:
        """Fetch RAM history within a time range, oldest first.

        Returns:
            Tuple of (timestamps, total_mb, used_mb, available_mb,
            utilization_percent).
        """
        try:
            _, since = self._range_window(time_range)
            records = self.db.get_ram_history(limit=MAX_POINTS, since=since)
            records = list(reversed(records))
            timestamps = [row[0] for row in records]
            totals = [_to_float(row[1]) for row in records]
            useds = [_to_float(row[2]) for row in records]
            availables = [_to_float(row[3]) for row in records]
            utils = [_to_float(row[4]) for row in records]
            return timestamps, totals, useds, availables, utils
        except Exception as e:
            logger.error("Error fetching RAM history: %s", e)
            return [], [], [], [], []

    def _fetch_disk_history(
        self, time_range: str = "5m", device_id: Optional[str] = None
    ) -> Tuple[List[float], List[Optional[float]]]:
        """Fetch disk utilization history within a time range, oldest first.

        Returns:
            Tuple of (timestamps, utilization_percent).
        """
        try:
            _, since = self._range_window(time_range)
            records = self.db.get_disk_history(
                limit=MAX_POINTS, since=since, device_id=device_id
            )
            records = list(reversed(records))
            return [row[0] for row in records], [
                _to_float(row[4]) for row in records
            ]
        except Exception as e:
            logger.error("Error fetching disk history: %s", e)
            return [], []

    # -- rendering ------------------------------------------------------

    @staticmethod
    def _style_axis(ax, time_range: str, title: str, ylabel: str) -> None:
        """Apply common labels, grid and time formatting to an axis."""
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.3)
        _, _, date_format = TIME_RANGES.get(time_range, TIME_RANGES["5m"])
        ax.xaxis.set_major_formatter(mdates.DateFormatter(date_format))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        for label in ax.get_xticklabels():
            label.set_rotation(45)
            label.set_horizontalalignment("right")

    @staticmethod
    def _save(fig) -> bytes:
        """Render a figure to PNG bytes and release it."""
        buf = BytesIO()
        try:
            fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        finally:
            plt.close(fig)
        return buf.getvalue()

    def generate_cpu_chart(self, time_range: str = "5m") -> Optional[bytes]:
        """Generate a CPU utilization chart.

        Args:
            time_range: One of "5m", "1h", "24h".

        Returns:
            PNG bytes, or None if no data is available.
        """
        label, _ = self._range_window(time_range)
        timestamps, utilizations, _, _ = self._fetch_cpu_history(time_range)
        if not timestamps:
            logger.warning("No CPU history data available for chart")
            return None

        try:
            with _RENDER_LOCK:
                fig, ax = plt.subplots(figsize=(10, 4.5))
                ax.plot(
                    _dates(timestamps),
                    _plot_values(utilizations),
                    color="#1f77b4",
                    linewidth=1.6,
                    label="CPU utilization",
                )
                ax.set_ylim(0, 100)
                self._style_axis(
                    ax, time_range, f"CPU utilization - {label}", "Utilization (%)"
                )
                ax.legend(loc="upper left")
                fig.tight_layout()
                return self._save(fig)
        except Exception as e:
            logger.error("Error generating CPU chart: %s", e)
            return None

    def generate_gpu_chart(self, time_range: str = "5m") -> Optional[bytes]:
        """Generate a GPU utilization / temperature / VRAM chart.

        Args:
            time_range: One of "5m", "1h", "24h".

        Returns:
            PNG bytes, or None if no data is available.
        """
        label, _ = self._range_window(time_range)
        (
            timestamps,
            utilizations,
            temperatures,
            _frequencies,
            _memory_utils,
            vram_used,
            vram_total,
            _powers,
        ) = self._fetch_gpu_history(time_range)
        if not timestamps:
            logger.warning("No GPU history data available for chart")
            return None

        try:
            with _RENDER_LOCK:
                fig, axes = plt.subplots(3, 1, sharex=True, figsize=(10, 8))
                fig.suptitle(f"GPU metrics - {label}", fontsize=14)
                dates = _dates(timestamps)

                axes[0].plot(
                    dates,
                    _plot_values(utilizations),
                    color="#d62728",
                    linewidth=1.6,
                    label="GPU utilization",
                )
                axes[0].set_ylim(0, 100)
                self._style_axis(
                    axes[0],
                    time_range,
                    "GPU utilization",
                    "Utilization (%)",
                )
                axes[0].legend(loc="upper left")

                axes[1].plot(
                    dates,
                    _plot_values(temperatures),
                    color="#ff7f0e",
                    linewidth=1.6,
                    label="GPU temperature",
                )
                axes[1].set_ylim(bottom=0)
                self._style_axis(
                    axes[1],
                    time_range,
                    "GPU temperature",
                    "Temperature (\u00b0C)",
                )
                axes[1].legend(loc="upper left")

                if any(v is not None for v in vram_used):
                    axes[2].plot(
                        dates,
                        _plot_values(vram_used),
                        color="#2ca02c",
                        linewidth=1.6,
                        label="Used VRAM",
                    )
                    if any(v is not None for v in vram_total):
                        axes[2].plot(
                            dates,
                            _plot_values(vram_total),
                            color="#2ca02c",
                            linestyle="--",
                            linewidth=1.2,
                            label="Total VRAM",
                        )
                    axes[2].legend(loc="upper left")
                else:
                    axes[2].text(
                        0.5,
                        0.5,
                        "No VRAM data available",
                        ha="center",
                        va="center",
                        transform=axes[2].transAxes,
                    )
                axes[2].set_ylim(bottom=0)
                self._style_axis(
                    axes[2], time_range, "VRAM usage", "VRAM (MB)"
                )

                fig.tight_layout(rect=(0, 0, 1, 0.96))
                return self._save(fig)
        except Exception as e:
            logger.error("Error generating GPU chart: %s", e)
            return None

    def generate_ram_chart(self, time_range: str = "5m") -> Optional[bytes]:
        """Generate a RAM usage chart.

        Args:
            time_range: One of "5m", "1h", "24h".

        Returns:
            PNG bytes, or None if no data is available.
        """
        label, _ = self._range_window(time_range)
        timestamps, totals, useds, _availables, _utils = self._fetch_ram_history(
            time_range
        )
        if not timestamps:
            logger.warning("No RAM history data available for chart")
            return None

        try:
            with _RENDER_LOCK:
                fig, ax = plt.subplots(figsize=(10, 4.5))
                dates = _dates(timestamps)
                ax.plot(
                    dates,
                    _plot_values(useds),
                    color="#1f77b4",
                    linewidth=1.6,
                    label="Used memory",
                )
                if any(t is not None for t in totals):
                    ax.plot(
                        dates,
                        _plot_values(totals),
                        color="#1f77b4",
                        linestyle="--",
                        linewidth=1.2,
                        label="Total memory",
                    )
                ax.set_ylim(bottom=0)
                self._style_axis(
                    ax, time_range, f"RAM usage - {label}", "Memory (MB)"
                )
                ax.legend(loc="upper left")
                fig.tight_layout()
                return self._save(fig)
        except Exception as e:
            logger.error("Error generating RAM chart: %s", e)
            return None

    def generate_disk_chart(
        self, time_range: str = "5m", device_id: Optional[str] = None
    ) -> Optional[bytes]:
        """Generate a disk utilization chart.

        Args:
            time_range: One of "5m", "1h", "24h".
            device_id: Optional specific drive; otherwise the busiest drive is
                selected from the newest record.

        Returns:
            PNG bytes, or None if no data is available.
        """
        label, _ = self._range_window(time_range)
        if device_id is None:
            try:
                _, since = self._range_window(time_range)
                recent = self.db.get_disk_history(limit=50, since=since)
                candidates = [
                    (row[4], row[1])
                    for row in recent
                    if row[4] is not None and row[4] != "N/A"
                ]
                if not candidates:
                    logger.warning("No disk history data available for chart")
                    return None
                device_id = max(candidates, key=lambda item: float(item[0]))[1]
            except Exception as e:
                logger.error("Error selecting disk for chart: %s", e)
                return None

        timestamps, utils = self._fetch_disk_history(time_range, device_id)
        if not timestamps:
            logger.warning("No disk history data available for chart")
            return None

        try:
            with _RENDER_LOCK:
                fig, ax = plt.subplots(figsize=(10, 4.5))
                ax.plot(
                    _dates(timestamps),
                    _plot_values(utils),
                    color="#9467bd",
                    linewidth=1.6,
                    label="Disk utilization",
                )
                ax.set_ylim(0, 100)
                self._style_axis(
                    ax,
                    time_range,
                    f"Disk {device_id} utilization - {label}",
                    "Utilization (%)",
                )
                ax.legend(loc="upper left")
                fig.tight_layout()
                return self._save(fig)
        except Exception as e:
            logger.error("Error generating disk chart: %s", e)
            return None

    def generate_chart(
        self,
        metric: str,
        time_range: str = "5m",
        device_id: Optional[str] = None,
    ) -> Optional[bytes]:
        """Generate a chart by metric name.

        Args:
            metric: "cpu", "gpu", "ram" or "disk".
            time_range: One of "5m", "1h", "24h".
            device_id: Optional drive for disk charts (e.g. ``D:\\``).

        Returns:
            PNG bytes, or None if the metric is unknown or has no data.
        """
        if metric == "disk":
            return self.generate_disk_chart(time_range, device_id)

        generators = {
            "cpu": self.generate_cpu_chart,
            "gpu": self.generate_gpu_chart,
            "ram": self.generate_ram_chart,
        }
        generator = generators.get(metric)
        if generator is None:
            logger.warning("Unknown chart metric: %s", metric)
            return None
        return generator(time_range)

    def get_current_status(self) -> Dict[str, Any]:
        """Get the most recent stored metrics.

        Returns:
            Dictionary with current metric values.
        """
        current: Dict[str, Any] = {}
        try:
            cpu_records = self.db.get_cpu_history(limit=1)
            if cpu_records:
                _, util, temp, freq = cpu_records[0]
                current["cpu_utilization"] = _to_float(util)
                current["cpu_temperature"] = _to_float(temp)
                current["cpu_frequency"] = _to_float(freq)

            ram_records = self.db.get_ram_history(limit=1)
            if ram_records:
                _, total, used, available, util = ram_records[0]
                current["ram_utilization_percent"] = _to_float(util)
                current["ram_total_mb"] = _to_float(total)
                current["ram_used_mb"] = _to_float(used)
                current["ram_available_mb"] = _to_float(available)

            gpu_records = self.db.get_gpu_history(limit=1)
            if gpu_records:
                _, util, temp, _freq, mem_util, v_used, v_total, power, *_ = gpu_records[0]
                current["gpu_utilization"] = _to_float(util)
                current["gpu_temperature"] = _to_float(temp)
                current["gpu_memory_util"] = _to_float(mem_util)
                current["gpu_vram_used_mb"] = _to_float(v_used)
                current["gpu_vram_total_mb"] = _to_float(v_total)
                current["gpu_power_watts"] = _to_float(power)
        except Exception as e:
            logger.error("Error getting current status from DB: %s", e)
        return current

    def cleanup(self) -> None:
        """Trim history tables according to the retention limits."""
        try:
            max_rows = min(self.retention.values()) if self.retention else 10000
            self.db.prune_history(max_rows=max_rows)
        except Exception as e:
            logger.error("Error during history cleanup: %s", e)


def render_chart(
    db_path: Path,
    metric: str,
    time_range: str = "5m",
    device_id: Optional[str] = None,
) -> Optional[bytes]:
    """Render a chart with a short-lived database connection.

    Intended to run in a worker thread: SQLite connections are not shared
    across threads, so the bot must not reuse its event-loop connection here.

    Args:
        db_path: Path to the SQLite database.
        metric: "cpu", "gpu", "ram" or "disk".
        time_range: One of "5m", "1h", "24h".
        device_id: Optional drive for disk charts (e.g. ``D:\\``).

    Returns:
        PNG bytes, or None if there is no data.
    """
    db = Database(Path(db_path))
    try:
        return Charts(db).generate_chart(metric, time_range, device_id)
    finally:
        db.close()