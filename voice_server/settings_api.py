"""REST API + static UI for the ClaudeVoice PC app. Localhost only."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import tempfile
import time
import wave
from pathlib import Path

import aiohttp
from aiohttp import web

from . import winshell
from .config import ROOT, apply_settings, as_dict

log = logging.getLogger("api")
UI_DIR = ROOT / "ui"
SPEAKERS = ["xenia", "baya", "kseniya", "aidar", "eugene"]
# Device tools the app may call directly.
APP_TOOLS = ("self.eyes.", "self.audio_speaker.set_volume", "self.screen.set_brightness",
             "self.get_device_status", "self.antennae.get_state", "self.reboot", "self.gpio.")


@web.middleware
async def local_only(request: web.Request, handler):
    if request.path.startswith(("/api/", "/ui")) and request.remote not in ("127.0.0.1", "::1"):
        raise web.HTTPForbidden(text="ClaudeVoice app is available on this PC only")
    return await handler(request)


def _svc(request):
    return request.app["svc"]


async def _device_call(svc, tool: str, args: dict, timeout=4.0):
    dev = svc.devices.current()
    if dev is None:
        raise web.HTTPConflict(text="Устройство не на связи: разбудите его кнопкой или триггерным словом.")
    res = await dev.mcp_call("tools/call", {"name": tool, "arguments": args}, timeout=timeout)
    text = " ".join(c.get("text", "") for c in (res or {}).get("content", []))
    try:
        return json.loads(text)
    except ValueError:
        return text


async def state(request):
    svc = _svc(request)
    cfg = svc.cfg
    dev = svc.devices.current()
    device = {"connected": dev is not None, "last_ota": svc.last_ota,
              "tools": [t["name"] for t in (dev.mcp_tools or [])] if dev else []}
    if dev is not None and request.query.get("device") == "1":
        for key, tool in (("status", "self.get_device_status"), ("antennae", "self.antennae.get_state")):
            if tool in device["tools"]:
                try:
                    device[key] = await _device_call(svc, tool, {}, timeout=2.5)
                except Exception as e:
                    device[key] = {"error": str(e)}
    from .app import OTA_DIR, OTA_FILE, firmware_version
    conf = as_dict(cfg)
    conf["server"]["token"] = "•" * 8 if cfg.server.token else ""
    conf["claude"].pop("system_prompt", None)
    conf["ollama"].pop("system_prompt", None)
    conf["openai"].pop("system_prompt", None)
    if cfg.openai.api_key:
        conf["openai"]["api_key"] = "•" * 8
    if cfg.music.yandex_token:
        conf["music"]["yandex_token"] = "•" * 8
    return web.json_response({
        "server": {"started": svc.started, "uptime_s": int(time.time() - svc.started),
                   "ota_url": f"http://{cfg.server.lan_host()}:{cfg.server.port}/xiaozhi/ota/",
                   "discovery": request.app.get("discovery", {})},
        "config": conf, "device": device,
        "ota_firmware": firmware_version(OTA_DIR / OTA_FILE),
        "autostart": winshell.autostart_enabled(), "speakers": SPEAKERS,
    })


async def update_config(request):
    svc = _svc(request)
    changes = await request.json()
    try:
        apply_settings(svc.cfg, changes)
    except (KeyError, TypeError, ValueError) as e:
        raise web.HTTPBadRequest(text=f"Неверная настройка: {e}")
    o = changes.get("ollama") or {}
    if {"managed", "managed_models_dir", "managed_port"} & o.keys():
        from . import ollama_manager
        if svc.cfg.ollama.managed:
            svc.cfg.ollama.url = ollama_manager.managed_url(svc.cfg)
            st = await asyncio.to_thread(ollama_manager.ensure_running, svc.cfg)
            if st in ("no-exe", "no-models-dir"):
                raise web.HTTPBadRequest(text={"no-exe": "Не найден ollama.exe",
                                               "no-models-dir": "Папка с моделями не найдена"}[st])
        else:
            ollama_manager.stop()
            svc.cfg.ollama.url = "http://127.0.0.1:11434"
        apply_settings(svc.cfg, {"ollama": {"url": svc.cfg.ollama.url}})
    if {"brain", "claude", "ollama", "openai", "pc_tools"} & changes.keys():
        svc.rebuild_brain()
        log.info("brain -> %s", svc.cfg.brain)
    if "tts" in changes or {"greetings", "fillers"} & (changes.get("session") or {}).keys():
        await asyncio.to_thread(svc.recache_phrases)
    return await state(request)


async def device_call(request):
    body = await request.json()
    tool = body.get("tool", "")
    if not tool.startswith(APP_TOOLS):
        raise web.HTTPForbidden(text=f"tool not allowed: {tool}")
    try:
        return web.json_response({"result": await _device_call(_svc(request), tool, body.get("arguments") or {})})
    except web.HTTPException:
        raise
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502)


async def tts_preview(request):
    svc = _svc(request)
    body = await request.json()
    speaker = body.get("speaker") or svc.cfg.tts.speaker
    if speaker not in SPEAKERS:
        raise web.HTTPBadRequest(text="unknown speaker")
    text = (body.get("text") or "Привет! Так звучит мой голос.")[:300]

    def synth():
        old = svc.cfg.tts.speaker
        svc.cfg.tts.speaker = speaker
        try:
            from .speakable import to_speakable
            return svc.tts.synth(to_speakable(text))
        finally:
            svc.cfg.tts.speaker = old

    pcm = await asyncio.to_thread(synth)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(svc.tts.sample_rate)
        w.writeframes(pcm.tobytes())
    return web.Response(body=buf.getvalue(), content_type="audio/wav")


async def brain_test(request):
    svc = _svc(request)
    text = ((await request.json()).get("text") or "").strip()[:500]
    if not text:
        raise web.HTTPBadRequest(text="empty")
    t0 = time.monotonic()
    parts = []
    async for chunk in svc.brain.respond(text):
        if chunk:
            parts.append(chunk)
    return web.json_response({"brain": svc.cfg.brain, "reply": "".join(parts).strip(),
                              "seconds": round(time.monotonic() - t0, 1)})


async def ollama_info(request):
    url = _svc(request).cfg.ollama.url
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as s:
            async with s.get(f"{url}/api/version") as r:
                version = (await r.json()).get("version")
            async with s.get(f"{url}/api/tags") as r:
                models = (await r.json()).get("models", [])
    except Exception as e:
        return web.json_response({"reachable": False, "error": str(e), "models": []})
    return web.json_response({"reachable": True, "version": version, "models": [
        {"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1),
         "params": (m.get("details") or {}).get("parameter_size", ""),
         "family": (m.get("details") or {}).get("family", "")} for m in models]})


async def openai_models(request):
    """Models of the configured OpenAI-compatible server (LM Studio, llama.cpp, ...)."""
    o = _svc(request).cfg.openai
    url = request.query.get("url") or o.url
    headers = {"Authorization": f"Bearer {o.api_key}"} if o.api_key else {}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as s:
            async with s.get(url.rstrip("/") + "/models", headers=headers) as r:
                if r.status != 200:
                    return web.json_response({"reachable": False, "error": f"HTTP {r.status}", "models": []})
                data = await r.json(content_type=None)
    except Exception as e:
        return web.json_response({"reachable": False, "error": str(e) or type(e).__name__, "models": []})
    return web.json_response({"reachable": True, "models": [m.get("id") for m in data.get("data", []) if m.get("id")]})


DISCOVER_PORTS = {1234: "LM Studio", 1235: "LM Studio / Bionic", 1236: "LM Studio", 41234: "LM Studio / Bionic",
                  41235: "LM Studio / Bionic", 8080: "llama.cpp / llama-swap",
                  8000: "vLLM", 5001: "KoboldCpp", 1337: "Jan", 5000: "text-generation-webui",
                  11434: "Ollama", 11435: "Ollama (своя для ClaudeVoice)", 4891: "GPT4All", 8081: "llama.cpp"}


async def openai_start(request):
    """Start the configured engine now (button in the app)."""
    from . import engine_manager
    svc = _svc(request)
    ok, msg = await asyncio.to_thread(engine_manager.ensure_engine, svc.cfg)
    if ok and svc.cfg.brain == "openai" and hasattr(svc.brain, "warmup"):
        asyncio.get_running_loop().create_task(svc.brain.warmup())
    return web.json_response({"ok": ok, "message": msg, "url": svc.cfg.openai.url})


async def openai_discover(request):
    """Find OpenAI-compatible servers running on this PC (quick probe of the usual ports)."""
    async def probe(s, port, name):
        url = f"http://127.0.0.1:{port}/v1"
        try:
            async with s.get(url + "/models") as r:
                if r.status != 200:
                    return None
                data = await r.json(content_type=None)
                models = [m.get("id") for m in data.get("data", []) if m.get("id")]
                return {"url": url, "port": port, "name": name, "models": models}
        except Exception:
            return None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.5)) as s:
        found = await asyncio.gather(*(probe(s, p, n) for p, n in DISCOVER_PORTS.items()))
    return web.json_response({"engines": [f for f in found if f]})


async def ollama_pull(request):
    """Download a model; progress is streamed back as NDJSON lines."""
    url = _svc(request).cfg.ollama.url
    model = ((await request.json()).get("model") or "").strip()
    if not re.fullmatch(r"[\w.\-]+(/[\w.\-]+)?(:[\w.\-]+)?", model):
        raise web.HTTPBadRequest(text="bad model name")
    resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
    await resp.prepare(request)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=600)) as s:
            async with s.post(f"{url}/api/pull", json={"model": model, "stream": True}) as r:
                async for line in r.content:
                    if line.strip():
                        await resp.write(line if line.endswith(b"\n") else line + b"\n")
    except Exception as e:
        await resp.write((json.dumps({"error": str(e)}) + "\n").encode())
    await resp.write_eof()
    return resp


async def logs(request):
    n = min(int(request.query.get("n", 200)), 2000)
    path = _svc(request).cfg.logs_dir / "voice.log"
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400_000))
            lines = f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        lines = []
    lines = [l for l in lines if "aiohttp.access" not in l and "httpx" not in l]
    return web.json_response({"lines": lines})


async def autostart(request):
    enabled = bool((await request.json()).get("enabled"))
    await asyncio.to_thread(winshell.set_autostart, enabled)
    return web.json_response({"autostart": winshell.autostart_enabled()})


async def firmware_upload(request):
    from .app import OTA_DIR, OTA_FILE, firmware_version
    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "file":
        raise web.HTTPBadRequest(text="no file")
    data = await field.read(decode=False)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as tmp:
        tmp.write(data)
    ver = firmware_version(Path(tmp.name))
    if not ver:
        Path(tmp.name).unlink(missing_ok=True)
        raise web.HTTPBadRequest(text="Это не файл прошивки ESP32 (xiaozhi.bin).")
    OTA_DIR.mkdir(exist_ok=True)
    Path(tmp.name).replace(OTA_DIR / OTA_FILE)
    log.info("firmware %s uploaded from the app (%d bytes)", ver, len(data))
    return web.json_response({"ota_firmware": ver})


# ---------------- skins ----------------
def _skin_url(cfg, skin_id="active"):
    return f"http://{cfg.server.lan_host()}:{cfg.server.port}/skins/{skin_id}.json"


async def skins_list(request):
    from . import skins
    cfg = _svc(request).cfg
    return web.json_response({"active": cfg.skin, "active_palette": cfg.skin_palette, "skins": skins.list_skins(),
                              "palettes": skins.PALETTES, "palette_names": skins.PALETTE_NAMES})


async def skins_save(request):
    """Body: skin fields + optional image (data URL), bg_brightness 0..1, invert auto|yes|no."""
    from . import skins
    body = await request.json()
    try:
        if body.get("image"):
            data = base64.b64decode(body.pop("image").split(",", 1)[-1])
            body["background"] = await asyncio.to_thread(
                skins.image_to_background, data, float(body.pop("bg_brightness", 0.45)), body.pop("invert", "auto"))
        saved = skins.save_skin(body)
    except (ValueError, KeyError, TypeError, OSError) as e:
        raise web.HTTPBadRequest(text=f"Скин не сохранён: {e}")
    return web.json_response({"skin": saved})


async def skins_delete(request):
    from . import skins
    sid = request.match_info["id"]
    if not skins.delete_skin(sid):
        raise web.HTTPNotFound(text="нет такого скина (встроенные удалить нельзя)")
    svc = _svc(request)
    if svc.cfg.skin == sid:
        apply_settings(svc.cfg, {"skin": "classic", "skin_palette": ""})
    return web.json_response({"ok": True})


async def skins_activate(request):
    from . import skins
    svc = _svc(request)
    sid = request.match_info["id"]
    if not skins.get_skin(sid):
        raise web.HTTPNotFound(text="нет такого скина")
    body = await request.json() if request.can_read_body else {}
    palette = (body or {}).get("palette") or ""
    try:
        if palette and not skins.resolve_palette(palette):
            raise ValueError("нет такой палитры")
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))
    apply_settings(svc.cfg, {"skin": sid, "skin_palette": palette})
    applied = False
    dev = svc.devices.current()
    if dev and any(t["name"] == "self.eyes.load_skin" for t in dev.mcp_tools or []):
        try:
            await _device_call(svc, "self.eyes.load_skin", {"url": _skin_url(svc.cfg)}, timeout=15)
            applied = True
        except Exception as e:
            log.warning("load_skin failed: %s", e)
    return web.json_response({"active": sid, "applied_now": applied})


async def skins_export(request):
    from . import skins
    s = skins.get_skin(request.match_info["id"])
    if not s:
        raise web.HTTPNotFound()
    s = {k: v for k, v in s.items() if k not in ("builtin", "updated")}
    return web.Response(text=json.dumps(s, ensure_ascii=False, indent=1), content_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{s["id"]}.cvskin.json"'})


async def device_skin(request):
    """Public: the firmware downloads its skin here (no localhost restriction)."""
    from . import skins
    sid = request.match_info["id"]
    override = ""
    if sid == "active":
        sid = _svc(request).cfg.skin
        override = _svc(request).cfg.skin_palette
    s = skins.get_skin(sid) or skins.get_skin("classic")
    return web.json_response(skins.to_device(s, override))


async def tools_call(request):
    """Settings app: run one of the body's server tools (music, camera, inputs) as the assistant would."""
    from . import server_tools
    body = await request.json()
    name = body.get("name", "")
    if name not in server_tools.TOOLS:
        raise web.HTTPNotFound(text=f"Нет инструмента {name}")
    svc = _svc(request)
    result = await server_tools.call(name, svc.devices, body.get("arguments") or {})
    if name in ("music_play", "music_resume", "music_next"):
        dev = svc.devices.current()
        if dev is not None and not dev.busy():
            dev.maybe_resume_music()      # no spoken answer to wait for: start now
    return web.json_response({"result": result})


