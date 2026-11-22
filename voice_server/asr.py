"""Speech-to-text with faster-whisper."""
from __future__ import annotations

import logging
import re
import threading

import numpy as np

from .config import AsrCfg

log = logging.getLogger("asr")

# Typical Whisper hallucinations on silence/noise (Russian subtitles corpora).
_HALLUCINATIONS = re.compile(
    r"(продолжение следует|субтитры (сделал|создавал|подготовил)|редактор субтитров|"
    r"спасибо за просмотр|подписывайтесь на канал|dimatorzok|amara\.org)",
    re.IGNORECASE,
)


class Asr:
    def __init__(self, cfg: AsrCfg, models_dir):
        from faster_whisper import WhisperModel

        self.cfg = cfg
        self._lock = threading.Lock()
        log.info("Loading Whisper %s on %s (%s)...", cfg.model, cfg.device, cfg.compute_type)
        self.model = WhisperModel(cfg.model, device=cfg.device, compute_type=cfg.compute_type,
                                  download_root=str(models_dir / "whisper"))
        self.transcribe(np.zeros(16000, np.float32))  # warm-up
        log.info("Whisper ready")

    def transcribe(self, audio: np.ndarray) -> str:
        with self._lock:
            segments, _info = self.model.transcribe(
                audio, language=self.cfg.language, beam_size=self.cfg.beam_size,
                vad_filter=False, condition_on_previous_text=False, without_timestamps=True,
                initial_prompt=self.cfg.initial_prompt or None,
            )
            parts = [s.text for s in segments if s.no_speech_prob < 0.6]
        text = " ".join(p.strip() for p in parts).strip()
        if _HALLUCINATIONS.search(text):
            log.info("Dropped hallucination: %r", text)
            return ""
        return text
