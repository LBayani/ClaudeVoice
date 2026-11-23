"""Russian text-to-speech with Silero TTS (standalone torch.package model)."""
from __future__ import annotations

import logging
import threading
import urllib.request
from pathlib import Path

import numpy as np
import torch

from .config import TtsCfg

log = logging.getLogger("tts")


class Tts:
    def __init__(self, cfg: TtsCfg, models_dir: Path):
        self.cfg = cfg
        self.sample_rate = cfg.sample_rate
        self._lock = threading.Lock()
        path = models_dir / "silero" / Path(cfg.model_url).name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            log.info("Downloading Silero model %s ...", cfg.model_url)
            urllib.request.urlretrieve(cfg.model_url, path)
        self.model = torch.package.PackageImporter(str(path)).load_pickle("tts_models", "model")
        self.model.to(torch.device(cfg.device))
        self.synth("Проверка.")  # warm-up
        log.info("Silero TTS ready (speaker=%s, %d Hz)", cfg.speaker, cfg.sample_rate)

    def synth(self, text: str, speaker: str | None = None) -> np.ndarray:
        """Returns int16 mono PCM at cfg.sample_rate; empty array if the model can't read it.
        speaker: another voice for this one phrase (the app's preview); the config stays as is."""
        with self._lock:
            try:
                audio = self.model.apply_tts(text=text, speaker=speaker or self.cfg.speaker,
                                             sample_rate=self.sample_rate,
                                             put_accent=True, put_yo=True)
            except Exception as e:  # Silero raises on unreadable input
                log.warning("TTS failed for %r: %s", text, e)
                return np.zeros(0, np.int16)
        pcm = audio.detach().cpu().numpy()
        return (np.clip(pcm, -1, 1) * 32767).astype(np.int16)