# ---------------- hardware profile ----------------
async def hardware_get(request):
    from . import hardware
    return web.json_response({"hardware": hardware.load(), "catalog": hardware.catalog(),
                              "file": str(hardware.path())})


async def hardware_save(request):
    """Validate and store; then ask the device to fetch it (it reboots itself if pins/display changed)."""
    from . import hardware
    body = await request.json()
    try:
        hw = hardware.save(body.get("hardware") or body)
    except (ValueError, TypeError, KeyError) as e:
        raise web.HTTPBadRequest(text=str(e))
    svc = _svc(request)
    applied = "Устройство не на связи: применится при следующем включении."
    dev = svc.devices.current()
    if dev is not None and "self.hw.reload" in [t["name"] for t in (dev.mcp_tools or [])]:
        try:
            applied = str(await _device_call(svc, "self.hw.reload", {"url": hardware_url(svc.cfg)}, timeout=8))
        except Exception as e:
            applied = f"Устройство не ответило: {e}"
    elif dev is not None:
        applied = "Прошивка устройства не умеет применять профиль: обновите прошивку."
    return web.json_response({"hardware": hw, "applied": applied})


async def hardware_reset(request):
    from . import hardware
    try:
        hardware.path().unlink()
    except OSError:
        pass
    return web.json_response({"hardware": hardware.load()})


