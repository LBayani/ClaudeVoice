"""Hardware profile of the device: display, pins, antennae / buttons and their actions, outputs, camera.

Stored in hardware.json next to config.yaml, editable in the settings app. The device downloads it
from /device/hardware.json at boot (and when asked via its MCP tool `self.hw.reload`), keeps a copy
in NVS and applies it. Pin and display changes need a reboot (the device does it by itself);
input actions apply at once.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .config import ROOT

FILE = ROOT / "hardware.json"

# Display drivers the firmware knows. "oled" drivers are monochrome over I2C: skins become 1-bit.
DRIVERS = {
    "st7789": {"bus": "spi", "label": "ST7789 (цветной SPI)"},
    "gc9a01": {"bus": "spi", "label": "GC9A01 (круглый SPI)"},
    "ili9341": {"bus": "spi", "label": "ILI9341 (цветной SPI)"},
    "st7796": {"bus": "spi", "label": "ST7796 (цветной SPI)"},
    "ssd1306": {"bus": "i2c", "label": "SSD1306 (OLED I2C, монохромный)"},
    "sh1106": {"bus": "i2c", "label": "SH1106 (OLED I2C, монохромный)"},
    "none": {"bus": "", "label": "Без дисплея"},
}

# Ready-made display presets: only the fields that differ from the driver defaults.
DISPLAY_PRESETS = {
    "st7789_240x240": {"label": "ST7789 1.3–1.54\" 240×240", "driver": "st7789", "width": 240, "height": 240,
                       "offset_x": 0, "offset_y": 0, "invert": True},
    "st7789_240x280": {"label": "ST7789 1.69\" 240×280", "driver": "st7789", "width": 240, "height": 280,
                       "offset_x": 0, "offset_y": 20, "invert": True},
    "st7789_240x320": {"label": "ST7789 2.0\" 240×320", "driver": "st7789", "width": 240, "height": 320,
                       "offset_x": 0, "offset_y": 0, "invert": True},
    "st7789_320x240": {"label": "ST7789 2.0\" 320×240 (альбомный)", "driver": "st7789", "width": 320, "height": 240,
                       "offset_x": 0, "offset_y": 0, "invert": True, "swap_xy": True, "mirror_x": True},
    "st7789_135x240": {"label": "ST7789 1.14\" 135×240", "driver": "st7789", "width": 135, "height": 240,
                       "offset_x": 52, "offset_y": 40, "invert": True},
    "st7789_172x320": {"label": "ST7789 1.47\" 172×320", "driver": "st7789", "width": 172, "height": 320,
                       "offset_x": 34, "offset_y": 0, "invert": True},
    "gc9a01_240": {"label": "GC9A01 1.28\" круглый 240×240", "driver": "gc9a01", "width": 240, "height": 240,
                   "offset_x": 0, "offset_y": 0, "invert": True, "rgb_order": "bgr", "mirror_x": True},
    "ili9341_320x240": {"label": "ILI9341 2.4–2.8\" 320×240", "driver": "ili9341", "width": 320, "height": 240,
                        "offset_x": 0, "offset_y": 0, "invert": False, "rgb_order": "bgr", "swap_xy": True,
                        "mirror_x": True, "pclk_mhz": 40},
    "st7796_480x320": {"label": "ST7796 3.5\" 480×320", "driver": "st7796", "width": 480, "height": 320,
                       "offset_x": 0, "offset_y": 0, "invert": False, "rgb_order": "bgr", "swap_xy": True,
                       "pclk_mhz": 40},
    "ssd1306_128x64": {"label": "OLED SSD1306 0.96\" 128×64", "driver": "ssd1306", "width": 128, "height": 64},
    "ssd1306_128x32": {"label": "OLED SSD1306 0.91\" 128×32", "driver": "ssd1306", "width": 128, "height": 32},
    "sh1106_128x64": {"label": "OLED SH1106 1.3\" 128×64", "driver": "sh1106", "width": 128, "height": 64},
}

# What a touch antenna or a button can do. "event" tells the assistant, which decides what to do.
ACTIONS = {
    "none": "Ничего",
    "affection": "Ласка (награда, радость)",
    "wake": "Разбудить / начать разговор",
    "listen": "Слушать, пока держу (рация)",
    "stop": "Остановить речь",
    "event": "Сообщить ассистенту",
    "vibrate": "Вибрация",
    "volume_up": "Громче",
    "volume_down": "Тише",
    "emotion:happy": "Эмоция: радость",
    "emotion:surprised": "Эмоция: удивление",
    "emotion:angry": "Эмоция: злость",
    "emotion:sad": "Эмоция: грусть",
    "emotion:sleepy": "Эмоция: сонный",
    "sound:popup": "Звук: щелчок",
    "sound:success": "Звук: успех",
    "sound:vibration": "Звук: жужжание",
    "sound:exclamation": "Звук: внимание",
}
GESTURES = ("tap", "double", "hold")
INPUT_TYPES = ("touch", "button")
OUTPUT_TYPES = {"vibration": "Вибромотор", "led": "Светодиод", "buzzer": "Пищалка", "relay": "Реле"}
CAMERA_TYPES = {"none": "Нет", "board": "Модуль на плате (OV2640/OV3660)",
                "esp32cam": "ESP32-CAM по Wi-Fi", "pc": "Камера ПК / IP-камера"}

# ESP32-S3 touch-capable GPIOs (touch sensor channels).
TOUCH_PINS = set(range(1, 15))

# The board this project ships with: zhengchen 1.54" TFT Wi-Fi (ESP32-S3, octal PSRAM).
DEFAULT = {
    "board": "zhengchen-1.54tft-wifi",
    "display": {"preset": "st7789_240x240", "driver": "st7789", "width": 240, "height": 240,
                "offset_x": 0, "offset_y": 0, "mirror_x": False, "mirror_y": False, "swap_xy": False,
                "invert": True, "rgb_order": "rgb", "spi_mode": 3, "pclk_mhz": 80,
                "pins": {"mosi": 41, "sclk": 42, "cs": 21, "dc": 40, "rst": 45, "bl": 20, "sda": -1, "scl": -1},
                "bl_invert": False, "i2c_addr": 60},
    "audio": {"mic_ws": 4, "mic_sck": 5, "mic_din": 6, "spk_dout": 7, "spk_bclk": 15, "spk_lrck": 16},
    "buttons": {"boot": 0, "volume_up": 10, "volume_down": 39},
    "power": {"latch": 2, "charge_detect": 9},
    "inputs": [
        {"pin": 11, "type": "touch", "name": "Левая антенна", "sensitivity": 4,
         "tap": "affection", "double": "affection", "hold": "affection"},
        {"pin": 12, "type": "touch", "name": "Правая антенна", "sensitivity": 4,
         "tap": "affection", "double": "affection", "hold": "affection"},
    ],
    "outputs": [],
    "camera": {"type": "none", "url": "", "pc_device": "",
               "pins": {"d0": -1, "d1": -1, "d2": -1, "d3": -1, "d4": -1, "d5": -1, "d6": -1, "d7": -1,
                        "xclk": -1, "pclk": -1, "vsync": -1, "href": -1, "sda": -1, "scl": -1,
                        "pwdn": -1, "reset": -1}},
}

# Pins nobody may reuse on this module: flash / octal PSRAM (26-37) and USB (19, 20 is backlight here).
RESERVED = set(range(26, 38))


def load() -> dict:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return copy.deepcopy(DEFAULT)
    return _merge(copy.deepcopy(DEFAULT), data)


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _pin(v, what: str) -> int:
    try:
        p = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: номер пина должен быть числом") from None
    if p == -1:
        return -1
    if not 0 <= p <= 48:
        raise ValueError(f"{what}: GPIO{p} не существует у ESP32-S3 (0–48)")
    if p in RESERVED:
        raise ValueError(f"{what}: GPIO{p} занят флеш-памятью/PSRAM")
    return p


def validate(hw: dict) -> dict:
    """Checks types, ranges and pin conflicts. Returns the cleaned profile or raises ValueError."""
    hw = _merge(copy.deepcopy(DEFAULT), hw)
    d = hw["display"]
    if d["driver"] not in DRIVERS:
        raise ValueError(f"Неизвестный драйвер дисплея: {d['driver']}")
    for k in ("width", "height"):
        d[k] = int(d[k])
        if not 32 <= d[k] <= 800:
            raise ValueError("Разрешение дисплея должно быть от 32 до 800 точек")
    for k in ("offset_x", "offset_y"):
        d[k] = int(d[k])
    for k in ("mirror_x", "mirror_y", "swap_xy", "invert", "bl_invert"):
        d[k] = bool(d[k])
    d["rgb_order"] = "bgr" if d.get("rgb_order") == "bgr" else "rgb"
    d["spi_mode"] = int(d["spi_mode"]) & 3
    d["pclk_mhz"] = max(1, min(80, int(d["pclk_mhz"])))
    d["i2c_addr"] = int(d["i2c_addr"])

    used: dict[int, str] = {}

    def claim(p, what):
        p = _pin(p, what)
        if p >= 0:
            if p in used:
                raise ValueError(f"GPIO{p} назначен дважды: «{used[p]}» и «{what}»")
            used[p] = what
        return p

    bus = DRIVERS[d["driver"]]["bus"]
    names = {"mosi": "Дисплей MOSI/SDA", "sclk": "Дисплей SCLK", "cs": "Дисплей CS", "dc": "Дисплей DC",
             "rst": "Дисплей RST", "bl": "Подсветка", "sda": "Дисплей I2C SDA", "scl": "Дисплей I2C SCL"}
    for k, v in d["pins"].items():
        active = (bus == "spi" and k not in ("sda", "scl")) or (bus == "i2c" and k in ("sda", "scl", "rst"))
        d["pins"][k] = claim(v, names.get(k, k)) if active else _pin(v, names.get(k, k))
    for k, v in hw["audio"].items():
        hw["audio"][k] = claim(v, f"Звук {k}")
    for k, v in hw["buttons"].items():
        hw["buttons"][k] = claim(v, f"Кнопка {k}")
    for k, v in hw["power"].items():
        hw["power"][k] = claim(v, f"Питание {k}")

    inputs = []
    for i, inp in enumerate(hw.get("inputs") or []):
        name = str(inp.get("name") or f"Вход {i + 1}")[:32]
        t = inp.get("type", "touch")
        if t not in INPUT_TYPES:
            raise ValueError(f"{name}: тип должен быть touch или button")
        p = claim(inp.get("pin"), name)
        if t == "touch" and p not in TOUCH_PINS:
            raise ValueError(f"{name}: сенсорный вход возможен только на GPIO1–14, а указан GPIO{p}")
        item = {"pin": p, "type": t, "name": name,
                "sensitivity": max(1, min(30, int(inp.get("sensitivity", 4))))}
        for g in GESTURES:
            acts = [a.strip() for a in str(inp.get(g, "none")).split(",") if a.strip()]
            for a in acts:
                if a not in ACTIONS:
                    raise ValueError(f"{name}: неизвестное действие «{a}»")
            item[g] = ",".join(acts) or "none"
        inputs.append(item)
    if len(inputs) > 8:
        raise ValueError("Не больше 8 входов")
    hw["inputs"] = inputs

    outputs = []
    for i, out in enumerate(hw.get("outputs") or []):
        name = str(out.get("name") or f"Выход {i + 1}")[:32]
        if out.get("type") not in OUTPUT_TYPES:
            raise ValueError(f"{name}: неизвестный тип выхода")
        outputs.append({"pin": claim(out.get("pin"), name), "type": out["type"], "name": name,
                        "active_low": bool(out.get("active_low", False))})
    hw["outputs"] = outputs

    cam = hw["camera"]
    if cam.get("type") not in CAMERA_TYPES:
        raise ValueError("Неизвестный тип камеры")
    if cam["type"] == "board":
        for k, v in cam["pins"].items():
            cam["pins"][k] = claim(v, f"Камера {k}")
        need = ("d0", "d1", "d2", "d3", "d4", "d5", "d6", "d7", "xclk", "pclk", "vsync", "href", "sda", "scl")
        missing = [k for k in need if cam["pins"][k] < 0]
        if missing:
            raise ValueError("Для камеры на плате не заданы пины: " + ", ".join(missing))
    cam["url"] = str(cam.get("url", ""))[:200]
    cam["pc_device"] = str(cam.get("pc_device", ""))[:200]
    return hw


def save(hw: dict) -> dict:
    hw = validate(hw)
    FILE.write_text(json.dumps(hw, ensure_ascii=False, indent=2), encoding="utf-8")
    return hw


def to_device(hw: dict) -> dict:
    """The compact form the firmware parses (no labels, only what it needs)."""
    d = hw["display"]
    return {
        "display": {k: d[k] for k in ("driver", "width", "height", "offset_x", "offset_y", "mirror_x", "mirror_y",
                                      "swap_xy", "invert", "rgb_order", "spi_mode", "pclk_mhz", "pins",
                                      "bl_invert", "i2c_addr")},
        "audio": hw["audio"], "buttons": hw["buttons"], "power": hw["power"],
        "inputs": hw["inputs"], "outputs": hw["outputs"],
        "camera": {"type": hw["camera"]["type"], "pins": hw["camera"]["pins"]},
    }


def catalog() -> dict:
    """Everything the settings UI needs to render choices."""
    return {"drivers": DRIVERS, "presets": DISPLAY_PRESETS, "actions": ACTIONS, "gestures": list(GESTURES),
            "input_types": list(INPUT_TYPES), "output_types": OUTPUT_TYPES, "camera_types": CAMERA_TYPES,
            "touch_pins": sorted(TOUCH_PINS), "reserved": sorted(RESERVED), "default": DEFAULT}


def summary_for_assistant(hw: dict) -> str:
    """Short description of inputs/outputs so the assistant can talk about and reconfigure them."""
    parts = []
    for i in hw["inputs"]:
        parts.append(f"{i['name']} (GPIO{i['pin']}, {'сенсор' if i['type'] == 'touch' else 'кнопка'}: "
                     f"касание={i['tap']}, двойное={i['double']}, удержание={i['hold']})")
    for o in hw["outputs"]:
        parts.append(f"{o['name']} (GPIO{o['pin']}, {OUTPUT_TYPES[o['type']]})")
    return "; ".join(parts)


def path() -> Path:
    return FILE
