"""Raw Opus packet encode/decode via PyAV (bundled libopus), mono only."""
from __future__ import annotations

import av
import numpy as np


class OpusDecoder:
    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self.ctx = av.CodecContext.create("libopus", "r")
        self.ctx.sample_rate = sample_rate
        self.ctx.layout = "mono"
        # FFmpeg's Opus decoders always output 48 kHz; convert to the rate we want.
        self.resampler = av.AudioResampler(format="flt", layout="mono", rate=sample_rate)

    def decode(self, packet: bytes) -> np.ndarray:
        """Decode one Opus packet -> float32 mono PCM in [-1, 1] at self.sample_rate."""
        out = []
        for frame in self.ctx.decode(av.Packet(packet)):
            for rf in self.resampler.resample(frame):
                out.append(rf.to_ndarray().reshape(-1).astype(np.float32))
        return np.concatenate(out) if out else np.zeros(0, np.float32)


class OpusEncoder:
    """Stateful encoder for one utterance stream. Call flush() at the end."""

    def __init__(self, sample_rate: int = 24000, frame_ms: int = 60, bitrate: int = 32000):
        self.sample_rate = sample_rate
        self.frame_samples = sample_rate * frame_ms // 1000
        self.ctx = av.CodecContext.create("libopus", "w")
        self.ctx.sample_rate = sample_rate
        self.ctx.layout = "mono"
        self.ctx.format = "s16"
        self.ctx.bit_rate = bitrate
        self.ctx.options = {"frame_duration": str(frame_ms), "application": "audio"}
        self.ctx.open()
        self._pending = np.zeros(0, np.int16)
        self._pts = 0

    def _encode_frame(self, samples: np.ndarray) -> list[bytes]:
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = self.sample_rate
        frame.pts = self._pts
        self._pts += len(samples)
        return [bytes(p) for p in self.ctx.encode(frame)]

    def encode(self, pcm: np.ndarray) -> list[bytes]:
        """pcm: int16 or float32 mono. Returns zero or more Opus packets."""
        if pcm.dtype != np.int16:
            pcm = (np.clip(pcm, -1, 1) * 32767).astype(np.int16)
        buf = np.concatenate([self._pending, pcm])
        packets: list[bytes] = []
        n = self.frame_samples
        full = len(buf) // n * n
        for i in range(0, full, n):
            packets += self._encode_frame(buf[i:i + n])
        self._pending = buf[full:]
        return packets

    def flush(self) -> list[bytes]:
        packets: list[bytes] = []
        if len(self._pending):
            pad = np.zeros(self.frame_samples - len(self._pending), np.int16)
            packets += self._encode_frame(np.concatenate([self._pending, pad]))
            self._pending = np.zeros(0, np.int16)
        packets += [bytes(p) for p in self.ctx.encode(None)]
        return packets
