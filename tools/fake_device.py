"""Virtual xiaozhi device: exercises OTA + WebSocket like the real firmware does.

uv run python tools/fake_device.py [--text "фраза"] [--wav in.wav] [--barge]
Saves what the server said to logs/fake_device_out.wav.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from pathlib import Path

import aiohttp
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voice_server.config import load_config  # noqa: E402
from voice_server.opus_codec import OpusDecoder, OpusEncoder  # noqa: E402


def make_speech(text: str, cfg) -> np.ndarray:
    """Synthesize test speech with the server's own TTS, resampled to 16 kHz float32."""
    import torch
    import torchaudio.functional as AF

    from voice_server.tts import Tts

    cfg.tts.device = "cpu"
    pcm = Tts(cfg.tts, cfg.models_dir).synth(text).astype(np.float32) / 32768
    pcm16k = AF.resample(torch.from_numpy(pcm), cfg.tts.sample_rate, 16000).numpy()
    return pcm16k


def load_wav(path: str) -> np.ndarray:
    with wave.open(path) as w:
        assert w.getnchannels() == 1 and w.getframerate() == 16000, "need 16 kHz mono wav"
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="Привет! Это проверка связи с локальным сервером.")
    ap.add_argument("--wav")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--barge", action="store_true", help="send abort 1 s into the answer")
    ap.add_argument("--stops", type=int, default=2, help="finish after this many tts stops")
    args = ap.parse_args()
    cfg = load_config()

    speech = load_wav(args.wav) if args.wav else make_speech(args.text, cfg)
    audio = np.concatenate([np.zeros(8000, np.float32), speech, np.zeros(16000 * 2, np.float32)])
    enc = OpusEncoder(16000, 60)
    packets = enc.encode(audio) + enc.flush()

    base = f"http://{args.host}:{cfg.server.port}"
    headers = {"Device-Id": "fa:ke:00:00:00:01", "Client-Id": "fake-client", "Protocol-Version": "1"}
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{base}/xiaozhi/ota/", json={"application": {"version": "2.2.4"}},
                             headers=headers) as r:
            ota = await r.json()
        print("OTA ->", {k: v for k, v in ota.items() if k != "websocket"},
              "| ws:", ota["websocket"]["url"])
        ws_url = ota["websocket"]["url"].replace(cfg.server.public_host, args.host)
        headers["Authorization"] = f"Bearer {ota['websocket']['token']}"

        async with http.ws_connect(ws_url, headers=headers) as ws:
            await ws.send_str(json.dumps({"type": "hello", "version": 1, "features": {"mcp": True},
                                          "transport": "websocket",
                                          "audio_params": {"format": "opus", "sample_rate": 16000,
                                                           "channels": 1, "frame_duration": 60}}))
            hello = json.loads((await ws.receive()).data)
            sid = hello.get("session_id")
            out_rate = hello["audio_params"]["sample_rate"]
            print("hello ->", hello)
            dec = OpusDecoder(out_rate)
            received: list[np.ndarray] = []
            t_start = time.monotonic()
            t_speech_end = None
            tts_stops = 0
            speaking = asyncio.Event()

            async def reader():
                nonlocal tts_stops
                async for msg in ws:
                    t = time.monotonic() - t_start
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        received.append(dec.decode(msg.data))
                        continue
                    m = json.loads(msg.data)
                    print(f"  {t:6.2f}s <- {m.get('type')}: "
                          f"{ {k: v for k, v in m.items() if k not in ('type', 'session_id')} }")
                    if m.get("type") == "tts" and m.get("state") == "start":
                        speaking.set()
                    if m.get("type") == "tts" and m.get("state") == "stop":
                        speaking.clear()
                        tts_stops += 1
                        if tts_stops == args.stops:  # greeting + (filler) + answer
                            return

            rtask = asyncio.create_task(reader())
            # Wake word -> greeting, wait for it to finish (device does the same)
            await ws.send_str(json.dumps({"session_id": sid, "type": "listen", "state": "detect", "text": "тест"}))
            await asyncio.sleep(0.2)
            while speaking.is_set():
                await asyncio.sleep(0.05)
            await ws.send_str(json.dumps({"session_id": sid, "type": "listen", "state": "start", "mode": "auto"}))
            print(f"  {time.monotonic() - t_start:6.2f}s -> streaming {len(packets)} frames")
            speech_end_frame = (8000 + len(speech)) // 960
            for i, p in enumerate(packets):
                if speaking.is_set():
                    break  # real device stops sending while it speaks
                await ws.send_bytes(p)
                if i == speech_end_frame:
                    t_speech_end = time.monotonic()
                await asyncio.sleep(0.06)
            if args.barge:
                await speaking.wait()
                await asyncio.sleep(1.0)
                print(f"  {time.monotonic() - t_start:6.2f}s -> abort (barge-in)")
                await ws.send_str(json.dumps({"session_id": sid, "type": "abort", "reason": "wake_word_detected"}))
            try:
                await asyncio.wait_for(rtask, 60)
            except asyncio.TimeoutError:
                print("timeout waiting for answer")

    if t_speech_end:
        print(f"Speech ended at frame {speech_end_frame}; see log for ASR/TTS timings.")
    out = np.concatenate(received) if received else np.zeros(0, np.float32)
    path = ROOT / "logs" / "fake_device_out.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(out_rate)
        w.writeframes((np.clip(out, -1, 1) * 32767).astype(np.int16).tobytes())
    print(f"Received {len(out) / out_rate:.1f}s of audio -> {path}")


if __name__ == "__main__":
    asyncio.run(main())
