"""Verify the local faster-whisper adapter has the same silence-rejection
+ hallucination-filter behavior as the Groq STT adapter, so switching
STT_PROVIDER=local doesn't reintroduce the 'Oh my god' bug."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class _FakeSegment:
    def __init__(self, text: str):
        self.text = text


def _fake_model_returning(text: str):
    m = MagicMock()

    def _transcribe(*args, **kwargs):
        # faster-whisper returns (segments_iter, info)
        return iter([_FakeSegment(text)]), MagicMock()

    m.transcribe = _transcribe
    return m


@pytest.mark.asyncio
async def test_short_clip_returns_empty_without_loading_model(monkeypatch):
    from app.providers.stt.local_whisper_stt import LocalWhisperSTT

    stt = LocalWhisperSTT()
    result = await stt.transcribe(b"\x00" * 100, mime="audio/webm;codecs=opus")
    assert result == ""
    # Model should NOT have loaded — short-circuit happens before _load()
    assert stt._model is None


@pytest.mark.asyncio
async def test_filters_oh_my_god_hallucination_local(monkeypatch):
    from app.providers.stt.local_whisper_stt import LocalWhisperSTT

    for hallucinated in ["Oh my god!", "OH MY GOD", "Thank you.", "You.", "Bye."]:
        stt = LocalWhisperSTT()
        stt._model = _fake_model_returning(hallucinated)
        result = await stt.transcribe(b"\x00" * 20000, mime="audio/webm;codecs=opus")
        assert result == "", f"expected filter to reject {hallucinated!r}, got {result!r}"


@pytest.mark.asyncio
async def test_real_transcript_passes_through_local():
    from app.providers.stt.local_whisper_stt import LocalWhisperSTT

    stt = LocalWhisperSTT()
    stt._model = _fake_model_returning("I need an appointment for back pain tomorrow.")
    result = await stt.transcribe(b"\x00" * 20000, mime="audio/webm;codecs=opus")
    assert "back pain" in result


@pytest.mark.asyncio
async def test_vad_and_deterministic_args_passed():
    """Confirm the safety knobs (vad_filter, temperature=0, no_speech_threshold)
    are actually being sent to faster-whisper. If someone strips one of these
    thinking it's 'cleanup', hallucinations come back."""
    from app.providers.stt.local_whisper_stt import LocalWhisperSTT

    captured = {}

    class _Model:
        def transcribe(self, *args, **kwargs):
            captured.update(kwargs)
            return iter([_FakeSegment("hi there")]), MagicMock()

    stt = LocalWhisperSTT()
    stt._model = _Model()
    await stt.transcribe(b"\x00" * 20000, mime="audio/wav")

    assert captured.get("vad_filter") is True
    assert captured.get("temperature") == 0
    # Was 0.6 originally. Lowered to 0.4 in the July 2026 STT audit — the old
    # threshold rejected legitimate soft-spoken openers like "hello can you hear me"
    # as silence. 0.4 still filters clear hallucinations without eating real speech.
    assert captured.get("no_speech_threshold", 0) >= 0.4
    assert captured.get("language") == "en"
    assert captured.get("condition_on_previous_text") is False
