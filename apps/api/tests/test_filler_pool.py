"""Filler pool tests: warms every phrase, skips failing synths, picks
round-robin without repeating."""
from __future__ import annotations

import pytest

from packages.voice import FillerPool, DEFAULT_FILLERS


class FakeTTS:
    def __init__(self, fail_indices=(), mime="audio/wav"):
        self.calls = 0
        self.fail_indices = set(fail_indices)
        self._mime = mime

    async def synthesize(self, text, voice=None):
        i = self.calls
        self.calls += 1
        if i in self.fail_indices:
            raise RuntimeError("fake TTS failed")
        return f"AUDIO({text})".encode(), self._mime


@pytest.mark.asyncio
async def test_warm_synthesizes_all_phrases():
    pool = FillerPool()
    tts = FakeTTS()
    n = await pool.warm(tts)
    assert n == len(DEFAULT_FILLERS)
    assert pool.is_warm()
    assert all(c.audio for c in pool.clips)


@pytest.mark.asyncio
async def test_warm_skips_failing_synths():
    """Partial pool is still useful. A failure on 1 phrase shouldn't kill
    the whole warmup."""
    pool = FillerPool()
    tts = FakeTTS(fail_indices={1, 3})
    n = await pool.warm(tts)
    assert n == len(DEFAULT_FILLERS) - 2
    assert pool.is_warm()


@pytest.mark.asyncio
async def test_warm_skips_browser_sentinel_tts():
    """Browser SpeechSynthesis returns a sentinel mime; those aren't real
    audio bytes so we shouldn't cache them."""
    class BrowserTTS:
        async def synthesize(self, text, voice=None):
            return b"", "text/x-browser-speak"

    pool = FillerPool()
    n = await pool.warm(BrowserTTS())
    assert n == 0
    assert not pool.is_warm()


@pytest.mark.asyncio
async def test_pick_returns_none_when_empty():
    pool = FillerPool()
    assert pool.pick() is None


@pytest.mark.asyncio
async def test_pick_rotates_without_immediate_repeats():
    pool = FillerPool()
    tts = FakeTTS()
    await pool.warm(tts)

    picks = [pool.pick().text for _ in range(10)]
    # No two adjacent picks are the same
    for a, b in zip(picks, picks[1:]):
        assert a != b, f"filler repeated back-to-back: {a!r}"


@pytest.mark.asyncio
async def test_singleton_get_pool_is_stable():
    from packages.voice import get_pool
    a = get_pool()
    b = get_pool()
    assert a is b
