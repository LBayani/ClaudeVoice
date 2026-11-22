"""Music on the device speaker: YouTube / YouTube Music (yt-dlp), Yandex Music (your token),
internet radio (radio-browser catalogue or any stream URL) and Spotify (Spotify Connect via librespot).

Audio is decoded on the server (PyAV), mixed down to 24 kHz mono and sent to the device as the same
Opus stream as speech. Talking to the device pauses the music; after the answer it resumes unless the
user asked to stop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from . import server_tools

if TYPE_CHECKING:
    from .config import Config
    from .session import DeviceSession

log = logging.getLogger("music")
SOURCES = {"youtube": "YouTube / YouTube Music", "yandex": "Яндекс Музыка", "radio": "Интернет-радио",
           "spotify": "Spotify Connect"}
RADIO_API = "https://all.api.radio-browser.info/json/stations/search"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class Track:
    title: str
    source: str
    url: str = ""                 # direct stream URL (resolved lazily for Yandex)
    ref: str = ""                 # page / id to re-resolve expired links
    seekable: bool = True
    duration: float = 0.0


@dataclass
class PlayerState:
    queue: list[Track] = field(default_factory=list)
    index: int = 0
    position: float = 0.0         # seconds into the current track
    state: str = "stopped"        # stopped | playing | interrupted
    wave: bool = False            # Yandex "My Wave": fetch more tracks when the queue ends
    error: str = ""

    def current(self) -> Track | None:
        return self.queue[self.index] if 0 <= self.index < len(self.queue) else None


class Music:
    def __init__(self, cfg: "Config"):
        self.cfg = cfg
        self.st = PlayerState()
        self._yandex = None
        self._spotify: subprocess.Popen | None = None

    # ---------------- search / resolve (blocking, run in threads) ----------------
    def search(self, query: str, source: str) -> list[Track]:
        source = source if source in SOURCES else self.cfg.music.default_source
        if source == "radio":
            return self._radio(query)
        if source == "yandex":
            return self._yandex_search(query)
        if source == "spotify":
            return [Track("Spotify Connect", "spotify", "pipe:", seekable=False)]
        return self._youtube(query)

    def _youtube(self, query: str) -> list[Track]:
        import yt_dlp
        q = query if query.startswith("http") else f"ytsearch5:{query}"
        opts = {"format": "bestaudio/best", "quiet": True, "no_warnings": True, "noplaylist": True,
                "extract_flat": "in_playlist"}
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(q, download=False)
        entries = info.get("entries") or [info]
        out = []
        for e in entries[:5]:
            if not e:
                continue
            out.append(Track(e.get("title") or "трек", "youtube", e.get("url") if e.get("acodec") else "",
                             e.get("webpage_url") or e.get("url") or "", True, float(e.get("duration") or 0)))
        return out

    def _youtube_stream(self, t: Track) -> str:
        import yt_dlp
        with yt_dlp.YoutubeDL({"format": "bestaudio/best", "quiet": True, "no_warnings": True,
                               "noplaylist": True}) as y:
            info = y.extract_info(t.ref, download=False)
        t.duration = float(info.get("duration") or t.duration)
        return info["url"]

    def _radio(self, query: str) -> list[Track]:
        if query.startswith("http"):
            return [Track(query, "radio", query, query, False)]
        params = urllib.parse.urlencode({"name": query, "limit": 5, "hidebroken": "true", "order": "votes",
                                         "reverse": "true"})
        req = urllib.request.Request(f"{RADIO_API}?{params}", headers={"User-Agent": "ClaudeVoice/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            stations = json.loads(r.read().decode("utf-8"))
        return [Track(s["name"].strip(), "radio", s.get("url_resolved") or s["url"], s.get("stationuuid", ""), False)
                for s in stations if s.get("url_resolved") or s.get("url")]

    def _yandex_client(self):
        if not self.cfg.music.yandex_token:
            raise RuntimeError("не задан токен Яндекс Музыки (Настройки → Музыка)")
        if self._yandex is None:
            from yandex_music import Client
            self._yandex = Client(self.cfg.music.yandex_token).init()
        return self._yandex

    def _yandex_search(self, query: str) -> list[Track]:
        c = self._yandex_client()
        if is_wave(query):
            return self._yandex_wave()
        res = c.search(query, type_="track")
        tracks = (res.tracks.results if res and res.tracks else [])[:5]
        return [Track(f"{', '.join(a.name for a in t.artists)} — {t.title}", "yandex", "", f"{t.id}:{t.albums[0].id}"
                      if t.albums else str(t.id), True, (t.duration_ms or 0) / 1000) for t in tracks]

    def _yandex_wave(self) -> list[Track]:
        c = self._yandex_client()
        seq = c.rotor_station_tracks("user:onyourwave").sequence
        return [Track(f"{', '.join(a.name for a in s.track.artists)} — {s.track.title}", "yandex", "",
                      str(s.track.id), True, (s.track.duration_ms or 0) / 1000) for s in seq]

    def _yandex_stream(self, t: Track) -> str:
        c = self._yandex_client()
        track = c.tracks([t.ref])[0]
        info = sorted(track.get_download_info(), key=lambda i: i.bitrate_in_kbps, reverse=True)
        mp3 = next((i for i in info if i.codec == "mp3"), info[0])
        return mp3.get_direct_link()

    def stream_url(self, t: Track) -> str:
        if t.source == "youtube" and (not t.url or t.ref):
            t.url = self._youtube_stream(t)
        elif t.source == "yandex":
            t.url = self._yandex_stream(t)
        return t.url

    # ---------------- Spotify Connect (librespot) ----------------
    def spotify_process(self) -> subprocess.Popen:
        if self._spotify and self._spotify.poll() is None:
            return self._spotify
        exe = self.cfg.music.librespot_path or shutil.which("librespot") or ""
        if not exe or not os.path.exists(exe):
            raise RuntimeError("не найден librespot.exe: укажите путь в Настройки → Музыка → Spotify")
        self._spotify = subprocess.Popen(
            [exe, "--name", self.cfg.music.spotify_name or "ClaudeVoice", "--backend", "pipe",
             "--bitrate", "160", "--initial-volume", "100", "--disable-audio-cache", "--quiet"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        log.info("librespot started: device '%s' is visible in the Spotify app", self.cfg.music.spotify_name)
        return self._spotify

    def stop_spotify(self):
        if self._spotify and self._spotify.poll() is None:
            self._spotify.terminate()
        self._spotify = None

    # ---------------- playback (called by the device session) ----------------
    async def play(self, session: "DeviceSession"):
        """Stream the queue to the device. Cancelled when the user talks (state -> interrupted)."""
        from .opus_codec import OpusEncoder
        from .session import FRAME_MS, Pacer
        st = self.st
        st.state = "playing"
        try:
            while True:
                t = st.current()
                if t is None:
                    if st.wave and st.queue:
                        more = await asyncio.to_thread(self._yandex_wave)
                        st.queue += more
                        continue
                    st.state = "stopped"
                    return
                log.info("playing [%s] %s from %.0fs", t.source, t.title, st.position)
                await session.send_json({"type": "llm", "emotion": "happy", "text": "🎵"})
                await session._begin_speaking()
                await session.send_json({"type": "tts", "state": "sentence_start", "text": f"♪ {t.title}"})
                enc = OpusEncoder(session.svc.tts.sample_rate, FRAME_MS, bitrate=self.cfg.music.bitrate)
                pacer = Pacer(session.ws, lead_s=0.6)
                async for pcm in self._pcm(t, session.svc.tts.sample_rate):
                    for p in enc.encode(pcm * self.cfg.music.volume):
                        await pacer.send(p)
                        st.position += FRAME_MS / 1000
                    session.touch()
                for p in enc.flush():
                    await pacer.send(p)
                await pacer.drain()
                if t.source == "spotify":     # librespot ended
                    st.state = "stopped"
                    return
                st.index += 1
                st.position = 0.0
        except asyncio.CancelledError:
            if st.state == "playing":
                st.state = "interrupted"   # resumes after the answer
            raise
        except Exception as e:
            st.state, st.error = "stopped", str(e)
            log.warning("music failed: %s", e)
        finally:
            await session._end_speaking()

    async def _pcm(self, t: Track, rate: int):
        """Async generator of float32 mono chunks at `rate`, produced by a decoder thread."""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        stop = threading.Event()
        start = self.st.position if t.seekable else 0.0

        def put(item):
            if stop.is_set() and item is not None:
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, item)
            except RuntimeError:      # loop closed: playback is over
                stop.set()

        def worker():
            import av
            try:
                if t.source == "spotify":
                    proc = self.spotify_process()
                    res = av.AudioResampler(format="flt", layout="mono", rate=rate)
                    while not stop.is_set():
                        raw = proc.stdout.read(44100 * 4 // 10)   # 100 ms of s16le stereo
                        if not raw:
                            break
                        a = np.frombuffer(raw, np.int16).reshape(1, -1)
                        fr = av.AudioFrame.from_ndarray(a, format="s16", layout="stereo")
                        fr.sample_rate = 44100
                        for o in res.resample(fr):
                            put(o.to_ndarray().reshape(-1).astype(np.float32))
                    return
                url = self.stream_url(t)
                opts = {"reconnect": "1", "reconnect_streamed": "1", "reconnect_delay_max": "5",
                        "user_agent": "Mozilla/5.0"}
                with av.open(url, options=opts, timeout=15) as c:
                    s = c.streams.audio[0]
                    if start > 1:
                        c.seek(int(start / s.time_base), stream=s)
                    res = av.AudioResampler(format="flt", layout="mono", rate=rate)
                    for frame in c.decode(s):
                        if stop.is_set():
                            break
                        for o in res.resample(frame):
                            while q.qsize() > 48 and not stop.is_set():   # don't run far ahead
                                time.sleep(0.05)
                            put(o.to_ndarray().reshape(-1).astype(np.float32))
            except Exception as e:
                put(e)
            finally:
                put(None)

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        try:
            while True:
                item = await q.get()
                if item is None:
                    return
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            stop.set()


def is_wave(query: str) -> bool:
    return query.lower().strip(" .!") in ("моя волна", "мою волну", "волна", "волну", "my wave")


# ---------------- tools for the assistant ----------------
_music: Music | None = None


def attach(cfg: "Config") -> Music:
    global _music
    _music = Music(cfg)
    return _music


def _session(devices):
    dev = devices.current()
    if dev is None:
        raise RuntimeError("устройство не на связи")
    return dev


async def music_play(devices, query: str, source: str = "") -> str:
    m = _music
    if m is None or not m.cfg.music.enabled:
        return "Музыка выключена в настройках."
    dev = _session(devices)
    tracks = await asyncio.to_thread(m.search, query, source or m.cfg.music.default_source)
    if not tracks:
        return f"Ничего не нашёл по запросу «{query}»."
    m.st = PlayerState(queue=tracks, wave=tracks[0].source == "yandex" and is_wave(query))
    m.st.state = "interrupted"     # the session starts it right after the spoken answer
    dev.music_pending = True
    return f"Включаю: {tracks[0].title} ({SOURCES[tracks[0].source]}). Начнёт играть после твоего ответа."


async def music_stop(devices) -> str:
    if _music:
        _music.st.state = "stopped"
        _music.st.wave = False
    dev = devices.current()
    if dev and getattr(dev, "music_task", None):
        dev.music_task.cancel()
    return "Музыка остановлена."


async def music_resume(devices) -> str:
    m = _music
    if not m or not m.st.queue:
        return "Нечего продолжать."
    m.st.state = "interrupted"
    _session(devices).music_pending = True
    return f"Продолжаю: {m.st.current().title if m.st.current() else ''}"


async def music_next(devices) -> str:
    m = _music
    if not m or not m.st.queue:
        return "Очередь пуста."
    m.st.index += 1
    m.st.position = 0.0
    m.st.state = "interrupted"
    _session(devices).music_pending = True
    t = m.st.current()
    return f"Следующий: {t.title}" if t else "Это был последний трек."


async def music_status(devices) -> str:
    m = _music
    if not m:
        return "Музыка выключена."
    t = m.st.current()
    return json.dumps({"state": m.st.state, "track": t.title if t else None, "source": t.source if t else None,
                       "position_s": round(m.st.position), "queue": [x.title for x in m.st.queue[m.st.index:][:5]],
                       "sources": SOURCES, "default_source": m.cfg.music.default_source}, ensure_ascii=False)


server_tools.register(
    "music_play", music_play,
    "Включить музыку на динамике устройства. `query`: что искать (исполнитель, песня, жанр, название станции, "
    "«моя волна» для Яндекса или прямая ссылка). `source`: youtube, yandex, radio, spotify или пусто "
    "(источник по умолчанию из настроек). Музыка начнёт играть после твоего ответа, поэтому ответь очень коротко.",
    {"query": {"type": "string"}, "source": {"type": "string", "enum": ["", *SOURCES]}}, ["query"])
server_tools.register("music_stop", music_stop, "Остановить музыку.")
server_tools.register("music_resume", music_resume, "Продолжить музыку с того места, где остановилась.")
server_tools.register("music_next", music_next, "Следующий трек в очереди.")
server_tools.register("music_status", music_status, "Что сейчас играет, очередь и доступные источники музыки.")
