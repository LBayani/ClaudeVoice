# ClaudeVoice

A voice body for Claude (or a local model) on a cheap **xiaozhi-esp32** device: you talk to the
device, a server on your PC recognises speech, an assistant answers and acts, the device speaks back —
with animated eyes, touch "antennae", GPIO control, music and a camera.

```
ESP32-S3 device (wake word, mic, speaker, display, antennae)
  ⇅ Wi-Fi: WebSocket, Opus 16 kHz up / 24 kHz down (xiaozhi protocol v1) + MCP tools
ClaudeVoice server on your PC: Silero VAD → faster-whisper (ASR) → brain → Silero TTS
  brain = Claude Code · Ollama · LM Studio / Bionic / llama.cpp / vLLM / any OpenAI-compatible engine
```

Features: interrupting an answer (wake word, button or antenna — the server ignores the mic while the
device speaks, since it would hear itself), characters (personas), 8 recolourable screen skins + a skin editor, local models
that can open apps on the PC, device auto-discovery on the LAN, a hardware profile (display, pins,
antennae actions, outputs), music (YouTube, Yandex Music, radio, Spotify), camera (ESP32-CAM, PC/IP
camera, board module), a settings app and a tray launcher. Interface language: Russian.

## Requirements

- Windows 10/11, an NVIDIA GPU is recommended (CPU works with smaller models, see below)
- [uv](https://docs.astral.sh/uv/) (Python 3.12 is installed by it)
- For the Claude brain: [Claude Code](https://claude.com/claude-code) logged in on this PC
- For local brains: [Ollama](https://ollama.com) or LM Studio / another OpenAI-compatible engine
- The device: an ESP32-S3 xiaozhi board; the firmware here targets **zhengchen 1.54" TFT Wi-Fi**,
  other displays and pins are set in the app (see `firmware/`)

## Install and run

```powershell
git clone <this repository> ClaudeVoice
cd ClaudeVoice
copy config.example.yaml config.yaml
uv sync
uv run python -m voice_server
```

Open the settings app at http://127.0.0.1:8765/ui. First start downloads Whisper `large-v3-turbo`
(~1.6 GB) and Silero `v4_ru` (~100 MB) into `models/`.

Tray launcher (no console window; starts the server hidden, restarts it on crash, opens the app):

```powershell
uv run --with pyinstaller pyinstaller --noconfirm --onefile --windowed --name ClaudeVoice --icon "$PWD\assets\app.ico" --exclude-module numpy --distpath . --workpath build_exe --specpath build_exe tray_app.py
```

The exe is only the tray shell (~16 MB): AI libraries and models stay in `.venv` and `models/`.

Offline self-test without the device (server must be running):

```powershell
uv run python tools/fake_device.py
uv run python tools/fake_device.py --barge   # interrupt test
```

## Set up a device (setup wizard)

Click **«Мастер настройки»** in the app header (it also opens by itself on first start, and from the
tray menu). Connect the board by USB and the wizard:

1. finds it and reads the chip, flash, PSRAM, MAC and what firmware it runs now;
2. recognises the board (xiaozhi boards carry their name) or lets you pick one of ~60 known boards, or
   «Своя плата» to set the pins later in **Железо**;
3. backs up the whole flash to `backups\`, then writes the **universal ClaudeVoice firmware** for the
   chip (downloaded from this repository's Releases) plus a settings partition with the board profile,
   your Wi-Fi and this PC's address — no compiler, no Docker;
4. waits until the device joins the network and finds the PC.

Supported chips: ESP32-S3 (16 MB / 8 MB flash, octal or quad PSRAM) and ESP32-C3 (4 MB+). See
`firmware/README.md`.

## Point the device at this server

With the firmware from `firmware/` there is nothing to set: the device finds the server on the LAN
(see *Automatic discovery*). With other firmware:

1. Put the device into Wi-Fi configuration mode, join its `Xiaozhi-XXXX` hotspot, open the page.
2. In **Advanced**, set **Custom OTA URL** to `http://<server-ip>:8765/xiaozhi/ota/` and save.
3. On boot the device asks that URL, receives the WebSocket address + token and switches from the
   vendor cloud to this server.

To go back to the vendor cloud, clear the Custom OTA URL.

## Automatic discovery (no fixed IP needed)

Firmware 2.4+ finds the server by itself: when the OTA check or the WebSocket connection fails, the
device broadcasts `{"cv":"discover"}` on **UDP 8766**; every ClaudeVoice server on the LAN answers
with its id and its current address. The device pairs with the first server that answers and then
prefers it. So PC and device may both get new DHCP addresses — they still find each other.
Windows must allow inbound UDP 8766 for the server's Python (the usual "Allow access" prompt does it).
Switch it off in the app: **Железо → Связь с компьютером**.

## Hardware profile (display, pins, antennae, outputs, camera)

Everything about the wiring lives in `hardware.json` and is edited in the app (**Железо**):

- **Display**: driver (ST7789, GC9A01, ILI9341, ST7796, SSD1306/SH1106 OLED, none), resolution,
  offsets, rotation/mirroring, colour order, SPI clock, all pins — or a ready preset.
- **Inputs**: touch antennae (GPIO1–14) or buttons on any free pin. Tap / double tap / hold each run
  one or two actions: affection (reward), wake, push-to-talk, stop, tell the assistant (it decides),
  vibrate, volume, emotions, sounds. The assistant can reassign them by voice (`body_set_input_action`).
- **Outputs**: vibration motor, LED, buzzer, relay (assistant tools `self.outputs.set`, `self.vibrate`).
- **Audio, buttons, power** pins, and **camera module** pins for boards that have one.

The device downloads the profile at boot from `/device/hardware.json` (and at once when you press
**Save**); pin/display changes reboot it. A wrong profile never bricks it: hold **BOOT** right after powering
on (the universal firmware) or **Volume −** (the zhengchen 1.54" build) to boot with the built-in
defaults, then fix the profile in the app.

## Music

The assistant plays music on the device speaker (`music_play`, `music_stop`, `music_next`, …):

| Source | What you need |
|---|---|
| YouTube / YouTube Music | nothing (yt-dlp) |
| Internet radio | nothing (radio-browser.info catalogue or any stream URL) |
| Yandex Music | your account token (Plus for full tracks); "моя волна" plays My Wave |
| Spotify | Premium + [librespot](https://github.com/librespot-org/librespot) binary; pick the "ClaudeVoice" speaker in the Spotify app |

Talking to the device pauses the music; it resumes after the answer. "Стоп" stops it.

## Camera

**Камера** in the app: an ESP32-CAM on Wi-Fi (stock `CameraWebServer` sketch, `/capture`), a PC
webcam or an RTSP/HTTP IP camera, or a camera module on the device board (pins in the hardware
profile; the board posts photos to `/vision/explain`). Claude looks at the saved photo itself; local
brains use a vision model (set one, e.g. `qwen2.5vl:7b`, if the chat model cannot see images).

## Privacy

`config.yaml`, `settings.json` (API keys, Yandex token) and `hardware.json` stay on your PC and are
git-ignored. The settings app and its API answer only on localhost.

## Threat model (read before enabling shell tools)

ClaudeVoice is built for a **home LAN you trust**. What it protects and what it does not:

- **Settings app / API** (`/ui`, `/api/*`): only from this PC. Requests must come from 127.0.0.1 with a
  `localhost` / `127.0.0.1` Host (stops DNS rebinding); state-changing calls also need the `X-CV-App`
  header that only the app sends, and requests with a foreign `Origin` are refused (stops CSRF from
  web pages you open in the browser).
- **Device ⇄ server** is plain HTTP + WebSocket, **no TLS**: anyone on the LAN can listen to the audio
  and the replies. Encryption (wss) is on the roadmap.
- **Device token**: the OTA endpoint hands the WebSocket token only to devices you know — boards set up
  by the wizard or in **Железо**, boards you allow in the app («Новое устройство просит подключиться»),
  and the first board of a fresh install. Device-Id is the MAC address, which others on the LAN can
  see and copy, so this keeps out strangers and stray gadgets, not a skilled attacker inside your network.
- **Discovery** (UDP 8766): a device pairs with the first server that answers. A rogue server on the LAN
  could answer first; once paired, the device prefers its server's id.
- **The voice is the key.** The device hears everyone in the room (and the TV). By default Claude only
  reads (files, web). Shell and file editing are opt-in; when on, a server-side hook refuses deleting,
  installing, shutting down and unreadable (encoded / inline) commands unless your phrase in that turn
  was an explicit «да». It is a safety net with an incomplete pattern list, **not a sandbox**: give shell
  tools only on a PC where a wrong command is not a disaster. The same goes for PowerShell for local
  models («Действия на компьютере»), which is off by default.
- Firmware images downloaded by the wizard are checked against `SHA256SUMS.txt` of the release.

## Moving to another server

Copy the folder and start it there: the device finds the new server by itself (UDP discovery).
`server.public_host` is only needed if auto-detection picks the wrong network card. CPU-only machines: set `asr.device: cpu`,
`asr.compute_type: int8`, a smaller `asr.model` (e.g. `small`), and `tts.device: cpu`.

## Files

| File | Purpose |
|---|---|
| `voice_server/app.py` | HTTP OTA endpoint + WebSocket endpoint |
| `voice_server/session.py` | Per-device protocol state machine, pacing, abort, idle close |
| `voice_server/vad.py` | Silero VAD utterance segmentation |
| `voice_server/asr.py` | faster-whisper wrapper + hallucination filter |
| `voice_server/tts.py` | Silero TTS |
| `voice_server/speakable.py` | Markdown/numbers/Latin → speakable Russian |
| `voice_server/brain.py` | Claude Code brain (+ `brain_ollama.py`, `brain_openai.py` for local engines) |
| `voice_server/guard.py` | Hook that refuses irreversible shell / file actions without a spoken «да» |
| `voice_server/pairing.py` | Which devices get the WebSocket token |
| `voice_server/discovery.py` | UDP 8766 auto-discovery responder |
| `voice_server/hardware.py` | Hardware profile: validation, presets, actions |
| `voice_server/server_tools.py` | Body tools for every brain (inputs, music, camera) |
| `voice_server/music.py` | YouTube / Yandex / radio / Spotify playback to the device |
| `voice_server/camera.py` | ESP32-CAM / PC / IP / board camera + vision |
| `voice_server/flasher.py` | Setup wizard: board detection, backup, universal image + settings partition |
| `firmware/board_presets.json` | Pins of known xiaozhi boards (`tools/import_board_presets.py`) |
| `tools/fake_device.py` | Virtual device for offline tests |

## License

MIT, see `LICENSE`. The firmware patch applies to [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)
(MIT). Voice models are downloaded from their authors under their own licenses (Silero, Whisper).
