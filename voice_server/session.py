"""One WebSocket connection with a xiaozhi device (protocol v1, firmware 2.x)."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import uuid
from typing import AsyncIterator

import numpy as np
from aiohttp import WSMsgType, web

from .mcp_bridge import McpClientMixin
from .opus_codec import OpusDecoder, OpusEncoder
from .speakable import split_sentences, to_speakable
from .vad import Segmenter

log = logging.getLogger("session")

FRAME_MS = 60


class Pacer:
    """Sends Opus frames roughly in real time, keeping a small lead so the device buffer never starves."""

    def __init__(self, ws: web.WebSocketResponse, frame_s: float = FRAME_MS / 1000, lead_s: float = 0.4):
        self.ws, self.frame_s, self.lead_s = ws, frame_s, lead_s
        self.t0: float | None = None
        self.sent = 0.0

    async def send(self, pkt: bytes):
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self.t0 is None:
            self.t0 = now
        if now > self.t0 + self.sent:  # we fell behind (slow TTS): restart the timeline from now
            self.t0 = now - self.sent
        wait = self.t0 + self.sent - self.lead_s - now
        if wait > 0:
            await asyncio.sleep(wait)
        await self.ws.send_bytes(pkt)
        self.sent += self.frame_s

    async def drain(self):
        if self.t0 is None:
            return
        wait = self.t0 + self.sent - asyncio.get_running_loop().time()
        if wait > 0:
            await asyncio.sleep(wait)


class DeviceSession(McpClientMixin):
    def __init__(self, ws: web.WebSocketResponse, svc, device_id: str):
        self._mcp_setup()
        self.ws, self.svc, self.device_id = ws, svc, device_id
        self.cfg = svc.cfg
        self.session_id = uuid.uuid4().hex[:12]
        self.decoder = OpusDecoder(16000)
        self.seg = Segmenter(self.cfg.vad)
        self.listening = False
        self.mode = "auto"
        self.speaking = False
        self.task: asyncio.Task | None = None
        self.last_activity = time.monotonic()
        self.last_sent = time.monotonic()
        self._asr_tasks: set[asyncio.Task] = set()
        self.music_task: asyncio.Task | None = None
        self.music_pending = False       # a tool asked for music: start it when the answer is over

    # ---------- plumbing ----------
    def touch(self):
        self.last_activity = time.monotonic()

    async def send_json(self, obj: dict):
        if self.ws.closed:
            return
        obj.setdefault("session_id", self.session_id)
        await self.ws.send_str(json.dumps(obj, ensure_ascii=False))
        self.last_sent = time.monotonic()

    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    async def cancel_task(self):
        if self.busy():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass

    async def start_task(self, coro):
        await self.cancel_task()
        await self.pause_music()       # the device talks: music waits
        self.task = asyncio.create_task(self._guard(coro))

    async def _guard(self, coro):
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[%s] response failed", self.session_id)
        finally:
            self.touch()
        self.maybe_resume_music()

    # ---------- music ----------
    async def pause_music(self):
        t = self.music_task
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    def maybe_resume_music(self):
        """After an answer: start music a tool asked for, or resume music the conversation interrupted."""
        m = getattr(self.svc, "music", None)
        if m is None or self.ws.closed or self.busy():
            return
        if m.st.state == "interrupted" and (self.music_pending or m.st.queue):
            self.music_pending = False
            self.music_task = asyncio.create_task(m.play(self))

    # ---------- main loop ----------
    async def run(self):
        log.info("[%s] device %s connected", self.session_id, self.device_id)
        self.svc.devices.add(self)
        watchdog = asyncio.create_task(self._watchdog())
        try:
            async for msg in self.ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        log.warning("[%s] bad json: %r", self.session_id, msg.data[:200])
                        continue
                    await self.on_json(data)
                elif msg.type == WSMsgType.BINARY:
                    await self.on_audio(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.svc.devices.remove(self)
            watchdog.cancel()
            await self.pause_music()
            for t in list(self._asr_tasks):
                t.cancel()
            await self.cancel_task()
            log.info("[%s] disconnected", self.session_id)

    async def _watchdog(self):
        while not self.ws.closed:
            await asyncio.sleep(1)
            now = time.monotonic()
            if self.busy():
                if now - self.last_sent > self.cfg.session.keepalive_s:  # device drops silent channels at 120 s
                    await self.send_json({"type": "llm", "emotion": "thinking", "text": "🤔"})
                continue
            if self.listening and not self.seg.in_speech and \
                    now - self.last_activity > self.cfg.session.idle_timeout_s:
                log.info("[%s] idle %.0fs -> closing channel", self.session_id, now - self.last_activity)
                await self.ws.close()
                return

    async def on_json(self, m: dict):
        t = m.get("type")
        log.debug("[%s] <- %s", self.session_id, m)
        if t == "hello":
            await self.send_json({
                "type": "hello", "transport": "websocket",
                "audio_params": {"format": "opus", "sample_rate": self.svc.tts.sample_rate,
                                 "channels": 1, "frame_duration": FRAME_MS},
            })
            if (m.get("features") or {}).get("mcp"):
                asyncio.create_task(self.mcp_discover())
        elif t == "listen":
            state = m.get("state")
            if state == "start":
                await self.pause_music()
                self.listening, self.mode = True, m.get("mode", "auto")
                self.seg.reset()
                self.touch()
                log.info("[%s] listening (%s)", self.session_id, self.mode)
            elif state == "stop":
                self.listening = False
                audio = self.seg.flush()
                if audio is not None:
                    t = asyncio.create_task(self.on_utterance(audio))
                    self._asr_tasks.add(t)
                    t.add_done_callback(self._asr_tasks.discard)
            elif state == "detect":
                log.info("[%s] wake word: %s", self.session_id, m.get("text"))
                self.touch()
                text = str(m.get("text") or "")
                if text.startswith("cv_event:"):          # an input with the "event" action woke us up
                    _, name, gesture = (text.split(":", 2) + ["", ""])[:3]
                    await self.on_device_event(name, gesture)
                elif self.svc.greetings:
                    text, pcm = random.choice(self.svc.greetings)
                    await self.start_task(self.speak_pcm(text, pcm))
        elif t == "abort":
            log.info("[%s] abort (%s)", self.session_id, m.get("reason", "button"))
            await self.cancel_task()
            await self.pause_music()
            if self.speaking:
                await self.send_json({"type": "tts", "state": "stop"})
                self.speaking = False
        elif t == "mcp":
            payload = m.get("payload")
            if isinstance(payload, dict):
                if payload.get("method") == "notifications/cv_event":
                    prm = payload.get("params") or {}
                    await self.on_device_event(str(prm.get("input", ""))[:40], str(prm.get("gesture", ""))[:10])
                else:
                    self.mcp_incoming(payload)
        else:
            log.info("[%s] unhandled message: %s", self.session_id, m)

    GESTURE_RU = {"tap": "коснулся", "double": "дважды коснулся", "hold": "держит"}

    async def on_device_event(self, name: str, gesture: str):
        """The user touched an antenna / pressed a button whose action is "event": let the assistant react."""
        log.info("[%s] device event: %s %s", self.session_id, name, gesture)
        if self.busy():
            return
        what = self.GESTURE_RU.get(gesture, gesture)
        await self.start_task(self.respond(
            f"(Это не речь, а событие тела устройства: пользователь {what} «{name}». "
            f"Отреагируй коротко, одной-двумя фразами, в своём характере.)"))

    async def on_audio(self, data: bytes):
        if not self.listening or self.speaking:
            return
        try:
            pcm = self.decoder.decode(data)
        except Exception as e:
            log.debug("opus decode error: %s", e)
            return
        for ev, audio in self.seg.feed(pcm):
            self.touch()
            if ev == "start":
                log.debug("[%s] speech start", self.session_id)
            elif ev == "segment":
                log.info("[%s] utterance %.1fs", self.session_id, len(audio) / 16000)
                # Recognize first; only real words (not noise) may interrupt the current answer.
                t = asyncio.create_task(self.on_utterance(audio))
                self._asr_tasks.add(t)
                t.add_done_callback(self._asr_tasks.discard)

    # ---------- responses ----------
    def _is_stop(self, text: str) -> bool:
        words = re.sub(r"[^\w\s]", " ", text.lower()).split()
        return 0 < len(words) <= 3 and any(w in self.cfg.session.stop_words for w in words)

    async def on_utterance(self, audio: np.ndarray):
        t0 = time.monotonic()
        text = await asyncio.to_thread(self.svc.asr.transcribe, audio)
        log.info("[%s] ASR %.2fs: %r", self.session_id, time.monotonic() - t0, text)
        if not text or self.ws.closed:
            return
        self.touch()
        await self.send_json({"type": "stt", "text": text})
        if self._is_stop(text):
            m = getattr(self.svc, "music", None)
            was_busy = self.busy() or (m is not None and m.st.state in ("playing", "interrupted"))
            if m is not None:
                m.st.state = "stopped"      # "стоп" also stops the music
            await self.cancel_task()
            await self.pause_music()
            phrase = "Остановил." if was_busy else "Хорошо."
            pcm = self.svc.phrases.get(phrase)
            if pcm is not None:
                await self.start_task(self.speak_pcm(phrase, pcm))
            return
        if self.busy():
            log.info("[%s] new question interrupts the previous answer", self.session_id)
        await self.start_task(self.respond(text))

    async def _device_context(self) -> str:
        """A one-line briefing about the device's body (antennae mood) for Claude."""
        if not self.mcp_tools or not any(t["name"] == "self.antennae.get_state" for t in self.mcp_tools):
            return ""
        try:
            res = await self.mcp_call("tools/call", {"name": "self.antennae.get_state", "arguments": {}}, timeout=3)
            st = json.loads(res["content"][0]["text"])
        except Exception as e:
            log.debug("antennae state unavailable: %s", e)
            return ""
        mins, aff = st.get("minutes_since_last_touch", 0), st.get("affection", 0.5)
        mood = "счастлив" if aff > 0.7 else "доволен" if aff > 0.4 else "скучает по прикосновениям"
        now = "антенки трогают прямо сейчас; " if st.get("touched_now") else ""
        return (f"[Состояние тела: {now}антенки трогали {mins:.0f} мин назад, привязанность {aff:.2f} из 1, "
                f"устройство {mood}.]")

    async def respond(self, text: str):
        await self.send_json({"type": "llm", "emotion": "thinking", "text": "🤔"})
        context = await self._device_context()
        await self.speak_stream(self._with_filler(self.svc.brain.respond(text, context)))

    async def _with_filler(self, gen):
        """Pass chunks through; if the first one is slow, say a short filler phrase meanwhile."""
        it = gen.__aiter__()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cfg.session.filler_after_s
        filler_done = not self.svc.fillers
        text_seen = False
        pending: asyncio.Future | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(it.__anext__())
                timeout = None if (filler_done or text_seen) else max(0.0, deadline - loop.time())
                done, _ = await asyncio.wait({pending}, timeout=timeout)
                if not done:  # nothing to say yet: fill the silence once
                    filler_done = True
                    text, pcm = random.choice(self.svc.fillers)
                    await self.speak_pcm(text, pcm)
                    await self.send_json({"type": "llm", "emotion": "thinking", "text": "🤔"})
                    continue
                try:
                    chunk = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                if chunk is None and not text_seen:
                    continue  # tool use before any speech: keep thinking quietly
                text_seen = text_seen or chunk is not None
                yield chunk
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                try:
                    await pending
                except (asyncio.CancelledError, StopAsyncIteration, Exception):
                    pass
            await gen.aclose()   # kills the Claude process if still running

    async def _begin_speaking(self):
        if not self.speaking:
            self.speaking = True
            await self.send_json({"type": "tts", "state": "start"})

    async def _end_speaking(self):
        if self.speaking:
            self.speaking = False
            await self.send_json({"type": "tts", "state": "stop"})

    async def speak_pcm(self, text: str, pcm: np.ndarray):
        enc, pacer = OpusEncoder(self.svc.tts.sample_rate, FRAME_MS), Pacer(self.ws)
        try:
            await self._begin_speaking()
            await self.send_json({"type": "tts", "state": "sentence_start", "text": text})
            for p in enc.encode(pcm) + enc.flush():
                await pacer.send(p)
            await pacer.drain()
        finally:
            await self._end_speaking()

    async def _pause_speaking(self, enc: OpusEncoder, pacer: Pacer):
        """Assistant went off to work: finish the audio, let the device listen/think meanwhile."""
        if self.speaking:
            for p in enc.flush():
                await pacer.send(p)
            await pacer.drain()
            await self._end_speaking()
        await self.send_json({"type": "llm", "emotion": "thinking", "text": "🤔"})

    async def speak_stream(self, chunks: AsyncIterator[str | None]):
        enc, pacer = OpusEncoder(self.svc.tts.sample_rate, FRAME_MS), Pacer(self.ws)
        first = True
        try:
            async for chunk in chunks:
                if chunk is None:
                    await self._pause_speaking(enc, pacer)
                    enc, pacer = OpusEncoder(self.svc.tts.sample_rate, FRAME_MS), Pacer(self.ws)
                    continue
                for sentence in split_sentences(to_speakable(chunk)):
                    t0 = time.monotonic()
                    pcm = await asyncio.to_thread(self.svc.tts.synth, sentence)
                    if not len(pcm):
                        continue
                    if first:
                        log.info("[%s] first audio after TTS %.2fs", self.session_id, time.monotonic() - t0)
                        first = False
                    if not self.speaking:
                        await self.send_json({"type": "llm", "emotion": "neutral", "text": "🙂"})
                    await self._begin_speaking()
                    await self.send_json({"type": "tts", "state": "sentence_start", "text": sentence})
                    for p in enc.encode(pcm):
                        await pacer.send(p)
            if self.speaking:
                for p in enc.flush():
                    await pacer.send(p)
                await pacer.drain()
        finally:
            if hasattr(chunks, "aclose"):
                await chunks.aclose()
            await self._end_speaking()
