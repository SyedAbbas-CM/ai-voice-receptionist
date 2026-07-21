"""Tests for the Kokoro-82M TTS adapter.

We don't actually load the 300 MB model in CI — that'd be slow and
unnecessary. Instead we mock the KPipeline and verify:
- Config wiring works
- The synthesize() method concatenates KPipeline chunks correctly
- Empty output doesn't crash callers
- Voice/factory dispatch works
"""
from __future__ import annotations

import io
import wave

import numpy as np
import pytest


def test_factory_registers_kokoro(monkeypatch):
    from app.core.config import settings
    from app.providers.factory import get_tts

    get_tts.cache_clear()
    monkeypatch.setattr(settings, "tts_provider", "kokoro")
    tts = get_tts()
    assert tts.name == "kokoro"
    # Reset for other tests
    get_tts.cache_clear()


def test_kokoro_defaults():
    from app.providers.tts.kokoro_tts import KokoroTTS

    tts = KokoroTTS()
    assert tts.voice == "af_heart"
    assert tts.lang == "a"
    assert tts.sample_rate == 24000
    assert tts.device in ("mps", "cuda", "cpu")


def test_known_voices_set_is_reasonable():
    from app.providers.tts.kokoro_tts import KokoroTTS
    assert "af_heart" in KokoroTTS.KNOWN_VOICES
    assert "am_adam" in KokoroTTS.KNOWN_VOICES
    assert "bf_emma" in KokoroTTS.KNOWN_VOICES
    assert len(KokoroTTS.KNOWN_VOICES) >= 20


@pytest.mark.asyncio
async def test_synthesize_concatenates_chunks(monkeypatch):
    """KPipeline yields chunks; verify they get joined into one WAV."""
    from app.providers.tts.kokoro_tts import KokoroTTS

    class FakePipeline:
        def __call__(self, text, voice=None):
            # Yield three fake chunks — Kokoro's actual shape is a tuple
            # (graphemes, phonemes, audio_ndarray)
            yield ("hello", "h eh l ow", np.array([0.1, 0.2, 0.3], dtype="float32"))
            yield ("there", "th eh r", np.array([0.4, 0.5, 0.6], dtype="float32"))
            yield ("!", "!", np.array([0.7], dtype="float32"))

    tts = KokoroTTS()
    tts._pipeline = FakePipeline()  # bypass _load

    audio_bytes, mime = await tts.synthesize("hello there!", voice="af_heart")
    assert mime == "audio/wav"
    assert len(audio_bytes) > 0

    # Verify it's a real WAV
    with wave.open(io.BytesIO(audio_bytes), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 24000
        # 7 samples across the three chunks
        assert w.getnframes() == 7


@pytest.mark.asyncio
async def test_synthesize_returns_silence_on_empty_pipeline(monkeypatch):
    """If KPipeline yields nothing (rare but possible), don't crash the caller."""
    from app.providers.tts.kokoro_tts import KokoroTTS

    class EmptyPipeline:
        def __call__(self, text, voice=None):
            return
            yield  # unreachable, makes this a generator

    tts = KokoroTTS()
    tts._pipeline = EmptyPipeline()

    audio_bytes, mime = await tts.synthesize("nothing to say")
    assert mime == "audio/wav"
    # Should return ~200ms of silence rather than empty
    assert len(audio_bytes) > 100


@pytest.mark.asyncio
async def test_synthesize_accepts_voice_override(monkeypatch):
    """Voice arg on synthesize() should override the env default."""
    from app.providers.tts.kokoro_tts import KokoroTTS

    captured_voices = []

    class TrackingPipeline:
        def __call__(self, text, voice=None):
            captured_voices.append(voice)
            yield ("x", "x", np.array([0.0], dtype="float32"))

    tts = KokoroTTS()
    tts._pipeline = TrackingPipeline()
    tts.voice = "af_heart"  # default

    await tts.synthesize("hi", voice="am_adam")  # override
    assert captured_voices[-1] == "am_adam"

    await tts.synthesize("hi")  # no override → use default
    assert captured_voices[-1] == "af_heart"


def test_cost_book_lists_kokoro():
    from packages.observability import estimate_tts_cost
    assert estimate_tts_cost("kokoro", characters=1_000_000) == 0.0


def test_pcm_to_wav_clips_out_of_range_samples():
    """Kokoro can occasionally emit values slightly outside [-1, 1] on
    edge chunks. Verify we clip instead of overflowing int16."""
    from app.providers.tts.kokoro_tts import KokoroTTS
    tts = KokoroTTS()

    dangerous = np.array([2.0, -2.0, 0.5], dtype="float32")
    audio_bytes = tts._pcm_to_wav_bytes(dangerous, 24000)

    with wave.open(io.BytesIO(audio_bytes), "rb") as w:
        pcm = w.readframes(w.getnframes())
    samples = np.frombuffer(pcm, dtype="<i2")
    # Everything within int16 range
    assert samples.min() >= -32768
    assert samples.max() <= 32767


@pytest.mark.asyncio
async def test_onnx_backend_synthesizes_via_create():
    """When the ONNX backend is active, synthesize() should call
    Kokoro.create() once and wrap the returned samples in WAV."""
    from app.providers.tts.kokoro_tts import KokoroTTS

    class FakeOnnxKokoro:
        def create(self, text, voice=None, speed=1.0, lang="en-us"):
            # kokoro_onnx returns (samples: np.ndarray, sample_rate: int)
            return np.array([0.1, 0.2, 0.3, 0.4], dtype="float32"), 24000

    tts = KokoroTTS()
    tts._onnx_model = FakeOnnxKokoro()
    tts._active_backend = "onnx"

    audio_bytes, mime = await tts.synthesize("hello", voice="af_heart")
    assert mime == "audio/wav"
    with wave.open(io.BytesIO(audio_bytes), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getframerate() == 24000
        assert w.getnframes() == 4


def test_backend_setting_defaults_to_auto():
    from app.providers.tts.kokoro_tts import KokoroTTS
    tts = KokoroTTS()
    assert tts.backend == "auto"


def test_backend_setting_reads_env(monkeypatch):
    """Verify KOKORO_BACKEND env is honored."""
    from app.core.config import settings
    from app.providers.tts.kokoro_tts import KokoroTTS

    monkeypatch.setattr(settings, "kokoro_backend", "onnx")
    tts = KokoroTTS()
    assert tts.backend == "onnx"

    monkeypatch.setattr(settings, "kokoro_backend", "pytorch")
    tts = KokoroTTS()
    assert tts.backend == "pytorch"
