"""System monitoring module for PC Remote.

Collects system metrics using psutil and nvidia-ml-py for display in Telegram.

Target operating system: Windows 11.
"""

import logging
import platform
import socket
import uuid
from typing import Any, Dict, Optional

import psutil

try:
    import pynvml

    pynvml.nvmlInit()
    NVML_AVAILABLE = True
except ImportError:
    NVML_AVAILABLE = False
    logger = logging.getLogger(__name__)


logger = logging.getLogger(__name__)


def get_hostname() -> str:
    """Get the system hostname."""
    try:
        return platform.node()
    except Exception:
        return "N/A"


def get_uptime_seconds() -> Optional[float]:
    """Get system uptime in seconds.

    Returns:
        Uptime in seconds, or None if unavailable.
    """
    try:
        # psutil.boot_time() returns the system boot time as a timestamp
        boot_time = psutil.boot_time()
        current_time = __import__("time").time()
        return current_time - boot_time
    except Exception:
        return None


def check_internet_connectivity() -> bool:
    """Check if the system has internet connectivity.

    Attempts to connect to a well-known host to verify connectivity.

    Returns:
        True if internet appears reachable, False otherwise.
    """
    try:
        # Try to resolve google.com
        socket.create_connection(("8.8.8.8", 53), timeout=3)
        return True
    except (OSError, socket.timeout):
        return False


def get_cpu_metrics() -> Dict[str, Any]:
    """Collect CPU metrics.

    Returns:
        Dictionary with CPU metrics. Uses "N/A" for unavailable values.
    """
    try:
        cpu_info = {
            "utilization": "N/A",
            "temperature": None,
            "frequency": None,
            "physical_cores": psutil.cpu_count(logical=False) or None,
            "logical_cores": psutil.cpu_count(logical=True) or None,
        }

        # Utilization percentage
        try:
            cpu_info["utilization"] = psutil.cpu_percent(interval=0.1)
        except Exception:
            pass

        # Temperature when available
        if NVML_AVAILABLE:
            try:
                handle = pynvml.nvmlSystemGetDefaultSystemDevice()
                if handle:
                    # Try to get GPU temperature as fallback; CPU temp not always available via NVML
                    gpu_temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                    cpu_info["temperature"] = gpu_temp
            except Exception:
                pass

        # Current frequency
        try:
            freq = psutil.cpu_freq()
            if freq:
                cpu_info["frequency"] = freq.current
        except Exception:
            pass

        # Ensure numeric values are properly typed
        if cpu_info["utilization"] is not None:
            cpu_info["utilization"] = float(cpu_info["utilization"])
        if cpu_info["frequency"] is not None:
            cpu_info["frequency"] = float(cpu_info["frequency"])

        # Replace None with "N/A" for display
        display = {}
        for key, value in cpu_info.items():
            if value is None:
                display[key] = "N/A"
            else:
                display[key] = value
        return display

    except Exception as e:
        logger.error(f"Error collecting CPU metrics: {e}")
        return {
            "utilization": "N/A",
            "temperature": "N/A",
            "frequency": "N/A",
            "physical_cores": "N/A",
            "logical_cores": "N/A",
        }


def get_ram_metrics() -> Dict[str, Any]:
    """Collect RAM metrics.

    Returns:
        Dictionary with RAM metrics. Uses "N/A" for unavailable values.
    """
    try:
        mem = psutil.virtual_memory()

        return {
            "total_mb": total_to_mb(mem.total),
            "used_mb": total_to_mb(mem.used),
            "available_mb": total_to_mb(mem.available),
            "utilization_percent": mem.percent,
        }
    except Exception as e:
        logger.error(f"Error collecting RAM metrics: {e}")
        return {
            "total_mb": "N/A",
            "used_mb": "N/A",
            "available_mb": "N/A",
            "utilization_percent": "N/A",
        }


def total_to_mb(bytes_value: int) -> float:
    """Convert bytes to megabytes.

    Args:
        bytes_value: Value in bytes.

    Returns:
        Value in megabytes.
    """
    return bytes_value / (1024 * 1024)


def get_disk_metrics() -> Dict[str, Dict[str, Any]]:
    """Collect disk/storage metrics for all partitions.

    Returns:
        Dictionary mapping drive letters to disk metrics.
        Uses "N/A" for unavailable values.
    """
    try:
        disks = {}
        partitions = psutil.disk_partitions(all=False)

        for partition in partitions:
            try:
                partition_mount = partition.mountpoint
                partition_id = partition.device or partition.mountpoint

                # Get disk usage
                usage = psutil.disk_usage(partition_mount)

                # Try to get temperature (may not be available on all systems)
                temperature = "N/A"
                try:
                    # Attempt to read temperature via various methods
                    partitions_by_device = psutil.disk_partitions()
                    for p in partitions_by_device:
                        if p.device == partition.device or p.mountpoint == partition_mount:
                            # Some systems expose temperature through ioctl or other means
                            temperature = "N/A"
                            break
                except Exception:
                    pass

                disks[partition_id] = {
                    "total_mb": total_to_mb(usage.total),
                    "used_mb": total_to_mb(usage.used),
                    "available_mb": total_to_mb(usage.free),
                    "utilization_percent": usage.percent,
                    "temperature": temperature,
                }
            except Exception as e:
                logger.warning(f"Error collecting disk metrics for {partition.mountpoint}: {e}")
                disks[partition.mountpoint] = {
                    "total_mb": "N/A",
                    "used_mb": "N/A",
                    "available_mb": "N/A",
                    "utilization_percent": "N/A",
                    "temperature": "N/A",
                }

        return disks
    except Exception as e:
        logger.error(f"Error collecting disk metrics: {e}")
        return {}


