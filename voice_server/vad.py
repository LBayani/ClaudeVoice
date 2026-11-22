"""Silero VAD based utterance segmenter (16 kHz)."""
from __future__ import annotations

import numpy as np
import torch
from silero_vad import load_silero_vad

from .config import VadCfg

SR = 16000
CHUNK = 512  # silero-vad window at 16 kHz (32 ms)


class Segmenter:
    """Feed PCM; get back ('start', None) when speech begins and ('segment', audio) when it ends."""

    def __init__(self, cfg: VadCfg):
        self.cfg = cfg
        self.model = load_silero_vad()
        self.reset()

    def reset(self):
        self.model.reset_states()
        self._buf = np.zeros(0, np.float32)
        self._pre: list[np.ndarray] = []
        self._speech: list[np.ndarray] = []
        self._in_speech = False
        self._silence_ms = 0.0
        self._speech_ms = 0.0

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def flush(self):
        """End of push-to-talk: return the pending utterance, if any."""
        audio = np.concatenate(self._speech) if self._in_speech and self._speech else None
        voiced_ms = self._speech_ms - self._silence_ms
        self.reset()
        if audio is not None and voiced_ms >= self.cfg.min_speech_ms:
            return audio
        return None

    def feed(self, pcm: np.ndarray):
        events = []
        self._buf = np.concatenate([self._buf, pcm])
        chunk_ms = CHUNK * 1000 / SR
        pre_chunks = max(1, int(self.cfg.pre_roll_ms / chunk_ms))
        while len(self._buf) >= CHUNK:
            chunk, self._buf = self._buf[:CHUNK], self._buf[CHUNK:]
            with torch.no_grad():
                prob = self.model(torch.from_numpy(chunk), SR).item()
            speech = prob >= self.cfg.threshold
            if not self._in_speech:
                self._pre.append(chunk)
                self._pre = self._pre[-pre_chunks:]
                if speech:
                    self._in_speech = True
                    self._speech = list(self._pre)
                    self._pre = []
                    self._speech_ms = chunk_ms
                    self._silence_ms = 0.0
                    events.append(("start", None))
                continue
            self._speech.append(chunk)
            self._speech_ms += chunk_ms
            self._silence_ms = 0.0 if speech else self._silence_ms + chunk_ms
            ended = self._silence_ms >= self.cfg.min_silence_ms
            too_long = self._speech_ms >= self.cfg.max_utterance_ms
            if ended or too_long:
                audio = np.concatenate(self._speech)
                voiced_ms = self._speech_ms - self._silence_ms
                self._in_speech = False
                self._speech = []
                if voiced_ms >= self.cfg.min_speech_ms:
                    events.append(("segment", audio))
                else:
                    events.append(("noise", None))
        return events
