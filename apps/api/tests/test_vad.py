"""Tests for the VAD abstraction. Silero is exercised only if torch is
installed; otherwise we skip. The RMS fallback is always tested so the
zero-dep path stays green."""
from __future__ import annotations

import audioop
import io
import wave

import pytest

from packages.voice import RmsVAD, build_vad


def _pcm16_silence(duration_ms: int, sample_rate: int = 8000) -> bytes:
    n = int(sample_rate * duration_ms / 1000)
    return b"\x00\x00" * n


def _pcm16_loud_tone(duration_ms: int, sample_rate: int = 8000, amplitude: int = 20000) -> bytes:
    """Generate a fake speech-ish signal — square wave at 200Hz. Loud enough
    to trip any reasonable RMS threshold."""
    import struct
    n = int(sample_rate * duration_ms / 1000)
    period = sample_rate // 200
    samples = []
    for i in range(n):
        v = amplitude if (i // period) % 2 == 0 else -amplitude
        samples.append(v)
    return b"".join(struct.pack("<h", v) for v in samples)


def _to_mulaw(pcm16: bytes) -> bytes:
    return audioop.lin2ulaw(pcm16, 2)


def test_rms_vad_silence():
    vad = RmsVAD(threshold=500)
    silence_mulaw = _to_mulaw(_pcm16_silence(60))
    assert vad.is_speech(silence_mulaw, sample_rate=8000, mime="audio/mulaw") is False


def test_rms_vad_loud_speech():
    vad = RmsVAD(threshold=500)
    loud_mulaw = _to_mulaw(_pcm16_loud_tone(60))
    assert vad.is_speech(loud_mulaw, sample_rate=8000, mime="audio/mulaw") is True


def test_rms_vad_empty_frame():
    vad = RmsVAD()
    assert vad.is_speech(b"", sample_rate=8000, mime="audio/mulaw") is False


def test_rms_vad_accepts_wav_input():
    vad = RmsVAD(threshold=500)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(_pcm16_loud_tone(50, sample_rate=16000))
    assert vad.is_speech(buf.getvalue(), sample_rate=16000, mime="audio/wav") is True


def test_build_vad_rms_explicit():
    vad = build_vad(kind="rms")
    assert vad.name == "rms"


def test_build_vad_auto_falls_back_gracefully(monkeypatch):
    """If SileroVAD._load blows up, auto mode should return RmsVAD."""
    from packages.voice import vad as vad_module

    orig_load = vad_module.SileroVAD._load

    def _broken_load(self):
        raise RuntimeError("simulated dep missing")

    monkeypatch.setattr(vad_module.SileroVAD, "_load", _broken_load)

    v = build_vad(kind="auto")
    assert v.name == "rms"
    # Restore
    monkeypatch.setattr(vad_module.SileroVAD, "_load", orig_load)


def test_build_vad_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown VAD kind"):
        build_vad(kind="magic")


# -----------------------------------------------------------------
# Silero test — only runs if torch is importable
# -----------------------------------------------------------------

def _torch_available() -> bool:
    try:
        import torch  # noqa
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _torch_available(), reason="torch not installed")
def test_silero_classifies_silence_vs_speech():
    """Silero should be more discriminative than RMS — verify it correctly
    says 'not speech' for a full second of silence."""
    from packages.voice.vad import SileroVAD

    vad = SileroVAD(threshold=0.5)
    try:
        vad._load()
    except RuntimeError as e:
        pytest.skip(f"Silero unavailable: {e}")

    silence_mulaw = _to_mulaw(_pcm16_silence(1000))
    assert vad.is_speech(silence_mulaw, sample_rate=8000, mime="audio/mulaw") is False
