"""Tests for hardware metric collection.

psutil, socket and NVML are replaced with fakes, so the suite never depends on
the machine's actual hardware and never opens a network connection.
"""

import time
from types import SimpleNamespace

import pytest

import monitor


# ---------- small helpers ----------

class FakeNvml:
    """Minimal pynvml stand-in with configurable values."""

    NVML_TEMPERATURE_GPU = 0
    NVML_CLOCK_GRAPHICS = 0

    def __init__(self, fail_handle=False):
        self.fail_handle = fail_handle
        self.handle = object()

    def nvmlDeviceGetHandleByIndex(self, index):
        if self.fail_handle:
            raise RuntimeError("no devices")
        return self.handle

    def nvmlDeviceGetCount(self):
        return 0 if self.fail_handle else 1

    def nvmlDeviceGetUtilizationRates(self, handle):
        return SimpleNamespace(gpu=42, memory=10)

    def nvmlDeviceGetTemperature(self, handle, sensor):
        return 61

    def nvmlDeviceGetClockInfo(self, handle, clock_type):
        # Production code reads ``.current`` from this result.
        return SimpleNamespace(current=1500)

    def nvmlDeviceGetMemoryInfo(self, handle):
        return SimpleNamespace(
            total=4096 * 1024 * 1024,
            used=1024 * 1024 * 1024,
            free=3072 * 1024 * 1024,
        )

    def nvmlDeviceGetPowerUsage(self, handle):
        return 45000  # milliwatts

    def nvmlSystemGetDefaultSystemDevice(self):
        return self.handle


# ---------- basic helpers ----------

def test_total_to_mb():
    assert monitor.total_to_mb(0) == 0
    assert monitor.total_to_mb(1024 * 1024) == pytest.approx(1.0)
    assert monitor.total_to_mb(5 * 1024 * 1024) == pytest.approx(5.0)


def test_get_hostname(monkeypatch):
    monkeypatch.setattr(monitor.platform, "node", lambda: "DESKTOP-TEST")
    assert monitor.get_hostname() == "DESKTOP-TEST"


def test_get_hostname_failure(monkeypatch):
    def broken():
        raise RuntimeError("no hostname")

    monkeypatch.setattr(monitor.platform, "node", broken)
    assert monitor.get_hostname() == "N/A"


def test_get_uptime_seconds(monkeypatch):
    monkeypatch.setattr(monitor.psutil, "boot_time", lambda: time.time() - 500)
    uptime = monitor.get_uptime_seconds()
    assert uptime is not None
    assert 495 <= uptime <= 600


def test_get_uptime_failure(monkeypatch):
    def broken():
        raise RuntimeError("boot time unavailable")

    monkeypatch.setattr(monitor.psutil, "boot_time", broken)
    assert monitor.get_uptime_seconds() is None


def test_check_internet_connectivity_success(monkeypatch):
    calls = []
    monkeypatch.setattr(
        monitor.socket,
        "create_connection",
        lambda address, timeout=None: calls.append((address, timeout)),
    )
    assert monitor.check_internet_connectivity() is True
    assert calls == [(("8.8.8.8", 53), 3)]


