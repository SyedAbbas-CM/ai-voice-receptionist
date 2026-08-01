"""Cartesia TTS provider tests.

Two layers:

  1. Shape tests (no network) — verify the provider assembles the right
     SDK call, decodes chunks correctly, respects settings overrides.
     These use a fake AsyncCartesia stub.

  2. Live integration test — gated on CARTESIA_API_KEY. Actually hits
     the Cartesia SSE endpoint and verifies audio bytes come back within
     a reasonable time budget. Skipped in CI unless the key is set.
"""
from __future__ import annotations

import asyncio
import base64
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ---------- shape tests ----------

class _FakeEvent:
    def __init__(self, type_: str, data=None):
        self.type = type_
        self.data = data


class _FakeSSE:
    """Async iterator that yields the events we've been seeded with."""
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class _FakeTTSResource:
    def __init__(self, events):
        self._events = events
        self.last_kwargs = None

    async def sse(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeSSE(list(self._events))


class _FakeAsyncCartesia:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.tts = _FakeTTSResource([])

    async def close(self):
        pass


def _make_provider(monkeypatch, api_key="test-key", voice_id=None, model="sonic-3"):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "cartesia_api_key", api_key, raising=False)
    monkeypatch.setattr(cfg.settings, "cartesia_voice_id", voice_id, raising=False)
    monkeypatch.setattr(cfg.settings, "cartesia_model", model, raising=False)
    from app.providers.tts.cartesia_tts import CartesiaTTS
    return CartesiaTTS()


@pytest.mark.asyncio
async def test_missing_api_key_raises_on_first_call(monkeypatch):
    provider = _make_provider(monkeypatch, api_key=None)
    with pytest.raises(RuntimeError, match="CARTESIA_API_KEY"):
        await provider.synthesize("hello")


@pytest.mark.asyncio
async def test_synthesize_concats_chunks(monkeypatch):
    provider = _make_provider(monkeypatch)
    fake = _FakeAsyncCartesia("test-key")
    fake.tts._events = [
        _FakeEvent("chunk", data=base64.b64encode(b"AAA").decode()),
        _FakeEvent("chunk", data=base64.b64encode(b"BBB").decode()),
        _FakeEvent("done"),
    ]
    with patch("cartesia.AsyncCartesia", return_value=fake):
        audio, mime = await provider.synthesize("hi there")
    assert audio == b"AAABBB"
    assert mime.startswith("audio/pcm")


@pytest.mark.asyncio
async def test_stream_sentences_yields_progressive_chunks(monkeypatch):
    provider = _make_provider(monkeypatch)
    fake = _FakeAsyncCartesia("test-key")
    fake.tts._events = [
        _FakeEvent("chunk", data=base64.b64encode(b"X").decode()),
        _FakeEvent("chunk", data=base64.b64encode(b"Y").decode()),
        _FakeEvent("chunk", data=base64.b64encode(b"Z").decode()),
        _FakeEvent("done"),
    ]
    with patch("cartesia.AsyncCartesia", return_value=fake):
        chunks = []
        async for audio, mime in provider.stream_sentences("test"):
            chunks.append(audio)
    assert chunks == [b"X", b"Y", b"Z"]


@pytest.mark.asyncio
async def test_stream_sentences_passes_model_voice_language(monkeypatch):
    provider = _make_provider(monkeypatch, voice_id="my-clone-id", model="sonic-2")
    fake = _FakeAsyncCartesia("test-key")
    fake.tts._events = [_FakeEvent("done")]
    with patch("cartesia.AsyncCartesia", return_value=fake):
        async for _ in provider.stream_sentences("hi", voice="override-voice"):
            pass
    kwargs = fake.tts.last_kwargs
    assert kwargs["model_id"] == "sonic-2"
    assert kwargs["voice"] == {"mode": "id", "id": "override-voice"}
    assert kwargs["language"] == "en"
    assert kwargs["output_format"]["encoding"] == "pcm_s16le"


@pytest.mark.asyncio
async def test_error_event_raises(monkeypatch):
    provider = _make_provider(monkeypatch)
    fake = _FakeAsyncCartesia("test-key")
    fake.tts._events = [
        _FakeEvent("chunk", data=base64.b64encode(b"partial").decode()),
        _FakeEvent("error", data="rate limited"),
    ]
    with patch("cartesia.AsyncCartesia", return_value=fake):
        with pytest.raises(RuntimeError, match="cartesia SSE error: rate limited"):
            async for _ in provider.stream_sentences("hi"):
                pass


def test_provider_advertises_streaming(monkeypatch):
    provider = _make_provider(monkeypatch)
    assert provider.supports_streaming is True


def test_default_model_is_sonic3(monkeypatch):
    provider = _make_provider(monkeypatch, model=None)
    assert provider.model == "sonic-3"


# ---------- live integration test (opt-in) ----------

@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("CARTESIA_API_KEY"),
    reason="CARTESIA_API_KEY not set — live test skipped",
)
async def test_live_sse_returns_audio_under_1s(monkeypatch):
    """Actually hits Cartesia. Verifies first-chunk arrives quickly and
    total audio bytes are non-trivial. Costs a tiny fraction of a cent."""
    from app.providers.tts.cartesia_tts import CartesiaTTS
    provider = CartesiaTTS()
    t0 = time.perf_counter()
    first_chunk_ms = None
    total_bytes = 0
    async for chunk, mime in provider.stream_sentences("Hello, this is a test."):
        if first_chunk_ms is None:
            first_chunk_ms = (time.perf_counter() - t0) * 1000
        total_bytes += len(chunk)
    total_ms = (time.perf_counter() - t0) * 1000
    print(f"cartesia live: first_chunk={first_chunk_ms:.0f}ms total={total_ms:.0f}ms bytes={total_bytes}")
    assert first_chunk_ms is not None, "no audio chunks arrived"
    assert first_chunk_ms < 1500, f"first chunk took {first_chunk_ms:.0f}ms (>1500ms budget)"
    assert total_bytes > 1000, f"only {total_bytes} bytes returned"
    await provider.close()
