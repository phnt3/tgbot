"""Tests for the file browser helpers.

All filesystem work happens inside ``tmp_path``; the 45 MB download cap is
checked without allocating a large file.
"""

import subprocess

import pytest

import file_manager
from file_manager import (
    copy_path,
    delete_path,
    format_size,
    list_directory,
    list_drives,
    make_directory,
    move_path,
    read_for_send,
    rename_path,
    sanitize_filename,
    save_upload,
    search_files,
    validate_name,
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
    with pytest.raises(ValueError):
        search_files(tmp_path, "   ")
    with pytest.raises(ValueError):
        search_files(tmp_path, "x" * 121)

    file_path = tmp_path / "file.txt"
    file_path.write_text("x")
    with pytest.raises(NotADirectoryError):
        search_files(file_path, "x")


# --- Drives and file operations --------------------------------------------


def test_list_drives_returns_mount_information():
    drives = list_drives()

    assert isinstance(drives, list)
    for item in drives:
        assert item["name"]
        assert item["path"].exists()
        assert item["total"] >= 0
        assert item["free"] >= 0
        assert 0.0 <= item["percent"] <= 100.0


def test_validate_name_accepts_normal_names():
    assert validate_name("notes.txt") == "notes.txt"
    assert validate_name("  My Folder  ") == "My Folder"
    # Surrounding whitespace is trimmed, matching what Explorer accepts.
    assert validate_name("report.txt ") == "report.txt"
    assert validate_name("console.txt") == "console.txt"
    assert validate_name("COM10.log") == "COM10.log"


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        ".",
        "..",
        "bad/name",
        "bad\\name",
        'bad:name',
        "bad?name",
        "trailing.",
        "CON",
        "con.txt",
        "LPT1",
        "x" * 151,
    ],
)
def test_validate_name_rejects_bad_names(name):
    with pytest.raises(ValueError):
        validate_name(name)


def test_make_directory_creates_folder_and_refuses_duplicates(tmp_path):
    created = make_directory(tmp_path, "New Folder")

    assert created == tmp_path / "New Folder"
    assert created.is_dir()

    with pytest.raises(FileExistsError):
        make_directory(tmp_path, "New Folder")

    file_path = tmp_path / "not-a-folder.txt"
    file_path.write_text("x")
    with pytest.raises(NotADirectoryError):
        make_directory(file_path, "child")


def test_make_directory_refuses_protected_folder(tmp_path, monkeypatch):
    protected = tmp_path / "Windows"
    protected.mkdir()
    monkeypatch.setattr(file_manager, "_critical_roots", lambda: [protected])

    with pytest.raises(PermissionError):
        make_directory(protected, "new")


def test_drive_root_is_protected():
    from pathlib import Path

    assert "Drive roots" in file_manager._modify_problem(Path("C:\\"))


def test_rename_path_moves_file_and_refuses_overwrite(tmp_path):
    source = tmp_path / "old.txt"
    source.write_text("content")

    target = rename_path(source, "new.txt")
    assert target == tmp_path / "new.txt"
    assert target.read_text() == "content"
    assert not source.exists()

    other = tmp_path / "other.txt"
    other.write_text("x")
    with pytest.raises(FileExistsError):
        rename_path(target, "other.txt")

    with pytest.raises(FileNotFoundError):
        rename_path(tmp_path / "missing.txt", "x.txt")


def test_rename_path_refuses_invalid_and_protected(tmp_path, monkeypatch):
    source = tmp_path / "old.txt"
    source.write_text("x")

    with pytest.raises(ValueError):
        rename_path(source, "bad/name")

    protected = tmp_path / "Windows"
    protected.mkdir()
    inside = protected / "file.txt"
    inside.write_text("x")
    monkeypatch.setattr(file_manager, "_critical_roots", lambda: [protected])

    with pytest.raises(PermissionError):
        rename_path(inside, "renamed.txt")


