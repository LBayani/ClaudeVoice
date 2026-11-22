# ClaudeVoice firmware

The device runs [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) (MIT) with the changes in
`claudevoice.patch`, applied to upstream commit `e77dedb1309153bb63fed285772962c920c97dd4` (v2.2.4).

## Universal firmware — nothing to compile

Users do not build anything. The app's **setup wizard** («Мастер настройки») flashes a ready
**universal image** for the detected chip and writes a settings partition next to it:

| Image | Chip / memory |
|---|---|
| `claudevoice-s3-16m.bin` | ESP32-S3, 16 MB flash, octal PSRAM (N16R8) |
| `claudevoice-s3-8m.bin` | ESP32-S3, 8 MB flash, octal PSRAM (N8R8) |
| `claudevoice-s3-8m-quad.bin` | ESP32-S3, 8 MB flash, quad PSRAM or none (N8R2, N8) |
| `claudevoice-c3-4m.bin` | ESP32-C3, 4 MB flash or more |

The board itself is not compiled in. Display (ST7789, GC9A01, ILI9341, ST7796, SSD1306/SH1106 OLED),
audio (INMP441 + MAX98357 style I2S, one I2S bus, ES8311, ES8311 + ES7210), buttons, touch antennae
(ESP32-S3), outputs and the camera come from the **hardware profile**. The wizard writes it into NVS
together with Wi-Fi and the server address, taken from a known board (`board_presets.json`, imported
from the xiaozhi board definitions) or left empty to fill in later in the app («Железо»).

The wizard downloads the images from this repository's GitHub release (or uses `images/` if present).

Safe mode: press and hold **BOOT** right after power-on to start with the factory profile.

## What the patch adds

- universal boards `claudevoice-s3` / `claudevoice-c3` and the shared body code in
  `main/boards/claudevoice-common` (also used by `zhengchen-1.54tft-wifi`)
- animated eyes and 8 skins (colour SPI screen + PSRAM), skins loaded from the server
- hardware profile, per device (`/device/hardware.json`, keyed by MAC)
- touch antennae / buttons with configurable actions, outputs, GPIO tools for the assistant
- LAN auto-discovery of the server over UDP 8766
- board camera support (ESP32-S3)

## Building the images (maintainers)

Needs Git and Docker Desktop.

```powershell
cd firmware
.\build.ps1                      # all universal images -> images\*.bin
.\build.ps1 -Variant claudevoice-c3-4m
```

`tools/import_board_presets.py` refreshes `board_presets.json` from the xiaozhi board folders.
