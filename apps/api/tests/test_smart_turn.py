"""S13-A: smart-turn-v3 detector tests.

These tests actually load the ONNX model — SKIP if unavailable so CI
without model cache still passes.
"""
import numpy as np
import pytest


try:
    from packages.runtime.smart_turn import SmartTurnDetector, predict_end_of_turn
    _MODEL_AVAILABLE = True
except ImportError:
    _MODEL_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not _MODEL_AVAILABLE, reason="smart-turn deps unavailable"
)


def _make_pcm(seconds: float, fill: str = "silence") -> bytes:
    n = int(16000 * seconds)
    if fill == "silence":
        return np.zeros(n, dtype=np.int16).tobytes()
    if fill == "noise":
        return (np.random.RandomState(0).randn(n).clip(-1, 1) * 8000).astype(np.int16).tobytes()
    if fill == "sine":
        return (np.sin(2 * np.pi * 220 * np.arange(n) / 16000) * 8000).astype(np.int16).tobytes()
    raise ValueError(fill)


def test_returns_probability_between_0_and_1():
    det = SmartTurnDetector.get()
    p = det.predict(_make_pcm(1.0, "noise"))
    assert 0.0 <= p <= 1.0


def test_wrong_sample_rate_raises():
    det = SmartTurnDetector.get()
    with pytest.raises(ValueError):
        det.predict(_make_pcm(1.0, "silence"), sample_rate=8000)


def test_empty_input_returns_zero():
    det = SmartTurnDetector.get()
    assert det.predict(b"") == 0.0


def test_short_input_padded_ok():
    det = SmartTurnDetector.get()
    # 200 ms of audio — should be padded to 8s and still work
    p = det.predict(_make_pcm(0.2, "silence"))
    assert 0.0 <= p <= 1.0


def test_long_input_truncated_ok():
    det = SmartTurnDetector.get()
    # 20s of audio — should be truncated to last 8s
    p = det.predict(_make_pcm(20.0, "noise"))
    assert 0.0 <= p <= 1.0


def test_singleton_reused():
    a = SmartTurnDetector.get()
    b = SmartTurnDetector.get()
    assert a is b


def test_freefunc_works():
    p = predict_end_of_turn(_make_pcm(1.0, "silence"))
    assert 0.0 <= p <= 1.0