def test_copy_path_copies_file_and_folder(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("a")
    (source / "nested").mkdir()
    (source / "nested" / "b.txt").write_text("b")
    dest = tmp_path / "dest"
    dest.mkdir()

    file_target = copy_path(source / "a.txt", dest)
    assert file_target.read_text() == "a"

    folder_target = copy_path(source, dest)
    assert (folder_target / "a.txt").read_text() == "a"
    assert (folder_target / "nested" / "b.txt").read_text() == "b"
    assert source.exists()

    with pytest.raises(FileExistsError):
        copy_path(source / "a.txt", dest)


def test_copy_path_refuses_folder_into_itself(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    nested = source / "nested"
    nested.mkdir()

    with pytest.raises(ValueError):
        copy_path(source, nested)


def test_copy_path_allows_protected_source_but_not_destination(
    tmp_path, monkeypatch
):
    protected = tmp_path / "Windows"
    protected.mkdir()
    source = protected / "file.txt"
    source.write_text("x")
    dest = tmp_path / "dest"
    dest.mkdir()
    monkeypatch.setattr(file_manager, "_critical_roots", lambda: [protected])

    copied = copy_path(source, dest)
    assert copied.read_text() == "x"

    with pytest.raises(PermissionError):
        copy_path(source, protected)


def test_copy_tree_skips_reparse_points(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.mkdir()
    (source / "real.txt").write_text("real")
    linked = source / "linked"
    linked.mkdir()
    (linked / "secret.txt").write_text("secret")
    dest = tmp_path / "dest"
    dest.mkdir()

    original = file_manager._is_reparse_point
    monkeypatch.setattr(
        file_manager,
        "_is_reparse_point",
        lambda entry: entry.name == "linked" or original(entry),
    )

    target = copy_path(source, dest)

    assert (target / "real.txt").read_text() == "real"
    assert not (target / "linked").exists()


def test_copy_path_refuses_reparse_source(tmp_path, monkeypatch):
    source = tmp_path / "linked"
    source.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    monkeypatch.setattr(file_manager, "_is_reparse_path", lambda path: True)

    with pytest.raises(PermissionError):
        copy_path(source, dest)


def test_move_path_moves_file_and_folder(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("a")
    dest = tmp_path / "dest"
    dest.mkdir()

    moved_file = move_path(source / "a.txt", dest)
    assert moved_file.read_text() == "a"
    assert not (source / "a.txt").exists()

    (source / "b.txt").write_text("b")
    moved_folder = move_path(source, dest)
    assert (moved_folder / "b.txt").read_text() == "b"
    assert not source.exists()


def test_move_path_refuses_protected_source(tmp_path, monkeypatch):
    protected = tmp_path / "Windows"
    protected.mkdir()
    source = protected / "file.txt"
    source.write_text("x")
    dest = tmp_path / "dest"
    dest.mkdir()
    monkeypatch.setattr(file_manager, "_critical_roots", lambda: [protected])

    with pytest.raises(PermissionError):
        move_path(source, dest)


def test_move_path_cross_drive_copies_then_deletes(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("a")
    dest = tmp_path / "dest"
    dest.mkdir()
    monkeypatch.setattr(file_manager, "_same_drive", lambda *args: False)

    moved = move_path(source, dest)

    assert (moved / "a.txt").read_text() == "a"
    assert not source.exists()


def test_move_path_cross_drive_reports_partial_delete(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("a")
    dest = tmp_path / "dest"
    dest.mkdir()
    monkeypatch.setattr(file_manager, "_same_drive", lambda *args: False)
    monkeypatch.setattr(
        file_manager,
        "delete_path",
        lambda path: {"deleted": 0, "bytes": 0, "errors": 1, "skipped": 0},
    )

    with pytest.raises(OSError):
        move_path(source, dest)
    assert (dest / "src" / "a.txt").read_text() == "a"


def test_delete_path_removes_file_and_tree(tmp_path):
    file_path = tmp_path / "file.txt"
    file_path.write_text("hello")
    stats = delete_path(file_path)
    assert not file_path.exists()
    assert stats["deleted"] == 1
    assert stats["bytes"] == 5

    folder = tmp_path / "folder"
    (folder / "nested").mkdir(parents=True)
    (folder / "a.txt").write_text("a")
    (folder / "nested" / "b.txt").write_text("bb")

    stats = delete_path(folder)
    assert not folder.exists()
    assert stats["deleted"] == 4
    assert stats["errors"] == 0


def test_delete_path_refuses_protected_and_missing(tmp_path, monkeypatch):
    protected = tmp_path / "Windows"
    protected.mkdir()
    source = protected / "file.txt"
    source.write_text("x")
    monkeypatch.setattr(file_manager, "_critical_roots", lambda: [protected])

    with pytest.raises(PermissionError):
        delete_path(source)

    with pytest.raises(FileNotFoundError):
        delete_path(tmp_path / "missing.txt")


def test_delete_path_does_not_follow_junctions(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "secret.txt").write_text("secret")
    root = tmp_path / "root"
    root.mkdir()
    (root / "keep.txt").write_text("x")
    link = root / "linked"
    try:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("cannot create a junction on this system")

    stats = delete_path(root)

    assert not root.exists()
    assert stats["deleted"] == 3
    assert stats["errors"] == 0
    assert (target / "secret.txt").read_text() == "secret"