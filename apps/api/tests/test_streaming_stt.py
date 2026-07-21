"""Tests for streaming STT — verify the event shape, `supports_streaming`
flag, and that the interface degrades gracefully when a provider doesn't
implement streaming."""
from __future__ import annotations

import pytest

from app.providers.base import STTEvent, STTProvider


class NonStreamingStub(STTProvider):
    name = "nonstreaming"
    supports_streaming = False

    async def transcribe(self, audio_bytes, sample_rate=16000, mime="audio/wav"):
        return "ignored"


class StreamingStub(STTProvider):
    """A tiny STT that emits one partial then one final for every 5 chunks."""
    name = "streamstub"
    supports_streaming = True

    async def transcribe(self, audio_bytes, sample_rate=16000, mime="audio/wav"):
        return "batch"

    async def transcribe_stream(self, audio_chunks, sample_rate=16000, encoding="linear16"):
        count = 0
        async for chunk in audio_chunks:
            count += 1
            if count == 2:
                yield STTEvent(kind="speech_start")
                yield STTEvent(kind="partial", text="hello")
            if count == 5:
                yield STTEvent(kind="final", text="hello there", is_final=True)


def test_stt_event_defaults():
    e = STTEvent(kind="partial", text="hi")
    assert e.text == "hi"
    assert e.is_final is False


def test_stt_event_final():
    e = STTEvent(kind="final", text="hello there", is_final=True)
    assert e.is_final is True


@pytest.mark.asyncio
async def test_nonstreaming_provider_raises():
    stt = NonStreamingStub()

    async def chunks():
        yield b"x"

    with pytest.raises(NotImplementedError):
        async for _ in stt.transcribe_stream(chunks()):
            pass


@pytest.mark.asyncio
async def test_streaming_provider_yields_events_in_order():
    stt = StreamingStub()

    async def chunks():
        for _ in range(6):
            yield b"x"

    events = []
    async for ev in stt.transcribe_stream(chunks()):
        events.append((ev.kind, ev.text, ev.is_final))

    # Expect: speech_start, partial, final
    assert events == [
        ("speech_start", "", False),
        ("partial", "hello", False),
        ("final", "hello there", True),
    ]


def test_provider_supports_streaming_flag():
    """Callers can check `supports_streaming` before trying to open a stream."""
    from app.providers.stt.deepgram_stt import DeepgramSTT
    from app.providers.stt.local_whisper_stt import LocalWhisperSTT
    from app.providers.stt.openai_stt import OpenAISTT
    from app.providers.stt.groq_stt import GroqSTT

    assert DeepgramSTT.supports_streaming is True
    assert LocalWhisperSTT.supports_streaming is True
    # Batch-only providers keep the base class default
    assert OpenAISTT.supports_streaming is False
    assert GroqSTT.supports_streaming is False
