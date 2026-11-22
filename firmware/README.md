# ClaudeVoice firmware

The device runs [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) (MIT) with the changes in
`claudevoice.patch`, applied to upstream commit `e77dedb1309153bb63fed285772962c920c97dd4` (v2.2.4).

What the patch adds (board `zhengchen-1.54tft-wifi`, ESP32-S3 with octal PSRAM):

- animated eyes and 8 skins rendered on the device (`cv_eyes*`, `cv_skins.cc`), skins loaded from the server
- **hardware profile** (`cv_hw.*`): display driver/resolution/pins, audio/button/power pins, inputs,
  outputs and camera pins come from the server's `/device/hardware.json`; hold **Volume −** at power-on
  to ignore a broken profile
- touch antennae / buttons with configurable actions, vibration and other outputs, GPIO tools for the
  assistant (`cv_io.*`)
- **LAN auto-discovery** of the server over UDP 8766 (`main/cv_discovery.*`)
- board camera support through the stock `self.camera.take_photo` tool

## Build

Needs Git and Docker Desktop.

```powershell
cd firmware
.\build.ps1
```

The script clones upstream, applies the patch and builds `xiaozhi-esp32\build\xiaozhi.bin`, then copies
it to `..\ota\` so devices already running this firmware update over Wi-Fi on their next start.

## First install over USB

Hold **BOOT**, connect USB (or press RESET while holding BOOT), then:

```powershell
cd firmware\xiaozhi-esp32\build
esptool --chip esp32s3 --port COM8 --baud 460800 write-flash --flash-mode dio --flash-freq 80m --flash-size 16MB `
  0x0 bootloader/bootloader.bin 0x8000 partition_table/partition-table.bin 0xd000 ota_data_initial.bin `
  0x20000 xiaozhi.bin 0x800000 generated_assets.bin
```

(`pip install esptool`; replace `COM8` with your port.) Wi-Fi is set up on first boot through the
device's `Xiaozhi-XXXX` hotspot; after that it finds the ClaudeVoice server by itself.
