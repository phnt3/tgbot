"""SQLite database module for PC Remote bot.

Handles persistent storage of monitoring data, alerts, application launches,
and history measurements.
"""

import sqlite3
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class Database:
    """SQLite database handler for PC Remote."""

    def __init__(self, db_path: Path) -> None:
        """Initialize database connection and create tables if needed.

        Args:
            db_path: Path to the SQLite database file.
        """
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._connect()
        self._create_tables()

    def _connect(self) -> None:
        """Establish database connection."""
        try:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error as e:
            raise RuntimeError(f"Failed to connect to database: {e}")

    def _create_tables(self) -> None:
        """Create all required tables if they don't exist."""
        if self._conn is None:
            raise RuntimeError("Database connection not established")

        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS measurements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                metric_name TEXT NOT NULL,
                metric_value TEXT NOT NULL,
                unit TEXT
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                level TEXT NOT NULL,
                category TEXT NOT NULL,
                message TEXT NOT NULL,
                acknowledged INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS app_launches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                application_name TEXT NOT NULL,
                success INTEGER DEFAULT 1,
                error_message TEXT
            );

            CREATE TABLE IF NOT EXISTS system_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                hostname TEXT,
                uptime_seconds REAL,
                internet_connectivity INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS cpu_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                utilization_percent REAL,
                temperature_c REAL,
                frequency_mhz REAL
            );

            CREATE TABLE IF NOT EXISTS gpu_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                utilization_percent REAL,
                temperature_c REAL,
                frequency_mhz REAL,
                memory_utilization_percent REAL,
                vram_used_mb REAL,
                vram_total_mb REAL,
                power_watts REAL
            );

            CREATE TABLE IF NOT EXISTS ram_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                total_mb REAL,
                used_mb REAL,
                available_mb REAL,
                utilization_percent REAL
            );

            CREATE TABLE IF NOT EXISTS disk_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                device_id TEXT NOT NULL,
                total_mb REAL,
                available_mb REAL,
                utilization_percent REAL,
                temperature_c REAL
            );

            CREATE TABLE IF NOT EXISTS process_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                processes_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
        """)
        self._conn.commit()

    # Measurement methods
    def insert_measurement(
        self,
        metric_name: str,
        metric_value: str,
        unit: Optional[str] = None,
    ) -> None:
        """Insert a metric measurement.

        Args:
            metric_name: Name of the metric (e.g., 'cpu_utilization').
            metric_value: Value as string.
            unit: Optional unit (e.g., '%', '°C', 'MHz').
        """
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        try:
            self._conn.execute(
                "INSERT INTO measurements (timestamp, metric_name, metric_value, unit) "
                "VALUES (?, ?, ?, ?)",
                (time.time(), metric_name, metric_value, unit),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            raise RuntimeError(f"Failed to insert measurement: {e}")

    def insert_alert(
        self,
        level: str,
        category: str,
        message: str,
        acknowledged: int = 0,
    ) -> None:
        """Insert an alert record.

        Args:
            level: Alert level (e.g., 'critical', 'warning', 'info').
            category: Alert category (e.g., 'cpu', 'gpu', 'ram', 'disk').
            message: Human-readable alert message.
            acknowledged: Whether the alert has been acknowledged (0 or 1).
        """
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        try:
            self._conn.execute(
                "INSERT INTO alerts (timestamp, level, category, message, acknowledged) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), level, category, message, acknowledged),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            raise RuntimeError(f"Failed to insert alert: {e}")

    def insert_app_launch(
        self,
        application_name: str,
        success: int = 1,
        error_message: Optional[str] = None,
    ) -> None:
        """Record an application launch event.

        Args:
            application_name: Name of the launched application.
            success: 1 if launch succeeded, 0 if failed.
            error_message: Optional error message if launch failed.
        """
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        try:
            self._conn.execute(
                "INSERT INTO app_launches (timestamp, application_name, success, error_message) "
                "VALUES (?, ?, ?, ?)",
                (time.time(), application_name, success, error_message),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            raise RuntimeError(f"Failed to record app launch: {e}")

    def insert_system_status(
        self,
        hostname: Optional[str],
        uptime_seconds: Optional[float],
        internet_connectivity: int = 0,
    ) -> None:
        """Record system status snapshot.

        Args:
            hostname: Hostname of the system.
            uptime_seconds: System uptime in seconds.
            internet_connectivity: 1 if internet appears reachable, 0 otherwise.
        """
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        try:
            self._conn.execute(
                "INSERT INTO system_status (timestamp, hostname, uptime_seconds, "
                "internet_connectivity) VALUES (?, ?, ?, ?)",
                (time.time(), hostname, uptime_seconds, internet_connectivity),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            raise RuntimeError(f"Failed to record system status: {e}")

    def _insert_history(
        self,
        table: str,
        columns: List[str],
        values: List[Any],
        timestamp: Optional[float] = None,
    ) -> None:
        """Insert one row into a history table.

        Args:
            table: History table name (internal constant only).
            columns: Column names to write (excluding timestamp).
            values: Values matching ``columns``.
            timestamp: Optional timestamp override (defaults to now).
        """
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        try:
            placeholders = ", ".join("?" for _ in range(len(columns) + 1))
            self._conn.execute(
                f"INSERT INTO {table} (timestamp, {', '.join(columns)}) "
                f"VALUES ({placeholders})",
                (time.time() if timestamp is None else timestamp, *values),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            raise RuntimeError(f"Failed to insert {table} record: {e}")

    def insert_cpu_history(
        self,
        utilization_percent: Optional[float],
        temperature_c: Optional[float] = None,
        frequency_mhz: Optional[float] = None,
        timestamp: Optional[float] = None,
    ) -> None:
        """Record a CPU measurement for charts and history."""
        self._insert_history(
            "cpu_history",
            ["utilization_percent", "temperature_c", "frequency_mhz"],
            [utilization_percent, temperature_c, frequency_mhz],
            timestamp,
        )

    def insert_gpu_history(
        self,
        utilization_percent: Optional[float],
        temperature_c: Optional[float] = None,
        frequency_mhz: Optional[float] = None,
        memory_utilization_percent: Optional[float] = None,
        vram_used_mb: Optional[float] = None,
        vram_total_mb: Optional[float] = None,
        power_watts: Optional[float] = None,
        timestamp: Optional[float] = None,
    ) -> None:
        """Record a GPU measurement for charts and history."""
        self._insert_history(
            "gpu_history",
            [
                "utilization_percent",
                "temperature_c",
                "frequency_mhz",
                "memory_utilization_percent",
                "vram_used_mb",
                "vram_total_mb",
                "power_watts",
            ],
            [
                utilization_percent,
                temperature_c,
                frequency_mhz,
                memory_utilization_percent,
                vram_used_mb,
                vram_total_mb,
                power_watts,
            ],
            timestamp,
        )

    def insert_ram_history(
        self,
        total_mb: Optional[float],
        used_mb: Optional[float],
        available_mb: Optional[float],
        utilization_percent: Optional[float],
        timestamp: Optional[float] = None,
    ) -> None:
        """Record a RAM measurement for charts and history."""
        self._insert_history(
            "ram_history",
            ["total_mb", "used_mb", "available_mb", "utilization_percent"],
            [total_mb, used_mb, available_mb, utilization_percent],
            timestamp,
        )

    def insert_disk_history(
        self,
        device_id: str,
        total_mb: Optional[float],
        available_mb: Optional[float],
        utilization_percent: Optional[float],
        temperature_c: Optional[float] = None,
        timestamp: Optional[float] = None,
    ) -> None:
        """Record a disk measurement for charts and history."""
        self._insert_history(
            "disk_history",
            [
                "device_id",
                "total_mb",
                "available_mb",
                "utilization_percent",
                "temperature_c",
            ],
            [device_id, total_mb, available_mb, utilization_percent, temperature_c],
            timestamp,
        )

    def prune_history(self, max_rows: int = 10000) -> int:
        """Trim history tables to the newest ``max_rows`` rows each.

        Args:
            max_rows: Maximum number of rows to keep per table.

        Returns:
            Number of deleted rows.
        """
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        deleted = 0
        try:
            for table in ("cpu_history", "gpu_history", "ram_history", "disk_history"):
                cursor = self._conn.execute(
                    f"DELETE FROM {table} WHERE id NOT IN "
                    f"(SELECT id FROM {table} ORDER BY timestamp DESC, id DESC LIMIT ?)",
                    (max_rows,),
                )
                deleted += cursor.rowcount if cursor.rowcount > 0 else 0
            self._conn.commit()
        except sqlite3.Error as e:
            raise RuntimeError(f"Failed to prune history: {e}")
        return deleted

    # Query methods
    def get_recent_measurements(
        self,
        metric_name: str,
        limit: int = 100,
    ) -> List[Tuple[float, str, Optional[str]]]:
        """Get recent measurements for a specific metric.

        Args:
            metric_name: Name of the metric to query.
            limit: Maximum number of records to return.

        Returns:
            List of (timestamp, metric_value, unit) tuples.
        """
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        try:
            cursor = self._conn.execute(
                "SELECT timestamp, metric_value, unit FROM measurements "
                "WHERE metric_name = ? ORDER BY timestamp DESC LIMIT ?",
                (metric_name, limit),
            )
            return cursor.fetchall()
        except sqlite3.Error as e:
            raise RuntimeError(f"Failed to query measurements: {e}")

    def get_recent_alerts(
        self,
        limit: int = 50,
        acknowledged_only: bool = False,
    ) -> List[Tuple[float, str, str, str, int]]:
        """Get recent alerts.

        Args:
            limit: Maximum number of alerts to return.
            acknowledged_only: If True, only return acknowledged alerts.

        Returns:
            List of (timestamp, level, category, message, acknowledged) tuples.
        """
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        try:
            query = (
                "SELECT timestamp, level, category, message, acknowledged FROM alerts "
                "ORDER BY timestamp DESC, id DESC LIMIT ?"
            )
            params = [limit]
            if acknowledged_only:
                query = (
                    "SELECT timestamp, level, category, message, acknowledged FROM alerts "
                    "WHERE acknowledged = 1 ORDER BY timestamp DESC, id DESC LIMIT ?"
                )
                params = [limit]
            cursor = self._conn.execute(query, params)
            return cursor.fetchall()
        except sqlite3.Error as e:
            raise RuntimeError(f"Failed to query alerts: {e}")

    def get_recent_app_launches(self, limit: int = 50) -> List[Tuple[float, str, int, Optional[str]]]:
        """Get recent application launch records.

        Args:
            limit: Maximum number of records to return.

        Returns:
            List of (timestamp, application_name, success, error_message) tuples.
        """
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        try:
            cursor = self._conn.execute(
                "SELECT timestamp, application_name, success, error_message "
                "FROM app_launches ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            return cursor.fetchall()
        except sqlite3.Error as e:
            raise RuntimeError(f"Failed to query app launches: {e}")

    def get_system_status(self) -> Optional[Tuple[float, str, Optional[float], int]]:
        """Get the most recent system status.

        Returns:
            Tuple of (timestamp, hostname, uptime_seconds, internet_connectivity)
            or None if no records exist.
        """
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        try:
            cursor = self._conn.execute(
                "SELECT timestamp, hostname, uptime_seconds, internet_connectivity "
                "FROM system_status ORDER BY timestamp DESC LIMIT 1"
            )
            return cursor.fetchone()
        except sqlite3.Error as e:
            raise RuntimeError(f"Failed to query system status: {e}")

    def get_cpu_history(
        self,
        limit: int = 100,
        since: Optional[float] = None,
    ) -> List[Tuple[float, Optional[float], Optional[float], Optional[float]]]:
        """Get CPU history records.

        Args:
            limit: Maximum number of records to return.
            since: Optional Unix timestamp lower bound (inclusive).

        Returns:
            List of (timestamp, utilization_percent, temperature_c, frequency_mhz) tuples.
        """
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        try:
            query = (
                "SELECT timestamp, utilization_percent, temperature_c, frequency_mhz "
                "FROM cpu_history"
            )
            params: List[Any] = []
            if since is not None:
                query += " WHERE timestamp >= ?"
                params.append(since)
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            cursor = self._conn.execute(query, params)
            return cursor.fetchall()
        except sqlite3.Error as e:
            raise RuntimeError(f"Failed to query CPU history: {e}")

    def get_gpu_history(
        self,
        limit: int = 100,
        since: Optional[float] = None,
    ) -> List[Tuple[float, Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]]:
        """Get GPU history records.

        Args:
            limit: Maximum number of records to return.
            since: Optional Unix timestamp lower bound (inclusive).

        Returns:
            List of (timestamp, utilization_percent, temperature_c, frequency_mhz,
            memory_utilization_percent, vram_used_mb, vram_total_mb, power_watts) tuples.
        """
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        try:
            query = (
                "SELECT timestamp, utilization_percent, temperature_c, frequency_mhz, "
                "memory_utilization_percent, vram_used_mb, vram_total_mb, power_watts "
                "FROM gpu_history"
            )
            params: List[Any] = []
            if since is not None:
                query += " WHERE timestamp >= ?"
                params.append(since)
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            cursor = self._conn.execute(query, params)
            return cursor.fetchall()
        except sqlite3.Error as e:
            raise RuntimeError(f"Failed to query GPU history: {e}")

    def get_ram_history(
        self,
        limit: int = 100,
        since: Optional[float] = None,
    ) -> List[Tuple[float, Optional[float], Optional[float], Optional[float], Optional[float]]]:
        """Get RAM history records.

        Args:
            limit: Maximum number of records to return.
            since: Optional Unix timestamp lower bound (inclusive).

        Returns:
            List of (timestamp, total_mb, used_mb, available_mb, utilization_percent) tuples.
        """
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        try:
            query = (
                "SELECT timestamp, total_mb, used_mb, available_mb, utilization_percent "
                "FROM ram_history"
            )
            params: List[Any] = []
            if since is not None:
                query += " WHERE timestamp >= ?"
                params.append(since)
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            cursor = self._conn.execute(query, params)
            return cursor.fetchall()
        except sqlite3.Error as e:
            raise RuntimeError(f"Failed to query RAM history: {e}")

    def get_disk_history(
        self,
        limit: int = 100,
        since: Optional[float] = None,
        device_id: Optional[str] = None,
    ) -> List[Tuple[float, str, Optional[float], Optional[float], Optional[float], Optional[float]]]:
        """Get disk history records.

        Args:
            limit: Maximum number of records to return.
            since: Optional Unix timestamp lower bound (inclusive).
            device_id: Optional drive filter (e.g. "C:").

        Returns:
            List of (timestamp, device_id, total_mb, available_mb, utilization_percent, temperature_c) tuples.
        """
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        try:
            query = (
                "SELECT timestamp, device_id, total_mb, available_mb, utilization_percent, temperature_c "
                "FROM disk_history"
            )
            conditions: List[str] = []
            params: List[Any] = []
            if since is not None:
                conditions.append("timestamp >= ?")
                params.append(since)
            if device_id is not None:
                conditions.append("device_id = ?")
                params.append(device_id)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            cursor = self._conn.execute(query, params)
            return cursor.fetchall()
        except sqlite3.Error as e:
            raise RuntimeError(f"Failed to query disk history: {e}")

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None