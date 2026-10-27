"""Tests for disk analysis and safe cleanup.

Cleanup tests run against temporary directories only: no real system folder is
ever touched and the Recycle Bin / PowerShell paths are monkeypatched.
"""

import disk_cleanup
from disk_cleanup import clean_targets, get_disk_overview, human_size, scan_cleanup_targets


class FakePartition:
    def __init__(self, mountpoint="C:\\", fstype="NTFS", opts="rw,fixed"):
        self.device = mountpoint
        self.mountpoint = mountpoint
        self.fstype = fstype
        self.opts = opts


class FakeUsage:
    def __init__(self, total=100, used=80, free=20, percent=80.0):
        self.total = total
        self.used = used
        self.free = free
        self.percent = percent


def test_human_size_uses_binary_units():
    assert human_size(0) == "0 B"
    assert human_size(None) == "0 B"
    assert human_size(512) == "512 B"
    assert human_size(1024) == "1.0 KB"
    assert human_size(5 * 1024 ** 3) == "5.0 GB"


def test_get_disk_overview_skips_cdrom_and_broken_disks(monkeypatch):
    def fake_usage(mountpoint):
        if mountpoint == "D:\\":
            raise OSError("device not ready")
        return FakeUsage()

    monkeypatch.setattr(
        disk_cleanup.psutil,
        "disk_partitions",
        lambda all=False: [
            FakePartition("C:\\"),
            FakePartition("D:\\"),
            FakePartition("E:\\", opts="rw,cdrom"),
            FakePartition("F:\\", fstype=""),
        ],
    )
    monkeypatch.setattr(disk_cleanup.psutil, "disk_usage", fake_usage)

    disks = get_disk_overview()

    assert [disk["mountpoint"] for disk in disks] == ["C:\\"]
    assert disks[0]["free_bytes"] == 20
    assert disks[0]["percent"] == 80.0


def test_scan_cleanup_targets_measures_and_sorts(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    small = tmp_path / "temp" / "small"
    small.mkdir(parents=True)
    (small / "a.txt").write_bytes(b"x" * 10)

    big = tmp_path / "other"
    big.mkdir()
    (big / "b.bin").write_bytes(b"y" * 500)

    targets = [
        {"name": "Small", "path": small, "cleanable": True},
        {"name": "Big", "path": big, "cleanable": False},
        {"name": "Missing", "path": tmp_path / "nope", "cleanable": True},
    ]

    scanned = scan_cleanup_targets(targets, budget_seconds=5)

    assert [item["name"] for item in scanned] == ["Big", "Small", "Missing"]
    assert scanned[0]["size_bytes"] == 500
    assert scanned[0]["cleanable"] is False
    assert scanned[1]["files"] == 1
    assert scanned[2]["exists"] is False


def test_clean_targets_removes_contents_but_keeps_folder(tmp_path):
    folder = tmp_path / "cache"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    (nested / "one.tmp").write_bytes(b"a" * 100)
    (folder / "two.tmp").write_bytes(b"b" * 50)

    result = clean_targets(
        [
            {"name": "Cache", "path": folder, "cleanable": True},
            {"name": "Update", "path": folder / "wu", "cleanable": False},
            {"name": "Gone", "path": tmp_path / "gone", "cleanable": True},
        ]
    )

    assert result["freed_bytes"] == 150
    assert result["removed_files"] == 2
    assert result["errors"] == 0
    assert folder.is_dir()
    assert not any(folder.iterdir())
    assert "Cache" in result["cleaned"]
    assert any("report only" in item for item in result["skipped"])
    assert any("not found" in item for item in result["skipped"])


def test_clean_targets_does_not_follow_directory_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_bytes(b"do not delete")

    cache = tmp_path / "cache"
    cache.mkdir()
    link = cache / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        import pytest

        pytest.skip("symlinks are not available on this account")

    result = clean_targets([{"name": "Cache", "path": cache, "cleanable": True}])

    assert keep.exists()
    assert result["errors"] == 0
    assert not link.exists()


def test_empty_recycle_bin_non_windows(monkeypatch):
    monkeypatch.setattr(disk_cleanup.os, "name", "posix")
    ok, message = disk_cleanup.empty_recycle_bin()
    assert ok is False
    assert "only available on Windows" in message


def test_get_recycle_bin_size_handles_powershell_output(monkeypatch):
    class Result:
        stdout = "  1234.0 \n"

    monkeypatch.setattr(disk_cleanup.os, "name", "nt")
    monkeypatch.setattr(
        disk_cleanup.subprocess, "run", lambda *a, **k: Result()
    )
    assert disk_cleanup.get_recycle_bin_size() == 1234