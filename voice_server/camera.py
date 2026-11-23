"""Eyes for the assistant: a photo from an ESP32-CAM on Wi-Fi, a PC webcam / IP camera, or a camera
module on the device board (the firmware posts its photos to /vision/explain).

Claude Code gets the saved JPEG path and looks at it with its Read tool; local models get a text
description made by a vision model (Ollama or the OpenAI-compatible engine).
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

from . import server_tools
from .config import ROOT

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger("camera")
SNAP_DIR = ROOT / "logs" / "camera"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_cfg: "Config | None" = None


def attach(cfg: "Config"):
    global _cfg
    _cfg = cfg


# ---------------- capture ----------------
def snap_esp32cam(url: str) -> bytes:
    if not url:
        raise RuntimeError("не задан адрес ESP32-CAM (Настройки → Камера)")
    if not url.rstrip("/").endswith(("capture", "jpg", "jpeg")):
        url = url.rstrip("/") + "/capture"      # stock CameraWebServer sketch
    if not url.lower().startswith(("http://", "https://")):      # no file://, ftp:// ...
        raise RuntimeError("адрес ESP32-CAM должен начинаться с http://")
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read()


def list_pc_cameras() -> list[str]:
    """Names of webcams Windows knows (they are the DirectShow device names)."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-PnpDevice -Class Camera,Image -Status OK | Select-Object -ExpandProperty FriendlyName"],
                             capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW).stdout
        return [l.strip() for l in out.splitlines() if l.strip()]
    except Exception:
        return []


def snap_pc(device: str) -> bytes:
    """One JPEG from a webcam (DirectShow name or index) or an RTSP/HTTP camera URL, via PyAV."""
    import av
    if not device or device.isdigit():
        cams = list_pc_cameras()
        if not cams:
            raise RuntimeError("веб-камера не найдена")
        device = cams[int(device or 0) % len(cams)]
    if "://" in device:
        if not device.lower().startswith(("rtsp://", "rtsps://", "http://", "https://")):
            raise RuntimeError("адрес камеры: только rtsp://, rtsps://, http:// или https://")
        container = av.open(device, options={"rtsp_transport": "tcp", "stimeout": "8000000"}, timeout=10)
    else:
        container = av.open(f"video={device}", format="dshow", options={"rtbufsize": "64M"})
    try:
        for i, frame in enumerate(container.decode(video=0)):
            if i < 5 and "://" not in device:   # let the webcam adjust exposure
                continue
            img = frame.to_image()
            img.thumbnail((1024, 1024))
            import io
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85)
            return buf.getvalue()
    finally:
        container.close()
    raise RuntimeError("камера не дала кадр")


def save(jpeg: bytes) -> Path:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAP_DIR / f"snap_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
    path.write_bytes(jpeg)
    shots = sorted(SNAP_DIR.glob("snap_*.jpg"))
    for old in shots[:-30]:       # keep the last 30
        old.unlink(missing_ok=True)
    return path


# ---------------- understanding ----------------
async def describe(jpeg: bytes, question: str) -> str:
    """What is in the picture, answering `question`. Claude reads the file itself."""
    cfg = _cfg
    path = save(jpeg)
    if cfg.brain == "claude":
        return (f"Снимок сохранён: {path}. Открой этот файл инструментом Read, чтобы увидеть изображение, "
                f"и ответь на вопрос: {question}")
    b64 = base64.b64encode(jpeg).decode()
    timeout = aiohttp.ClientTimeout(total=180)
    prompt = question or "Опиши, что на снимке."
    async with aiohttp.ClientSession(timeout=timeout) as s:
        if cfg.brain == "ollama":
            model = cfg.camera.vision_model or cfg.ollama.model
            body = {"model": model, "stream": False, "think": False,
                    "messages": [{"role": "user", "content": prompt + " Ответь по-русски кратко.", "images": [b64]}]}
            async with s.post(cfg.ollama.url.rstrip("/") + "/api/chat", json=body) as r:
                data = await r.json()
                if r.status != 200:
                    raise RuntimeError(f"модель {model} не видит изображения: {str(data)[:150]}")
                return data["message"]["content"].strip()
        model = cfg.camera.vision_model or cfg.openai.model
        body = {"model": model, "max_tokens": 400, "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt + " Ответь по-русски кратко."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]}
        headers = {"Authorization": f"Bearer {cfg.openai.api_key}"} if cfg.openai.api_key else {}
        async with s.post(cfg.openai.url.rstrip("/") + "/chat/completions", json=body, headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"модель {model} не видит изображения: {str(data)[:150]}")
            return data["choices"][0]["message"]["content"].strip()


async def capture(source: str = "") -> bytes:
    cfg = _cfg
    src = source or cfg.camera.source
    if src == "esp32cam":
        return await asyncio.to_thread(snap_esp32cam, cfg.camera.esp32cam_url)
    if src == "pc":
        return await asyncio.to_thread(snap_pc, cfg.camera.pc_device)
    if src == "board":
        raise RuntimeError("камера на плате снимает через инструмент устройства self.camera.take_photo")
    raise RuntimeError("камера не настроена (Настройки → Камера)")


# ---------------- assistant tool ----------------
async def camera_look(devices, question: str = "") -> str:
    if _cfg is None or _cfg.camera.source in ("none", "board"):
        return ("Камера ПК/ESP32-CAM не настроена." if _cfg is None or _cfg.camera.source == "none"
                else "Используй инструмент устройства self.camera.take_photo.")
    jpeg = await capture()
    return await describe(jpeg, question)


server_tools.register(
    "camera_look", camera_look,
    "Посмотреть через камеру (ESP32-CAM или камера компьютера) и ответить на вопрос о том, что видно. "
    "`question`: что именно нужно узнать на снимке.", {"question": {"type": "string"}}, [])


# ---------------- HTTP: device board camera + settings preview ----------------
async def vision_explain(request: web.Request) -> web.Response:
    """The firmware's self.camera.take_photo posts multipart {question, file} here."""
    svc = request.app["svc"]
    token = svc.cfg.server.token
    got = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if token and not hmac.compare_digest(got.encode(), token.encode()):
        raise web.HTTPUnauthorized()
    question, jpeg = "", b""
    reader = await request.multipart()
    async for part in reader:
        if part.name == "question":
            question = (await part.text())[:500]
        elif part.name in ("file", "image"):
            jpeg = await part.read(decode=False)
    if not jpeg:
        return web.json_response({"success": False, "message": "no image"})
    try:
        text = await describe(jpeg, question)
        return web.json_response({"success": True, "text": text})
    except Exception as e:
        log.warning("vision failed: %s", e)
        return web.json_response({"success": False, "message": str(e)})


async def api_snapshot(request: web.Request) -> web.Response:
    """Settings app: test the configured camera, returns the JPEG."""
    try:
        jpeg = await capture(request.query.get("source", ""))
    except Exception as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.Response(body=jpeg, content_type="image/jpeg")


async def api_cameras(request: web.Request) -> web.Response:
    return web.json_response({"pc": await asyncio.to_thread(list_pc_cameras)})


def setup_routes(app: web.Application):
    app.router.add_post("/vision/explain", vision_explain)
    app.router.add_get("/api/camera/snapshot", api_snapshot)
    app.router.add_get("/api/camera/list", api_cameras)


def mcp_capabilities(cfg: "Config", host: str) -> dict:
    """Sent to the device in MCP initialize: where its board camera posts photos."""
    return {"vision": {"url": f"http://{host}:{cfg.server.port}/vision/explain", "token": cfg.server.token}}

