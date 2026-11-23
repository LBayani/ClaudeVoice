import json

import pytest

from voice_server import config


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    return config.Config()


def test_apply_and_persist(cfg):
    config.apply_settings(cfg, {"music": {"volume": 0.3}, "server": {"port": 9000}})
    assert cfg.music.volume == 0.3 and cfg.server.port == 9000
    saved = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
    assert saved["music"]["volume"] == 0.3


@pytest.mark.parametrize("bad", [
    {"nope": 1},
    {"server": {"port": "8765"}},
    {"server": {"port": 70000}},
    {"server": {"port": 1.5}},
    {"music": {"volume": 3}},
    {"music": {"enabled": "yes"}},
    {"claude": {"allowed_tools": "Bash"}},
    {"claude": "x"},
    {"ollama": {"temperature": True}},
])
def test_rejects_bad_values_and_touches_nothing(cfg, bad):
    before = config.as_dict(cfg)
    with pytest.raises((KeyError, TypeError, ValueError)):
        config.apply_settings(cfg, bad)
    assert config.as_dict(cfg) == before
    assert not config.SETTINGS_FILE.exists()


def test_int_for_float_and_object_fields(cfg):
    config.apply_settings(cfg, {"ollama": {"temperature": 1}, "skin_palette": ["#000", "#fff"]})
    assert cfg.ollama.temperature == 1.0 and isinstance(cfg.ollama.temperature, float)
    assert cfg.skin_palette == ["#000", "#fff"]


def test_atomic_write(tmp_path):
    p = tmp_path / "a.json"
    config.atomic_write_text(p, "1")
    config.atomic_write_text(p, "2")
    assert p.read_text() == "2"
    assert [x.name for x in tmp_path.iterdir()] == ["a.json"]


def test_default_paths_keep_backslashes():
    assert config.OllamaCfg().executable == "%LOCALAPPDATA%\\Programs\\Ollama\\ollama.exe"
