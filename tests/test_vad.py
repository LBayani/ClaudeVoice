import numpy as np

from voice_server import vad
from voice_server.config import VadCfg


class FakeModel:
    """Speech = chunk energy above a threshold (instead of the Silero network)."""

    def reset_states(self):
        pass

    def __call__(self, chunk, sr):
        e = float(np.abs(np.asarray(chunk)).mean())
        return type("P", (), {"item": lambda self: 0.9 if e > 0.1 else 0.0})()


def make_seg(**kw):
    s = vad.Segmenter.__new__(vad.Segmenter)
    s.cfg = VadCfg(**kw)
    s.model = FakeModel()
    s.reset()
    return s


def ms(n, level):
    return np.full(int(16000 * n / 1000), level, np.float32)


def test_segment_after_silence():
    s = make_seg(min_silence_ms=300, min_speech_ms=200)
    ev = s.feed(np.concatenate([ms(200, 0), ms(600, 0.5), ms(400, 0)]))
    kinds = [e for e, _ in ev]
    assert kinds == ["start", "segment"]
    audio = ev[1][1]
    assert 0.6 * 16000 <= len(audio) <= 1.4 * 16000


def test_short_blip_is_noise():
    s = make_seg(min_silence_ms=300, min_speech_ms=250)
    kinds = [e for e, _ in s.feed(np.concatenate([ms(64, 0.5), ms(500, 0)]))]
    assert kinds == ["start", "noise"]


def test_max_utterance_cuts():
    s = make_seg(min_silence_ms=700, max_utterance_ms=1000)
    kinds = [e for e, _ in s.feed(ms(2500, 0.5))]
    assert kinds.count("segment") >= 2


def test_flush_push_to_talk():
    s = make_seg(min_silence_ms=2000, min_speech_ms=200)
    s.feed(ms(500, 0.5))
    assert s.flush() is not None
    assert s.flush() is None
