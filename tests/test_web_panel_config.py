"""Tests for ``web.panel_config``.

The panel configuration is independent from ``.env`` and lives in a file that
is created on first use. Tests always pass an explicit temporary path.
"""

import json

import pytest

from web.panel_config import (
    DEFAULT_CONFIG,
    PanelConfigError,
    load_panel_config,
    panel_urls,
    sanitize_config,
    save_panel_config,
)


def test_sanitize_defaults_for_empty_input():
    assert sanitize_config(None) == DEFAULT_CONFIG
    assert sanitize_config({}) == DEFAULT_CONFIG
    assert sanitize_config("nonsense") == DEFAULT_CONFIG


def test_sanitize_aliases_and_types():
    config = sanitize_config({"host": "localhost", "port": "8091", "enabled": "no"})
    assert config == {"host": "127.0.0.1", "port": 8091, "enabled": False}


def test_sanitize_wildcard_aliases():
    assert sanitize_config({"host": "*"})["host"] == "0.0.0.0"
    assert sanitize_config({"host": "all"})["host"] == "0.0.0.0"


def test_sanitize_keeps_a_specific_lan_address():
    assert sanitize_config({"host": "192.168.1.20"})["host"] == "192.168.1.20"


@pytest.mark.parametrize("host", ["", "  ", "example.com", "999.1.1.1"])
def test_sanitize_rejects_bad_host(host):
    with pytest.raises(PanelConfigError):
        sanitize_config({"host": host})


@pytest.mark.parametrize("port", [0, 65536, -1, "abc", None])
def test_sanitize_rejects_bad_port(port):
    with pytest.raises(PanelConfigError):
        sanitize_config({"port": port})


def test_load_creates_default_config(tmp_path):
    path = tmp_path / "web_panel.json"
    config = load_panel_config(path)

    assert config == DEFAULT_CONFIG
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == DEFAULT_CONFIG


def test_load_reads_saved_values(tmp_path):
    path = tmp_path / "web_panel.json"
    save_panel_config(
        {"host": "127.0.0.1", "port": 9000, "enabled": True}, path
    )

    assert load_panel_config(path) == {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 9000,
    }


def test_load_falls_back_on_corrupt_json(tmp_path):
    path = tmp_path / "web_panel.json"
    path.write_text("{broken", encoding="utf-8")

    assert load_panel_config(path) == DEFAULT_CONFIG


def test_load_falls_back_on_invalid_values(tmp_path):
    path = tmp_path / "web_panel.json"
    path.write_text(
        json.dumps({"host": "bad host", "port": 8090}), encoding="utf-8"
    )

    assert load_panel_config(path) == DEFAULT_CONFIG


def test_save_creates_parents_and_writes_atomically(tmp_path):
    path = tmp_path / "nested" / "web_panel.json"
    saved = save_panel_config({"host": "192.168.1.5", "port": 8090}, path)

    assert saved["host"] == "192.168.1.5"
    assert json.loads(path.read_text(encoding="utf-8"))["host"] == "192.168.1.5"
    assert not (path.parent / "web_panel.json.tmp").exists()


def test_save_rejects_invalid_port(tmp_path):
    with pytest.raises(PanelConfigError):
        save_panel_config({"port": 70000}, tmp_path / "web_panel.json")


def test_panel_urls_for_wildcard_bind():
    urls = panel_urls("0.0.0.0", 8090)

    assert urls["local"] == "http://127.0.0.1:8090/"
    assert "<LAN-IP>" in urls["lan"]


def test_panel_urls_for_specific_host():
    urls = panel_urls("192.168.1.5", 8090)

    assert urls == {
        "local": "http://192.168.1.5:8090/",
        "lan": "http://192.168.1.5:8090/",
    }