def test_check_internet_connectivity_failure(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(monitor.socket, "create_connection", broken)
    assert monitor.check_internet_connectivity() is False


# ---------- CPU ----------

def test_get_cpu_metrics_without_nvml(monkeypatch):
    monkeypatch.setattr(monitor, "NVML_AVAILABLE", False)
    monkeypatch.setattr(
        monitor.psutil, "cpu_count", lambda logical=True: 6 if logical else 3
    )
    monkeypatch.setattr(monitor.psutil, "cpu_percent", lambda interval=None: 17.5)
    monkeypatch.setattr(
        monitor.psutil, "cpu_freq", lambda: SimpleNamespace(current=3200.0)
    )

    cpu = monitor.get_cpu_metrics()

    assert cpu == {
        "utilization": 17.5,
        "temperature": "N/A",
        "frequency": 3200.0,
        "physical_cores": 3,
        "logical_cores": 6,
    }


def test_get_cpu_metrics_uses_nvml_temperature_when_available(monkeypatch):
    monkeypatch.setattr(monitor, "NVML_AVAILABLE", True)
    monkeypatch.setattr(monitor, "pynvml", FakeNvml())
    monkeypatch.setattr(monitor.psutil, "cpu_count", lambda logical=True: 8)
    monkeypatch.setattr(monitor.psutil, "cpu_percent", lambda interval=None: 5.0)
    monkeypatch.setattr(monitor.psutil, "cpu_freq", lambda: None)

    cpu = monitor.get_cpu_metrics()

    assert cpu["temperature"] == 61
    assert cpu["frequency"] == "N/A"
    assert cpu["logical_cores"] == 8


def test_get_cpu_metrics_frequency_failure(monkeypatch):
    monkeypatch.setattr(monitor, "NVML_AVAILABLE", False)
    monkeypatch.setattr(monitor.psutil, "cpu_count", lambda logical=True: 4)
    monkeypatch.setattr(monitor.psutil, "cpu_percent", lambda interval=None: 1.0)

    def broken():
        raise RuntimeError("frequency unavailable")

    monkeypatch.setattr(monitor.psutil, "cpu_freq", broken)

    cpu = monitor.get_cpu_metrics()
    assert cpu["utilization"] == 1.0
    assert cpu["frequency"] == "N/A"


def test_get_cpu_metrics_total_failure(monkeypatch):
    def broken(logical=True):
        raise RuntimeError("cpu_count failed")

    monkeypatch.setattr(monitor.psutil, "cpu_count", broken)

    cpu = monitor.get_cpu_metrics()
    assert cpu == {
        "utilization": "N/A",
        "temperature": "N/A",
        "frequency": "N/A",
        "physical_cores": "N/A",
        "logical_cores": "N/A",
    }


# ---------- RAM ----------

def test_get_ram_metrics(monkeypatch):
    monkeypatch.setattr(
        monitor.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(
            total=16 * 1024 * 1024 * 1024,
            used=8 * 1024 * 1024 * 1024,
            available=8 * 1024 * 1024 * 1024,
            percent=50.0,
        ),
    )

    ram = monitor.get_ram_metrics()

    assert ram == {
        "total_mb": pytest.approx(16384.0),
        "used_mb": pytest.approx(8192.0),
        "available_mb": pytest.approx(8192.0),
        "utilization_percent": 50.0,
    }


def test_get_ram_metrics_failure(monkeypatch):
    def broken():
        raise RuntimeError("memory unavailable")

    monkeypatch.setattr(monitor.psutil, "virtual_memory", broken)

    assert monitor.get_ram_metrics() == {
        "total_mb": "N/A",
        "used_mb": "N/A",
        "available_mb": "N/A",
        "utilization_percent": "N/A",
    }


# ---------- disks ----------

def test_get_disk_metrics(monkeypatch):
    partition = SimpleNamespace(device="C:\\", mountpoint="C:\\")
    monkeypatch.setattr(
        monitor.psutil, "disk_partitions", lambda all=False: [partition]
    )
    monkeypatch.setattr(
        monitor.psutil,
        "disk_usage",
        lambda mount: SimpleNamespace(
            total=500 * 1024 * 1024,
            used=400 * 1024 * 1024,
            free=100 * 1024 * 1024,
            percent=80.0,
        ),
    )

    disks = monitor.get_disk_metrics()

    assert set(disks) == {"C:\\"}
    assert disks["C:\\"]["total_mb"] == pytest.approx(500.0)
    assert disks["C:\\"]["used_mb"] == pytest.approx(400.0)
    assert disks["C:\\"]["available_mb"] == pytest.approx(100.0)
    assert disks["C:\\"]["utilization_percent"] == 80.0
    assert disks["C:\\"]["temperature"] == "N/A"


def test_get_disk_metrics_marks_failed_partition(monkeypatch):
    good = SimpleNamespace(device="C:\\", mountpoint="C:\\")
    bad = SimpleNamespace(device="Q:\\", mountpoint="Q:\\")
    monkeypatch.setattr(
        monitor.psutil, "disk_partitions", lambda all=False: [good, bad]
    )

    def disk_usage(mount):
        if mount == "Q:\\":
            raise PermissionError("drive not ready")
        return SimpleNamespace(
            total=1000 * 1024 * 1024,
            used=500 * 1024 * 1024,
            free=500 * 1024 * 1024,
            percent=50.0,
        )

    monkeypatch.setattr(monitor.psutil, "disk_usage", disk_usage)

    disks = monitor.get_disk_metrics()

    assert disks["C:\\"]["utilization_percent"] == 50.0
    assert disks["Q:\\"] == {
        "total_mb": "N/A",
        "used_mb": "N/A",
        "available_mb": "N/A",
        "utilization_percent": "N/A",
        "temperature": "N/A",
    }


def test_get_disk_metrics_listing_failure(monkeypatch):
    def broken(all=False):
        raise RuntimeError("partitions unavailable")

    monkeypatch.setattr(monitor.psutil, "disk_partitions", broken)
    assert monitor.get_disk_metrics() == {}


# ---------- GPU ----------

def test_get_gpu_metrics_without_nvml(monkeypatch):
    monkeypatch.setattr(monitor, "NVML_AVAILABLE", False)

    gpu = monitor.get_gpu_metrics()

    assert set(gpu.values()) == {"N/A"}
    assert set(gpu) == {
        "utilization",
        "temperature",
        "frequency",
        "memory_utilization",
        "vram_used_mb",
        "vram_total_mb",
        "power_watts",
    }


def test_get_gpu_metrics_with_nvml(monkeypatch):
    monkeypatch.setattr(monitor, "NVML_AVAILABLE", True)
    monkeypatch.setattr(monitor, "pynvml", FakeNvml())

    gpu = monitor.get_gpu_metrics()

    assert gpu["utilization"] == 42.0
    assert gpu["temperature"] == 61.0
    assert gpu["frequency"] == 1500.0
    assert gpu["vram_total_mb"] == pytest.approx(4096.0)
    assert gpu["vram_used_mb"] == pytest.approx(1024.0)
    assert gpu["memory_utilization"] == pytest.approx(25.0)
    assert gpu["power_watts"] == pytest.approx(45.0)


def test_get_gpu_metrics_no_device(monkeypatch):
    monkeypatch.setattr(monitor, "NVML_AVAILABLE", True)
    monkeypatch.setattr(monitor, "pynvml", FakeNvml(fail_handle=True))

    gpu = monitor.get_gpu_metrics()

    assert set(gpu.values()) == {"N/A"}


def test_get_gpu_metrics_individual_failures_fall_back(monkeypatch):
    monkeypatch.setattr(monitor, "NVML_AVAILABLE", True)

    class BrokenNvml(FakeNvml):
        def nvmlDeviceGetUtilizationRates(self, handle):
            raise RuntimeError("utilization unavailable")

        def nvmlDeviceGetTemperature(self, handle, sensor):
            raise RuntimeError("temperature unavailable")

        def nvmlDeviceGetClockInfo(self, handle, clock_type):
            raise RuntimeError("clock unavailable")

        def nvmlDeviceGetMemoryInfo(self, handle):
            raise RuntimeError("memory unavailable")

        def nvmlDeviceGetPowerUsage(self, handle):
            raise RuntimeError("power unavailable")

    monkeypatch.setattr(monitor, "pynvml", BrokenNvml())

    gpu = monitor.get_gpu_metrics()

    assert set(gpu.values()) == {"N/A"}


def test_get_gpu_metrics_total_failure(monkeypatch):
    monkeypatch.setattr(monitor, "NVML_AVAILABLE", True)

    class ExplodingNvml:
        NVML_TEMPERATURE_GPU = 0
        NVML_CLOCK_GRAPHICS = 0

        def nvmlDeviceGetHandleByIndex(self, index):
            raise RuntimeError("boom")

        def nvmlDeviceGetCount(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(monitor, "pynvml", ExplodingNvml())

    assert set(monitor.get_gpu_metrics().values()) == {"N/A"}


# ---------- aggregation ----------

def test_get_system_info(monkeypatch):
    monkeypatch.setattr(monitor.platform, "node", lambda: "PC")
    monkeypatch.setattr(monitor.platform, "platform", lambda: "Windows-11")
    monkeypatch.setattr(monitor.platform, "python_version", lambda: "3.12.0")

    info = monitor.get_system_info()

    assert info == {
        "hostname": "PC",
        "platform": "Windows-11",
        "python_version": "3.12.0",
    }


def test_get_system_info_failure(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("platform unavailable")

    monkeypatch.setattr(monitor.platform, "platform", broken)

    assert monitor.get_system_info() == {
        "hostname": "N/A",
        "platform": "N/A",
        "python_version": "N/A",
    }


def test_get_all_metrics_assembles_sections(monkeypatch):
    cpu = {"utilization": 1.0}
    ram = {"utilization_percent": 2.0}
    disks = {"C:\\": {"utilization_percent": 3.0}}
    gpu = {"utilization": 4.0}
    system = {"hostname": "TEST-PC", "platform": "p", "python_version": "v"}

    monkeypatch.setattr(monitor, "get_cpu_metrics", lambda: cpu)
    monkeypatch.setattr(monitor, "get_ram_metrics", lambda: ram)
    monkeypatch.setattr(monitor, "get_disk_metrics", lambda: disks)
    monkeypatch.setattr(monitor, "get_gpu_metrics", lambda: gpu)
    monkeypatch.setattr(monitor, "get_system_info", lambda: system)
    monkeypatch.setattr(monitor, "check_internet_connectivity", lambda: True)
    monkeypatch.setattr(monitor, "get_uptime_seconds", lambda: 123.0)

    metrics = monitor.get_all_metrics()

    assert metrics == {
        "hostname": "TEST-PC",
        "uptime": 123.0,
        "internet": True,
        "cpu": cpu,
        "ram": ram,
        "disk": disks,
        "gpu": gpu,
    }


def test_get_all_metrics_unexpected_failure_returns_minimal_structure(monkeypatch):
    def broken():
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(monitor, "get_cpu_metrics", broken)

    metrics = monitor.get_all_metrics()

    assert metrics["hostname"] == "N/A"
    assert metrics["uptime"] is None
    assert metrics["internet"] is False
    assert metrics["disk"] == {}
    assert set(metrics["cpu"].values()) == {"N/A"}
    assert set(metrics["ram"].values()) == {"N/A"}
    assert set(metrics["gpu"].values()) == {"N/A"}