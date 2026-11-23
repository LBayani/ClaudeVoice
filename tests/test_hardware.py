import copy
import json

import pytest

from voice_server import hardware


def test_default_profile_is_valid():
    hw = hardware.validate(copy.deepcopy(hardware.DEFAULT))
    assert hw["power"]["latch"] == 2


def test_latch_pin_cannot_be_reused():
    hw = copy.deepcopy(hardware.DEFAULT)
    hw["outputs"] = [{"pin": 2, "type": "led", "name": "LED"}]
    with pytest.raises(ValueError, match="питание"):
        hardware.validate(hw)


def test_double_assignment():
    hw = copy.deepcopy(hardware.DEFAULT)
    hw["outputs"] = [{"pin": 40, "type": "led", "name": "A"}, {"pin": 40, "type": "relay", "name": "B"}]
    with pytest.raises(ValueError, match="дважды"):
        hardware.validate(hw)


def test_reserved_and_missing_gpio():
    hw = copy.deepcopy(hardware.DEFAULT)
    hw["outputs"] = [{"pin": 27, "type": "led", "name": "A"}]
    with pytest.raises(ValueError):
        hardware.validate(hw)
    hw["outputs"] = [{"pin": 60, "type": "led", "name": "A"}]
    with pytest.raises(ValueError):
        hardware.validate(hw)


def test_strapping_warning():
    hw = copy.deepcopy(hardware.DEFAULT)
    hw["outputs"] = [{"pin": 46, "type": "led", "name": "Свет"}]
    w = hardware.warnings(hardware.validate(hw))
    assert any("GPIO46" in x for x in w)


def test_c3_has_no_touch():
    hw = copy.deepcopy(hardware.BLANK)
    hw["target"] = "esp32c3"
    hw["inputs"] = [{"pin": 4, "type": "touch", "name": "Ус"}]
    with pytest.raises(ValueError):
        hardware.validate(hw)


def test_load_broken_json_and_atomic_save(tmp_path, monkeypatch):
    monkeypatch.setattr(hardware, "DEVICES_DIR", tmp_path)
    monkeypatch.setattr(hardware, "FILE", tmp_path / "none.json")
    dev = "aa:bb:cc:dd:ee:ff"
    hardware.device_file(dev).write_text("{broken", encoding="utf-8")
    assert hardware.load(dev) is None
    hardware.save(copy.deepcopy(hardware.DEFAULT), dev)
    assert hardware.load(dev)["power"]["latch"] == 2
    assert json.loads(hardware.device_file(dev).read_text(encoding="utf-8"))["board"]
    assert hardware.load("11:22:33:44:55:66") is None
