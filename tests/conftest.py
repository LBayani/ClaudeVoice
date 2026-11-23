"""Tests run without the GPU stack: torch / silero_vad are stubbed when they are not installed (CI)."""
import contextlib
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import torch  # noqa: F401
except ImportError:
    sys.modules["torch"] = types.SimpleNamespace(no_grad=contextlib.nullcontext, from_numpy=lambda a: a)
try:
    import silero_vad  # noqa: F401
except ImportError:
    sys.modules["silero_vad"] = types.SimpleNamespace(load_silero_vad=lambda: None)
