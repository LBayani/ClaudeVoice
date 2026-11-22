"""Builds firmware/board_presets.json: hardware profiles of the xiaozhi-esp32 boards, read from their
config.h / board .cc (best effort), so the setup wizard can prefill pins for a known board.

    uv run python tools/import_board_presets.py [path\\to\\xiaozhi-esp32]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "firmware" / "xiaozhi-esp32"
OUT = ROOT / "firmware" / "board_presets.json"


def defines(text: str) -> dict:
    out = {}
    for name, val in re.findall(r"^\s*#define\s+(\w+)\s+(.+?)\s*(?://.*)?$", text, re.M):
        out[name] = val.strip()
    return out


def gpio(d: dict, *names) -> int:
    for n in names:
        v = d.get(n)
        if v is None:
            continue
        m = re.search(r"GPIO_NUM_(\d+)|^\(?(-?\d+)\)?$", v)
        if m:
            return int(m.group(1) or m.group(2))
        if "NC" in v:
            return -1
        if v in d:                       # alias to another define
            return gpio(d, v)
    return -1


def num(d, name, default):
    v = d.get(name)
    if v is None:
        return default
    m = re.search(r"-?\d+", v)
    return int(m.group()) if m else default


def flag(d, name, default=False):
    v = d.get(name)
    return default if v is None else v.strip().lower() == "true"


def preset(board_dir: Path) -> dict | None:
    cfg = board_dir / "config.json"
    hdr = board_dir / "config.h"
    if not cfg.exists() or not hdr.exists() or board_dir.name.startswith("claudevoice"):
        return None
    meta = json.loads(cfg.read_text(encoding="utf-8"))
    d = defines(hdr.read_text(encoding="utf-8", errors="replace"))
    code = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in board_dir.glob("*.cc"))
    code += "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in board_dir.glob("*.h"))

    # boards already described by a ClaudeVoice factory profile
    m = re.search(r'#define\s+CV_DEFAULT_PROFILE\s+((?:"(?:[^"\\]|\\.)*"\s*\\?\s*)+)', hdr.read_text(encoding="utf-8"))
    if m:
        js = "".join(json.loads(part) for part in re.findall(r'"(?:[^"\\]|\\.)*"', m.group(1)))
        prof = json.loads(js)
        return {"label": board_dir.name, "target": meta.get("target", ""), "source": "claudevoice", "supported": True,
                "camera_on_board": False, "profile": prof,
                "names": [b.get("name") for b in meta.get("builds", []) if b.get("name")]}

    # pins assigned directly in the board code: buscfg.sclk_io_num = DISPLAY_SCLK; etc.
    def code_pin(field):
        mm = re.search(field + r"\s*=\s*([A-Za-z_0-9]+)", code)
        if not mm:
            return -1
        v = mm.group(1)
        g = re.search(r"GPIO_NUM_(\d+)", v)
        return int(g.group(1)) if g else gpio(d, v)

    # display
    driver = "none"
    for drv, pat in (("st7789", "esp_lcd_new_panel_st7789"), ("gc9a01", "esp_lcd_new_panel_gc9a01"),
                     ("ili9341", "esp_lcd_new_panel_ili9341"), ("st7796", "esp_lcd_new_panel_st7796"),
                     ("ssd1306", "esp_lcd_new_panel_ssd1306"), ("sh1106", "esp_lcd_new_panel_sh1106")):
        if pat in code:
            driver = drv
            break
    disp = {"driver": driver, "width": num(d, "DISPLAY_WIDTH", 240), "height": num(d, "DISPLAY_HEIGHT", 240),
            "offset_x": num(d, "DISPLAY_OFFSET_X", 0), "offset_y": num(d, "DISPLAY_OFFSET_Y", 0),
            "mirror_x": flag(d, "DISPLAY_MIRROR_X"), "mirror_y": flag(d, "DISPLAY_MIRROR_Y"),
            "swap_xy": flag(d, "DISPLAY_SWAP_XY"), "invert": flag(d, "DISPLAY_INVERT_COLOR", True),
            "rgb_order": "bgr" if "LCD_RGB_ELEMENT_ORDER_BGR" in code else "rgb",
            "spi_mode": num(d, "DISPLAY_SPI_MODE", 0), "pclk_mhz": 40,
            "bl_invert": flag(d, "DISPLAY_BACKLIGHT_OUTPUT_INVERT"),
            "pins": {"mosi": gpio(d, "DISPLAY_MOSI_PIN", "DISPLAY_SPI_MOSI_PIN", "DISPLAY_MOSI", "DISPLAY_SDA"),
                     "sclk": gpio(d, "DISPLAY_CLK_PIN", "DISPLAY_SPI_SCLK_PIN", "DISPLAY_SCLK_PIN", "DISPLAY_SCK_PIN",
                                  "DISPLAY_SCL", "DISPLAY_CLK"),
                     "cs": gpio(d, "DISPLAY_CS_PIN", "DISPLAY_SPI_CS_PIN", "DISPLAY_CS"),
                     "dc": gpio(d, "DISPLAY_DC_PIN", "DISPLAY_SPI_DC_PIN", "DISPLAY_DC"),
                     "rst": gpio(d, "DISPLAY_RST_PIN", "DISPLAY_RESET_PIN", "DISPLAY_SPI_RESET_PIN", "DISPLAY_RES",
                                 "DISPLAY_RST"),
                     "bl": gpio(d, "DISPLAY_BACKLIGHT_PIN", "DISPLAY_BACKLIGHT"),
                     "sda": -1, "scl": -1}}
    for key, field in (("mosi", r"mosi_io_num"), ("sclk", r"sclk_io_num"), ("cs", r"cs_gpio_num"),
                       ("dc", r"dc_gpio_num"), ("rst", r"reset_gpio_num")):
        if disp["pins"][key] < 0:
            disp["pins"][key] = code_pin(field)
    if driver in ("ssd1306", "sh1106"):
        disp["pins"].update(mosi=-1, sclk=-1, cs=-1, dc=-1, bl=-1,
                            sda=gpio(d, "DISPLAY_SDA_PIN", "AUDIO_CODEC_I2C_SDA_PIN"),
                            scl=gpio(d, "DISPLAY_SCL_PIN", "AUDIO_CODEC_I2C_SCL_PIN"))
    # audio
    if "BoxAudioCodec" in code:
        codec = "box"
    elif "Es8311AudioCodec" in code:
        codec = "es8311"
    elif "NoAudioCodecSimplex" in code:
        codec = "i2s_simplex"
    elif "NoAudioCodecDuplex" in code:
        codec = "i2s_duplex"
    else:
        codec = "none"     # other codecs (ES8388, ES8374...) are not supported by the universal firmware
    audio = {"codec": codec, "in_rate": num(d, "AUDIO_INPUT_SAMPLE_RATE", 16000),
             "out_rate": num(d, "AUDIO_OUTPUT_SAMPLE_RATE", 24000),
             "mic_ws": gpio(d, "AUDIO_I2S_MIC_GPIO_WS"), "mic_sck": gpio(d, "AUDIO_I2S_MIC_GPIO_SCK"),
             "mic_din": gpio(d, "AUDIO_I2S_MIC_GPIO_DIN"), "spk_dout": gpio(d, "AUDIO_I2S_SPK_GPIO_DOUT"),
             "spk_bclk": gpio(d, "AUDIO_I2S_SPK_GPIO_BCLK"), "spk_lrck": gpio(d, "AUDIO_I2S_SPK_GPIO_LRCK", "AUDIO_I2S_SPK_GPIO_WS"),
             "mclk": gpio(d, "AUDIO_I2S_GPIO_MCLK"), "bclk": gpio(d, "AUDIO_I2S_GPIO_BCLK"),
             "ws": gpio(d, "AUDIO_I2S_GPIO_WS", "AUDIO_I2S_GPIO_LRCK"), "dout": gpio(d, "AUDIO_I2S_GPIO_DOUT"),
             "din": gpio(d, "AUDIO_I2S_GPIO_DIN"), "pa": gpio(d, "AUDIO_CODEC_PA_PIN"),
             "i2c_sda": gpio(d, "AUDIO_CODEC_I2C_SDA_PIN"), "i2c_scl": gpio(d, "AUDIO_CODEC_I2C_SCL_PIN"),
             "es8311_addr": 0x30, "es7210_addr": 0x80, "pa_inverted": False, "use_mclk": True}
    # a status LED sharing a pin with something else is dropped (some boards reuse it)
    led = gpio(d, "BUILTIN_LED_GPIO")
    taken = set(disp["pins"].values()) | {v for k, v in audio.items() if isinstance(v, int) and k not in
                                           ("in_rate", "out_rate", "es8311_addr", "es7210_addr")}
    if led in taken:
        led = -1
    return {
        "label": board_dir.name, "target": meta.get("target", ""), "source": "xiaozhi",
        "supported": codec != "none", "camera_on_board": "Esp32Camera" in code,
        "profile": {
            "display": disp, "audio": audio,
            "buttons": {"boot": gpio(d, "BOOT_BUTTON_GPIO"), "volume_up": gpio(d, "VOLUME_UP_BUTTON_GPIO"),
                        "volume_down": gpio(d, "VOLUME_DOWN_BUTTON_GPIO")},
            "power": {"latch": -1, "charge_detect": -1, "led": led},
            "inputs": [], "outputs": [],
        },
        "names": [b.get("name") for b in meta.get("builds", []) if b.get("name")],
    }


def main():
    presets = {}
    for bd in sorted((SRC / "main" / "boards").iterdir()):
        if not bd.is_dir():
            continue
        try:
            p = preset(bd)
        except Exception as e:      # one odd board must not stop the rest
            print(f"skip {bd.name}: {e}")
            continue
        if p:
            presets[bd.name] = p
    OUT.write_text(json.dumps(presets, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for p in presets.values() if p["supported"])
    print(f"{len(presets)} boards, {ok} with a supported audio codec -> {OUT}")


if __name__ == "__main__":
    main()