def hardware_url(cfg) -> str:
    return f"http://{cfg.server.lan_host()}:{cfg.server.port}/device/hardware.json"


async def device_hardware(request):
    """Public: the firmware downloads its hardware profile here."""
    from . import hardware
    return web.json_response(hardware.to_device(hardware.load()))


# ---------------- characters ----------------
async def personas_list(request):
    from . import personas
    return web.json_response({"active": _svc(request).cfg.persona, "personas": personas.list_personas(),
                              "body_rules": personas.BODY_RULES})


async def personas_save(request):
    from . import personas
    try:
        p = personas.save_persona(await request.json())
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response({"persona": p})


async def personas_delete(request):
    from . import personas
    svc, pid = _svc(request), request.match_info["id"]
    if not personas.delete_persona(pid):
        raise web.HTTPNotFound(text="нет такого характера (встроенные удалить нельзя)")
    if svc.cfg.persona == pid:
        apply_settings(svc.cfg, {"persona": "friendly"})
    return web.json_response({"ok": True})


async def personas_activate(request):
    from . import personas
    svc, pid = _svc(request), request.match_info["id"]
    if not any(p["id"] == pid for p in personas.list_personas()):
        raise web.HTTPNotFound(text="нет такого характера")
    apply_settings(svc.cfg, {"persona": pid})
    log.info("persona -> %s", pid)
    return web.json_response({"active": pid})


