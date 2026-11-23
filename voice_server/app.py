"""HTTP (OTA config + firmware) + WebSocket server for xiaozhi devices, plus /mcp for Claude."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from .brain import make_brain
from .config import ROOT, Config, atomic_write_text
from .mcp_bridge import DeviceRegistry, mcp_endpoint
from .speakable import to_speakable

if TYPE_CHECKING:   # heavy (PyTorch): imported lazily so the HTTP port opens in a couple of seconds
    import numpy as np

    from .asr import Asr
    from .tts import Tts

log = logging.getLogger("app")

OTA_DIR = ROOT / "ota"          # drop a built xiaozhi.bin here to update devices over Wi-Fi
OTA_FILE = "xiaozhi.bin"


@dataclass
class Services:
    cfg: Config
    asr: Asr
    tts: Tts
    brain: object
    devices: DeviceRegistry = field(default_factory=DeviceRegistry)
    greetings: list[tuple[str, np.ndarray]] = field(default_factory=list)
    fillers: list[tuple[str, np.ndarray]] = field(default_factory=list)
    phrases: dict[str, np.ndarray] = field(default_factory=dict)
    last_ota: dict = field(default_factory=dict)      # last time the device checked in
    started: float = field(default_factory=time.time)
    music: object = None

    def rebuild_brain(self):
        mcp_url = f"http://127.0.0.1:{self.cfg.server.port}/mcp"
        self.brain = make_brain(self.cfg.brain, self.cfg.claude, mcp_url, self.cfg.ollama, self.devices,
                                self.cfg.openai, self.cfg.pc_tools)
        from . import personas
        # every brain reads the active character's prompt on each turn, so switching applies at once
        self.brain.prompt_fn = lambda: personas.compose(self.cfg.persona)
        if self.cfg.brain == "openai":
            from . import engine_manager
            self.brain.ensure_fn = lambda: engine_manager.ensure_engine(self.cfg)
        if hasattr(self.brain, "warmup"):
            try:
                asyncio.get_running_loop().create_task(self.brain.warmup())
            except RuntimeError:
                self._pending_warmup = True   # no loop yet (startup): warm up once the server runs

    def recache_phrases(self):
        def cache(texts):
            return [(t, p) for t in texts if len(p := self.tts.synth(to_speakable(t)))]
        self.greetings = cache(self.cfg.session.greetings)
        self.fillers = cache(self.cfg.session.fillers)
        self.phrases = dict(cache(["Остановил.", "Хорошо."]))


def load_services(cfg: Config, devices: DeviceRegistry | None = None, stage=lambda s: None) -> Services:
    """Heavy start-up (models). Runs in a worker thread while the HTTP server is already up."""
    from . import ollama_manager
    if cfg.ollama.managed:   # start the private Ollama first: it boots in parallel with our models
        stage("запуск локальной Ollama")
        cfg.ollama.url = ollama_manager.managed_url(cfg)
        ollama_manager.ensure_running(cfg)
    stage("библиотеки (PyTorch, CUDA)")
    from .asr import Asr
    from .session import DeviceSession  # noqa: F401  (warm the import: VAD, Opus)
    from .tts import Tts
    stage("голос (Silero)")
    tts = Tts(cfg.tts, cfg.models_dir)
    stage("распознавание речи (Whisper)")
    asr = Asr(cfg.asr, cfg.models_dir)
    svc = Services(cfg=cfg, asr=asr, tts=tts, brain=None, devices=devices or DeviceRegistry())
    try:
        svc.last_ota = json.loads((cfg.logs_dir / "last_device.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    from . import pairing
    if (svc.last_ota or {}).get("ip") not in ("127.0.0.1", "::1", None):
        pairing.migrate(svc.last_ota.get("device_id", ""))
    stage("мозг и характер")
    from . import camera, music   # registers their assistant tools
    svc.music = music.attach(cfg)
    camera.attach(cfg)
    svc.rebuild_brain()
    stage("готовые фразы")
    svc.recache_phrases()
    return svc


def _ws_url(cfg: Config, host: str = "") -> str:
    return f"ws://{host or cfg.server.lan_host()}:{cfg.server.port}/xiaozhi/v1/"


def _device_host(request: web.Request, cfg: Config) -> str:
    """The address the device just used to reach us is the one it can reach us on."""
    h = (request.host or "").rsplit(":", 1)[0].strip("[]")
    return h if h and h not in ("localhost", "127.0.0.1", "::1") else cfg.server.lan_host()


def clean_field(v, limit: int = 40) -> str:
    """Untrusted text from the LAN (headers, OTA body, discovery) -> short, plain: no markup, no control chars."""
    return re.sub(r"[^\w.:+\- ]", "", str(v or ""))[:limit]


def _ver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def firmware_version(path: Path) -> str | None:
    """Read esp_app_desc_t.version from an ESP-IDF app image."""
    try:
        with path.open("rb") as f:
            head = f.read(0x50)
        if struct.unpack_from("<I", head, 0x20)[0] != 0xABCD5432:
            return None
        return head[0x30:0x50].split(b"\0", 1)[0].decode("ascii", "replace")
    except (OSError, struct.error):
        return None


async def ota(request: web.Request) -> web.Response:
    cfg: Config = request.app["svc"].cfg
    body = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}
    app_info = body.get("application") if isinstance(body, dict) else None
    version = clean_field((app_info if isinstance(app_info, dict) else {}).get("version"), 32) or "0.0.0"
    host = _device_host(request, cfg)
    firmware = {"version": version, "url": ""}       # default: "you are up to date"
    new = firmware_version(OTA_DIR / OTA_FILE)
    if new and _ver(new) > _ver(version):
        firmware = {"version": new,
                    "url": f"http://{host}:{cfg.server.port}/ota/{OTA_FILE}"}
        log.info("Offering firmware %s to device (has %s)", new, version)
    log.info("OTA check from %s (Device-Id=%s, fw %s)", request.remote,
             request.headers.get("Device-Id"), version)
    svc = request.app["svc"]
    from . import pairing
    device_id = clean_field(request.headers.get("Device-Id"), 40)
    board = clean_field(request.headers.get("User-Agent", "").split("/")[0], 60)
    # The token only goes to devices the user knows (see pairing.py); others wait for an OK in the app.
    # A program on this PC (tools/fake_device.py) is trusted: it can read the token from the config anyway.
    local = request.remote in ("127.0.0.1", "::1")
    paired = local or pairing.check(device_id, request.remote, board, (svc.last_ota or {}).get("device_id", ""))
    if not local:      # a test client on this PC is not "the device" (it would also hide the real one)
        svc.last_ota = {"time": time.time(), "ip": request.remote, "version": version,
                        "device_id": device_id, "board": board}
        try:
            atomic_write_text(cfg.logs_dir / "last_device.json", json.dumps(svc.last_ota))
        except OSError:
            pass
    offset_min = -time.timezone // 60 if not time.localtime().tm_isdst else -time.altzone // 60
    websocket = {"url": _ws_url(cfg, host), "version": 1}
    if paired:   # no "token" key at all otherwise: the firmware then keeps whatever token it already has
        websocket["token"] = cfg.server.token
    return web.json_response({
        "server_time": {"timestamp": int(time.time() * 1000), "timezone_offset": offset_min},
        "firmware": firmware,
        "websocket": websocket,
    })


async def ws_handler(request: web.Request) -> web.StreamResponse:
    svc: Services = request.app["svc"]
    token = svc.cfg.server.token
    if token:
        got = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(got, token):
            log.warning("Rejected WS from %s: bad token", request.remote)
            raise web.HTTPUnauthorized()
    ws = web.WebSocketResponse(max_msg_size=1 << 20)
    await ws.prepare(request)
    from .session import DeviceSession
    await DeviceSession(ws, svc, request.headers.get("Device-Id", "?")).run()
    return ws


async def status(request: web.Request) -> web.Response:
    svc: Services = request.app["svc"]
    if svc is None:
        return web.json_response({"ok": True, "loading": True, "stage": request.app["stage"]})
    dev = svc.devices.current()
    return web.json_response({"ok": True, "brain": svc.cfg.brain, "ws": _ws_url(svc.cfg),
                              "device_connected": dev is not None,
                              "device_tools": [t["name"] for t in (dev.mcp_tools if dev else [])],
                              "ota_firmware": firmware_version(OTA_DIR / OTA_FILE)})


@web.middleware
async def loading_guard(request: web.Request, handler):
    """While models load, the settings UI already works; everything else answers 'loading'."""
    if request.app["svc"] is None:
        p = request.path
        if p == "/api/state":
            return web.json_response({"loading": True, "stage": request.app["stage"]})
        if not (p == "/" or p.startswith("/ui")):
            return web.json_response({"loading": True, "stage": request.app["stage"]}, status=503)
    return await handler(request)


def build_app(svc: Services | None = None, devices: DeviceRegistry | None = None) -> web.Application:
    app = web.Application(client_max_size=8 << 20, middlewares=[loading_guard])   # 8 MB: firmware uploads
    app["svc"] = svc
    app["stage"] = "запуск"
    app["devices"] = svc.devices if svc else (devices or DeviceRegistry())
    for path in ("/xiaozhi/ota/", "/xiaozhi/ota"):
        app.router.add_route("*", path, ota)
    for path in ("/xiaozhi/v1/", "/xiaozhi/v1"):
        app.router.add_get(path, ws_handler)
    app.router.add_route("*", "/mcp", mcp_endpoint)
    OTA_DIR.mkdir(exist_ok=True)
    app.router.add_static("/ota/", OTA_DIR)
    app.router.add_get("/", status)
    from .settings_api import setup_routes
    setup_routes(app)
    from . import camera
    camera.setup_routes(app)

    async def on_cleanup(_app):
        from . import ollama_manager
        ollama_manager.stop()
        svc = _app.get("svc")
        if svc is not None and getattr(svc, "music", None) is not None:
            svc.music.stop_spotify()     # librespot must not outlive the server
    app.on_cleanup.append(on_cleanup)
    return app


def attach_services(app: web.Application, svc: Services):
    """Called on the event loop once the models are loaded."""
    app["svc"] = svc
    app["stage"] = "готов"
    if getattr(svc, "_pending_warmup", False) and hasattr(svc.brain, "warmup"):
        asyncio.get_running_loop().create_task(svc.brain.warmup())
