"""Tests for the file browser helpers.

All filesystem work happens inside ``tmp_path``; the 45 MB download cap is
checked without allocating a large file.
"""

import file_manager
from file_manager import (
    format_size,
    list_directory,
    read_for_send,
    sanitize_filename,
    save_upload,
    search_files,
)


def test_format_size_units():
    assert format_size(0) == "0 B"
    assert format_size(1024) == "1.0 KB"
    assert format_size(1536) == "1.5 KB"
    assert format_size(2 * 1024 ** 2) == "2.0 MB"


def test_list_directory_dirs_first_and_sorted(tmp_path):
    (tmp_path / "b.txt").write_bytes(b"22")
    (tmp_path / "a.txt").write_bytes(b"1")
    (tmp_path / "Zeta").mkdir()
    (tmp_path / "alpha").mkdir()

    listing = list_directory(tmp_path)

    assert listing["path"] == tmp_path
    assert listing["parent"] == tmp_path.parent
    assert [entry["name"] for entry in listing["entries"]] == [
        "alpha",
        "Zeta",
        "a.txt",
        "b.txt",
    ]
    assert listing["entries"][0]["is_dir"] is True
    assert listing["entries"][2]["size"] == 1


def test_list_directory_rejects_files(tmp_path):
    file_path = tmp_path / "file.txt"
    file_path.write_text("x")

    import pytest

    with pytest.raises(NotADirectoryError):
        list_directory(file_path)


def test_read_for_send_enforces_size_limit(tmp_path, monkeypatch):
    small = tmp_path / "small.bin"
    small.write_bytes(b"hello")

    data, name = read_for_send(small)
    assert data == b"hello"
    assert name == "small.bin"

    import pytest

    monkeypatch.setattr(file_manager, "MAX_SEND_BYTES", 2)
    with pytest.raises(ValueError):
        read_for_send(small)


def test_sanitize_filename_strips_paths_and_reserved_names():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("C:\\Windows\\evil.exe") == "evil.exe"
    assert sanitize_filename("bad:name?.txt") == "bad_name_.txt"
    assert sanitize_filename("") == "upload.bin"
    assert sanitize_filename("CON") == "_CON"


def test_save_upload_creates_folder_and_never_overwrites(tmp_path):
    dest = tmp_path / "uploads"

    first = save_upload("report.txt", b"one", dest)
    second = save_upload("report.txt", b"two", dest)

    assert first == dest / "report.txt"
    assert second == dest / "report_1.txt"
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"


def test_default_upload_dir_uses_data_dir():
    import config

    expected = config.config["DATA_DIR"] / "uploads"
    assert file_manager.default_upload_dir() == expected


# --- Recursive search ------------------------------------------------------


def test_search_finds_substring_case_insensitively(tmp_path):
    (tmp_path / "Movies").mkdir()
    (tmp_path / "Movies" / "Holiday_Clip.MP4").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("x")

    found = search_files(tmp_path, "holiday")

    assert [item["name"] for item in found["results"]] == ["Holiday_Clip.MP4"]
    assert found["results"][0]["is_dir"] is False
    assert found["results"][0]["size"] == 1
    assert found["truncated"] is False


def test_search_matches_folders_and_wildcards(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "clip1.mp4").write_bytes(b"123")
    (tmp_path / "clip2.txt").write_text("x")

    folders = search_files(tmp_path, "ALP")
    files = search_files(tmp_path, "*.mp4")

    assert [item["name"] for item in folders["results"]] == ["alpha"]
    assert folders["results"][0]["is_dir"] is True
    assert [item["name"] for item in files["results"]] == ["clip1.mp4"]


def test_search_respects_depth_limit(tmp_path):
    (tmp_path / "top.txt").write_text("x")
    (tmp_path / "one").mkdir()
    (tmp_path / "one" / "mid.txt").write_text("x")
    (tmp_path / "one" / "two").mkdir()
    (tmp_path / "one" / "two" / "deep.txt").write_text("x")

    shallow = search_files(tmp_path, "txt", max_depth=0)
    one_level = search_files(tmp_path, "txt", max_depth=1)
    deep = search_files(tmp_path, "txt", max_depth=2)

    assert [item["name"] for item in shallow["results"]] == ["top.txt"]
    assert {item["name"] for item in one_level["results"]} == {
        "top.txt",
        "mid.txt",
    }
    assert {item["name"] for item in deep["results"]} == {
        "top.txt",
        "mid.txt",
        "deep.txt",
    }


def test_search_stops_at_result_limit(tmp_path):
    for index in range(10):
        (tmp_path / f"match{index}.txt").write_text("x")

    found = search_files(tmp_path, "match", max_results=3)

    assert len(found["results"]) == 3
    assert found["truncated"] is True


def test_search_stops_at_directory_limit(tmp_path):
    for index in range(5):
        (tmp_path / f"dir{index}").mkdir()
    (tmp_path / "dir0" / "target.txt").write_text("x")

    found = search_files(tmp_path, "target", max_dirs=1)

    assert found["results"] == []
    assert found["scanned_dirs"] == 1
    assert found["truncated"] is True


def test_search_does_not_descend_into_reparse_points(tmp_path, monkeypatch):
    nested = tmp_path / "linked"
    nested.mkdir()
    (nested / "hidden_target.txt").write_text("x")

    monkeypatch.setattr(file_manager, "_is_reparse_point", lambda entry: True)

    found = search_files(tmp_path, "hidden_target")

    assert found["results"] == []


def test_search_rejects_bad_input(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        search_files(tmp_path, "   ")
    with pytest.raises(ValueError):
        search_files(tmp_path, "x" * 121)

    file_path = tmp_path / "file.txt"
    file_path.write_text("x")
    with pytest.raises(NotADirectoryError):
        search_files(file_path, "x")