async def ui_index(request):
    return web.FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-store"})


def setup_routes(app: web.Application):
    app.middlewares.append(local_only)
    r = app.router
    r.add_get("/api/state", state)
    r.add_post("/api/config", update_config)
    r.add_post("/api/device/call", device_call)
    r.add_post("/api/tts/preview", tts_preview)
    r.add_post("/api/brain/test", brain_test)
    r.add_get("/api/ollama", ollama_info)
    r.add_post("/api/ollama/pull", ollama_pull)
    r.add_get("/api/logs", logs)
    r.add_post("/api/autostart", autostart)
    r.add_post("/api/firmware", firmware_upload)
    r.add_get("/api/personas", personas_list)
    r.add_post("/api/personas", personas_save)
    r.add_post("/api/personas/{id}/activate", personas_activate)
    r.add_delete("/api/personas/{id}", personas_delete)
    r.add_get("/api/skins", skins_list)
    r.add_post("/api/skins", skins_save)
    r.add_post("/api/skins/{id}/activate", skins_activate)
    r.add_delete("/api/skins/{id}", skins_delete)
    r.add_get("/api/skins/{id}/export", skins_export)
    r.add_get("/skins/{id}.json", device_skin)
    r.add_get("/api/hardware", hardware_get)
    r.add_post("/api/hardware", hardware_save)
    r.add_post("/api/hardware/reset", hardware_reset)
    r.add_get("/device/hardware.json", device_hardware)
    r.add_post("/api/tools/call", tools_call)
    r.add_get("/api/openai/models", openai_models)
    r.add_get("/api/openai/discover", openai_discover)
    r.add_post("/api/openai/start", openai_start)
    r.add_static("/ui/engine/", ROOT / "proto")
    r.add_get("/ui", ui_index)
    r.add_get("/ui/", ui_index)
    if UI_DIR.exists():
        r.add_static("/ui/static/", UI_DIR)
