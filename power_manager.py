"""Power management module for PC Remote.

Provides safe power management operations for Windows 11.

Security:
- Requires explicit confirmation for destructive operations (shutdown, restart)
- Never executes arbitrary commands
- Validates all requests
- Provides cancellation capability where applicable
"""

import logging
import subprocess
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def lock_workstation() -> Tuple[bool, str]:
    """Lock the Windows workstation.

    Uses the Windows API via rundll32 to lock the workstation.

    Returns:
        Tuple of (success, message).
    """
    try:
        # Windows command to lock the workstation
        result = subprocess.run(
            ["rundll32.exe", "user32.dll,LockWorkStation"],
            capture_output=True,
            timeout=5,
        )

        if result.returncode == 0:
            return True, "🔒 Workstation locked successfully."
        else:
            return False, "❌ Failed to lock workstation."
    except subprocess.TimeoutExpired:
        return False, "❌ Command timed out while locking workstation."
    except Exception as e:
        logger.error(f"Error locking workstation: {e}")
        return False, f"❌ Error locking workstation: {str(e)}"


def sleep_system() -> Tuple[bool, str]:
    """Put the system to sleep.

    Uses SetSuspendState via rundll32: requests suspend (sleep). Depending on
    hibernate settings Windows may hibernate instead - that is the closest
    safe CLI-accessible state to sleep.

    Returns:
        Tuple of (success, message).
    """
    try:
        # powrprof.dll SetSuspendState: Hibernate=False, DisableWakeEvents=True
        result = subprocess.run(
            ["rundll32.exe", "powrprof.dll,SetSuspendState 0,1,0"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            return True, "System sleep initiated."
        else:
            return False, "Failed to initiate system sleep."
    except subprocess.TimeoutExpired:
        return False, "Command timed out while initiating sleep."
    except Exception as e:
        logger.error(f"Error initiating sleep: {e}")
        return False, f"Error initiating sleep: {str(e)}"


def cancel_shutdown() -> Tuple[bool, str]:
    """Cancel a pending scheduled shutdown.

    Returns:
        Tuple of (success, message).
    """
    try:
        result = subprocess.run(
            ["shutdown", "/a"],
            capture_output=True,
            timeout=5,
        )

        if result.returncode == 0:
            return True, "✅ Pending shutdown cancelled."
        else:
            # Check if there was no pending shutdown
            stderr = result.stderr.decode("utf-8", errors="replace")
            if "no shutdown pending" in stderr.lower():
                return True, "ℹ️ No pending shutdown to cancel."
            return False, "❌ Failed to cancel shutdown."
    except subprocess.TimeoutExpired:
        return False, "❌ Command timed out while cancelling shutdown."
    except Exception as e:
        logger.error(f"Error cancelling shutdown: {e}")
        return False, f"❌ Error cancelling shutdown: {str(e)}"


def confirm_shutdown() -> Dict[str, Any]:
    """Generate a shutdown confirmation message.

    Returns a structured confirmation that the caller should present to the user,
    including description of the action and confirmation buttons.

    Returns:
        Dictionary with confirmation details.
    """
    return {
        "action": "shutdown",
        "description": "🔌 **Shutdown Computer**\n\n"
                        "This will completely turn off your Windows 11 PC.\n"
                        "All unsaved work will be lost. Please save your work first.\n\n"
                        "Are you sure you want to proceed?",
        "requires_confirmation": True,
        "destructive": True,
        "cancel_text": "Cancel",
        "confirm_text": "Shutdown",
    }


def confirm_restart() -> Dict[str, Any]:
    """Generate a restart confirmation message.

    Returns a structured confirmation that the caller should present to the user,
    including description of the action and confirmation buttons.

    Returns:
        Dictionary with confirmation details.
    """
    return {
        "action": "restart",
        "description": "🔄 **Restart Computer**\n\n"
                        "This will reboot your Windows 11 PC.\n"
                        "All unsaved work will be lost. Please save your work first.\n\n"
                        "Are you sure you want to proceed?",
        "requires_confirmation": True,
        "destructive": True,
        "cancel_text": "Cancel",
        "confirm_text": "Restart",
    }


def execute_shutdown(confirmed: bool = False) -> Tuple[bool, str]:
    """Execute a system shutdown after confirmation.

    Args:
        confirmed: Whether the user has confirmed the shutdown.

    Returns:
        Tuple of (success, message).
    """
    if not confirmed:
        return False, "ℹ️ Shutdown cancelled - not confirmed."

    try:
        result = subprocess.run(
            ["shutdown", "/s", "/t", "30"],
            capture_output=True,
            timeout=10,
        )

        if result.returncode == 0:
            return True, "Shutdown scheduled: the PC powers off in 30 seconds. Press Cancel Shutdown to abort."
        else:
            stderr = result.stderr.decode("utf-8", errors="replace")
            return False, f"❌ Failed to initiate shutdown: {stderr}"
    except subprocess.TimeoutExpired:
        return False, "❌ Command timed out while initiating shutdown."
    except Exception as e:
        logger.error(f"Error executing shutdown: {e}")
        return False, f"❌ Error initiating shutdown: {str(e)}"


def execute_restart(confirmed: bool = False) -> Tuple[bool, str]:
    """Execute a system restart after confirmation.

    Args:
        confirmed: Whether the user has confirmed the restart.

    Returns:
        Tuple of (success, message).
    """
    if not confirmed:
        return False, "ℹ️ Restart cancelled - not confirmed."

    try:
        result = subprocess.run(
            ["shutdown", "/r", "/t", "30"],
            capture_output=True,
            timeout=10,
        )

        if result.returncode == 0:
            return True, "Restart scheduled: the PC reboots in 30 seconds. Press Cancel Shutdown to abort."
        else:
            stderr = result.stderr.decode("utf-8", errors="replace")
            return False, f"❌ Failed to initiate restart: {stderr}"
    except subprocess.TimeoutExpired:
        return False, "❌ Command timed out while initiating restart."
    except Exception as e:
        logger.error(f"Error executing restart: {e}")
        return False, f"❌ Error initiating restart: {str(e)}"


def get_power_status() -> Dict[str, Any]:
    """Get current power status information.

    Note: Windows has no reliable query API for a pending shutdown. Previously
    this probed with "shutdown /a /q", but /a actually ABORTS a pending
    shutdown - so the status check would silently cancel the user's request.
    We therefore report capabilities only, without touching pending state.

    Returns:
        Dictionary with power status and capabilities.
    """
    return {
        "can_lock": True,
        "can_sleep": True,
        # Cannot be queried safely; assume unknown
        "has_pending_shutdown": False,
        "shutdown_description": "Complete power off (30s delay, cancellable)",
        "restart_description": "Reboot the system (30s delay, cancellable)",
    }