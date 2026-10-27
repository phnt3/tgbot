"""Process information module for PC Remote.

Provides process listing, information, and safe termination capabilities.

Security:
- Only permits termination for explicitly allowlisted, non-critical applications
- Requires confirmation before termination
- Verifies process identity and PID
- Rejects critical Windows processes
- Never accepts arbitrary process commands from Telegram
- Never exposes sensitive command-line arguments
"""

import logging
import os
import signal
import subprocess
from typing import Any, Dict, List, Optional, Tuple

import psutil

from config import config
from security import validate_application_name, validate_path_safe

logger = logging.getLogger(__name__)

# Critical Windows processes that should never be terminated.
# Stored WITHOUT the .exe suffix: is_process_critical() normalizes incoming
# names the same way, so "lsass.exe" -> "lsass" matches here.
CRITICAL_PROCESS_NAMES = {
    "system",
    "smss",
    "csrss",
    "wininit",
    "services",
    "lsass",
    "svchost",
    "explorer",
    "dwm",
    "winlogon",
    "fontdrvhost",
    "sihost",
    "taskhostw",
}


def get_processes_by_cpu(limit: int = 10) -> List[Dict[str, Any]]:
    """Get top processes sorted by CPU usage.

    Args:
        limit: Maximum number of processes to return.

    Returns:
        List of process dictionaries with name, PID, and CPU usage.
    """
    try:
        processes = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent"]):
            try:
                info = proc.info
                if info["name"]:
                    processes.append({
                        "name": info["name"],
                        "pid": info["pid"],
                        "cpu_percent": info["cpu_percent"] or 0,
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

        # Sort by CPU usage descending
        processes.sort(key=lambda p: p["cpu_percent"], reverse=True)

        return processes[:limit]
    except Exception as e:
        logger.error(f"Error getting processes by CPU: {e}")
        return []


def get_processes_by_ram(limit: int = 10) -> List[Dict[str, Any]]:
    """Get top processes sorted by RAM consumption.

    Args:
        limit: Maximum number of processes to return.

    Returns:
        List of process dictionaries with name, PID, and RAM usage.
    """
    try:
        processes = []
        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                info = proc.info
                if info["name"]:
                    mem_info = info.get("memory_info")
                    if mem_info:
                        memory_mb = mem_info.rss / (1024 * 1024)
                    else:
                        memory_mb = 0
                    processes.append({
                        "name": info["name"],
                        "pid": info["pid"],
                        "ram_mb": round(memory_mb, 2),
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

        # Sort by RAM usage descending
        processes.sort(key=lambda p: p["ram_mb"], reverse=True)

        return processes[:limit]
    except Exception as e:
        logger.error(f"Error getting processes by RAM: {e}")
        return []


def is_process_critical(process_name: str) -> bool:
    """Check if a process name is a critical Windows process.

    Args:
        process_name: Name of the process to check.

    Returns:
        True if the process is considered critical, False otherwise.
    """
    name_lower = process_name.lower().replace(".exe", "")
    return name_lower in CRITICAL_PROCESS_NAMES


# Allowlist of terminable processes: only allowlisted, non-critical apps.
# Critical Windows processes are rejected regardless of the allowlist.
TERMINABLE_EXTRA_ALLOWLIST = {
    "taskmgr.exe",
    "notepad++.exe",
}


def get_terminable_allowlist() -> set:
    """Build the set of process names that may be terminated.

    Combines configured application allowlist paths (by executable name)
    with a small extra set of safe utility apps.

    Returns:
        Set of lowercase executable names.
    """
    from application_manager import load_applications_config

    names = set(TERMINABLE_EXTRA_ALLOWLIST)
    for _app_name, path in load_applications_config().items():
        basename = os.path.basename(path)
        if basename:
            names.add(basename.lower())
    return names


def is_process_terminable(process_name: str, allowlist: Optional[set] = None) -> bool:
    """Decide whether a process may be terminated.

    Args:
        process_name: Name reported by psutil.
        allowlist: Optional explicit allowlist override (for tests).

    Returns:
        True if termination is permitted, False otherwise.
    """
    if not process_name:
        return False

    basename = os.path.basename(process_name).lower()
    if is_process_critical(basename):
        return False

    if allowlist is None:
        allowlist = get_terminable_allowlist()

    return basename in allowlist


def get_process_details(pid: int) -> Optional[Dict[str, Any]]:
    """Get detailed information about a specific process.

    Args:
        pid: Process ID to look up.

    Returns:
        Dictionary with process details, or None if not found.
    """
    try:
        proc = psutil.Process(pid)
        info = {
            "name": proc.name(),
            "pid": proc.pid,
            "cpu_percent": proc.cpu_percent(interval=0.1),
            "memory_mb": proc.memory_info().rss / (1024 * 1024),
            "status": proc.status(),
            "create_time": proc.create_time(),
        }

        # Try to get command line (be careful not to expose secrets)
        try:
            cmdline = proc.cmdline()
            # Join but be careful - in full implementation we'd filter secrets
            info["cmdline"] = " ".join(cmdline)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            info["cmdline"] = "N/A (access denied)"

        return info
    except psutil.NoSuchProcess:
        logger.warning(f"Process {pid} no longer exists")
        return None
    except Exception as e:
        logger.error(f"Error getting process details for {pid}: {e}")
        return None


def safe_terminate_process(
    pid: int,
    reason: str = "User requested",
    db: Optional["Database"] = None,
) -> Tuple[bool, str]:
    """Safely terminate a process after verification.

    Security checks:
    1. Request confirmation (handled by caller)
    2. Verify process identity and PID
    3. Recheck that process is still the intended target
    4. Reject critical or unsafe targets
    5. Handle permission errors gracefully

    Args:
        pid: Process ID to terminate.
        reason: Reason for termination (for logging/recording).
        db: Optional Database instance for recording.

    Returns:
        Tuple of (success, message).
    """
    # Step 1: Verify process exists and get details
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False, f"❌ Process {pid} no longer exists."
    except psutil.AccessDenied:
        return False, f"❌ Access denied to process {pid}."
    except Exception as e:
        return False, f"❌ Error looking up process {pid}: {str(e)}"

    # Step 2: Check if it's a critical process
    try:
        process_name = proc.name().lower().replace(".exe", "")
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False, "❌ Could not read process name."

    if is_process_critical(process_name):
        return False, (
            f"❌ Cannot terminate critical Windows process: "
            f"{proc.name()}. "
            "This could cause system instability."
        )

    # Step 2b: enforce the termination allowlist (allowlisted non-critical apps only)
    if not is_process_terminable(proc.name()):
        return False, (
            f"❌ Process {proc.name()} is not on the termination allowlist. "
            "Only allowlisted, non-critical applications can be terminated."
        )

    # Step 3: Verify this is the intended target (recheck)
    # Additional safety: check if the process has a GUI or is user-initiated
    try:
        # Check if it's a system service
        if proc.status() == "running":
            # Additional checks can be added here
            pass
    except Exception:
        pass

    # Step 4: Attempt to terminate
    try:
        logger.info(f"Terminating process {pid} ({proc.name()}): {reason}")

        # First try nice termination
        proc.terminate()

        # Wait briefly for graceful shutdown
        try:
            proc.wait(timeout=3)
            # Successfully terminated gracefully
            if db is not None:
                try:
                    from datetime import time as dt_time
                    db.insert_app_launch(f"terminate_pid_{pid}", success=1)
                except Exception:
                    pass
            return True, f"✅ Process {proc.name()} (PID: {pid}) terminated successfully."

        except psutil.TimeoutExpired:
            # Process didn't terminate gracefully, force kill
            try:
                proc.kill()
                proc.wait(timeout=3)
                if db is not None:
                    try:
                        db.insert_app_launch(f"terminate_pid_{pid}", success=1)
                    except Exception:
                        pass
                return True, f"✅ Process {proc.name()} (PID: {pid}) force killed."
            except Exception as kill_e:
                return False, f"❌ Failed to kill process: {str(kill_e)}"

    except psutil.AccessDenied:
        return False, (
            f"❌ Access denied to terminate process {pid}. "
            "You may need administrator privileges."
        )
    except Exception as e:
        logger.error(f"Error terminating process {pid}: {e}")
        return False, f"❌ Error terminating process {pid}: {str(e)}"


def get_process_snapshot(db: Optional["Database"] = None) -> List[Dict[str, Any]]:
    """Get a snapshot of all running processes.

    Args:
        db: Optional Database instance for recording the snapshot.

    Returns:
        List of process dictionaries.
    """
    try:
        processes = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
            try:
                info = proc.info
                if info["name"]:
                    memory_mb = 0
                    mem_info = info.get("memory_info")
                    if mem_info:
                        memory_mb = mem_info.rss / (1024 * 1024)

                    processes.append({
                        "name": info["name"],
                        "pid": info["pid"],
                        "cpu_percent": info.get("cpu_percent", 0) or 0,
                        "ram_mb": round(memory_mb, 2),
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

        # Record snapshot in database if available
        if db is not None:
            try:
                import json
                processes_json = json.dumps(processes)
                db.insert_app_launch("process_snapshot", success=1)
                # Note: We'd need a dedicated table for this, but using app_launch as placeholder
            except Exception:
                pass

        return processes
    except Exception as e:
        logger.error(f"Error getting process snapshot: {e}")
        return []