def get_gpu_metrics() -> Dict[str, Any]:
    """Collect NVIDIA GPU metrics using NVML.

    Returns:
        Dictionary with GPU metrics. Uses "N/A" for unavailable values.
        Falls back gracefully if NVML is not available.
    """
    if not NVML_AVAILABLE:
        return {
            "utilization": "N/A",
            "temperature": "N/A",
            "frequency": "N/A",
            "memory_utilization": "N/A",
            "vram_used_mb": "N/A",
            "vram_total_mb": "N/A",
            "power_watts": "N/A",
        }

    try:
        # Get the default NVIDIA GPU handle
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception:
            # Try to find any NVIDIA device
            handle = None
            try:
                device_count = pynvml.nvmlDeviceGetCount()
                if device_count > 0:
                    handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            except Exception:
                handle = None

        if not handle:
            return {
                "utilization": "N/A",
                "temperature": "N/A",
                "frequency": "N/A",
                "memory_utilization": "N/A",
                "vram_used_mb": "N/A",
                "vram_total_mb": "N/A",
                "power_watts": "N/A",
            }

        gpu = {
            "utilization": "N/A",
            "temperature": "N/A",
            "frequency": "N/A",
            "memory_utilization": "N/A",
            "vram_used_mb": "N/A",
            "vram_total_mb": "N/A",
            "power_watts": "N/A",
        }

        # GPU utilization
        try:
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            gpu["utilization"] = util.gpu
        except Exception:
            pass

        # GPU temperature
        try:
            temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            gpu["temperature"] = temp
        except Exception:
            pass

        # GPU frequency
        try:
            freq_handle = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_GRAPHICS)
            gpu["frequency"] = freq_handle.current
        except Exception:
            pass

        # Memory information
        try:
            mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            gpu["vram_total_mb"] = mem_info.total / (1024 * 1024)
            gpu["vram_used_mb"] = mem_info.used / (1024 * 1024)
            if gpu["vram_total_mb"] > 0:
                gpu["memory_utilization"] = (
                    gpu["vram_used_mb"] / gpu["vram_total_mb"] * 100
                )
        except Exception:
            pass

        # Power consumption
        try:
            power = pynvml.nvmlDeviceGetPowerUsage(handle)
            # Convert from milliwatts to watts
            gpu["power_watts"] = power / 1000.0
        except Exception:
            pass

        # Ensure proper types for display
        for key in gpu:
            if gpu[key] is not None:
                try:
                    gpu[key] = float(gpu[key])
                except (ValueError, TypeError):
                    pass

        # Replace None/N/A properly
        display = {}
        for key, value in gpu.items():
            if value is None or (isinstance(value, float) and value < 0):
                display[key] = "N/A"
            else:
                display[key] = value

        return display

    except Exception as e:
        logger.error(f"Error collecting GPU metrics: {e}")
        return {
            "utilization": "N/A",
            "temperature": "N/A",
            "frequency": "N/A",
            "memory_utilization": "N/A",
            "vram_used_mb": "N/A",
            "vram_total_mb": "N/A",
            "power_watts": "N/A",
        }


def get_system_info() -> Dict[str, Any]:
    """Get general system information.

    Returns:
        Dictionary with system information.
    """
    try:
        return {
            "hostname": get_hostname(),
            "platform": platform.platform(),
            "python_version": platform.python_version(),
        }
    except Exception as e:
        logger.error(f"Error collecting system info: {e}")
        return {
            "hostname": "N/A",
            "platform": "N/A",
            "python_version": "N/A",
        }


def get_all_metrics() -> Dict[str, Any]:
    """Collect all available system metrics.

    This is the main entry point for collecting a complete set of system metrics.
    Each metric section independently handles failures so one failure doesn't
    stop other monitoring functions.

    Returns:
        Dictionary with all system metrics structured for display.
    """
    try:
        # Gather all metrics - each section is independent
        cpu = get_cpu_metrics()
        ram = get_ram_metrics()
        disks = get_disk_metrics()
        gpu = get_gpu_metrics()
        system = get_system_info()

        internet = check_internet_connectivity()
        uptime = get_uptime_seconds()
        hostname = system.get("hostname", "N/A")

        return {
            "hostname": hostname,
            "uptime": uptime,
            "internet": internet,
            "cpu": cpu,
            "ram": ram,
            "disk": disks,
            "gpu": gpu,
        }
    except Exception as e:
        logger.error(f"Unexpected error in get_all_metrics: {e}")
        # Return minimal viable structure
        return {
            "hostname": "N/A",
            "uptime": None,
            "internet": False,
            "cpu": {
                "utilization": "N/A",
                "temperature": "N/A",
                "frequency": "N/A",
                "physical_cores": "N/A",
                "logical_cores": "N/A",
            },
            "ram": {
                "total_mb": "N/A",
                "used_mb": "N/A",
                "available_mb": "N/A",
                "utilization_percent": "N/A",
            },
            "disk": {},
            "gpu": {
                "utilization": "N/A",
                "temperature": "N/A",
                "frequency": "N/A",
                "memory_utilization": "N/A",
                "vram_used_mb": "N/A",
                "vram_total_mb": "N/A",
                "power_watts": "N/A",
            },
        }