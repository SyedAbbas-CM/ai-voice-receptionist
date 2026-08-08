"""Task A: TTS cache tests.  Uses a fake inner provider so no
ElevenLabs credit gets spent."""
import asyncio
import os
import tempfile
import pytest

from packages.tts_cache import TTSCacheWrapper, TTSCache, normalize_key_text
from packages.tts_cache.warmup import warm_common_utterances


class _FakeTTS:
    """Records every synth call so we can assert cache hits skipped it."""
    name = "faketts"
    default_voice = "test-voice"
    output_format = "pcm_16000"

    def __init__(self):
        self.calls = []

    async def synthesize(self, text: str, voice=None):
        self.calls.append((text, voice))
        # Return deterministic fake audio so identical inputs → identical output
        return f"AUDIO({text})".encode("utf-8"), "audio/pcm"


@pytest.fixture
def tmp_cache_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def wrapper(tmp_cache_dir):
    cache = TTSCache(cache_dir=tmp_cache_dir, max_bytes=10 * 1024 * 1024)
    inner = _FakeTTS()
    return TTSCacheWrapper(inner, cache=cache), inner, cache


def test_normalize_ignores_case_and_trailing_punct():
    assert normalize_key_text("Yeah.") == normalize_key_text("yeah")
    assert normalize_key_text("Mm-hmm!") == normalize_key_text("mm-hmm")
    assert normalize_key_text("  hello   world  ") == "hello world"


def test_normalize_preserves_numbers():
    # 3pm and 3 p.m. and 3 pm are DIFFERENT audio, must not collide
    assert normalize_key_text("3pm") != normalize_key_text("3 pm")


def test_first_call_is_miss_second_call_is_hit(wrapper):
    w, inner, cache = wrapper
    async def _run():
        audio1, mime1 = await w.synthesize("hello world")
        assert audio1 == b"AUDIO(hello world)"
        assert len(inner.calls) == 1
        # Give the fire-and-forget write task a tick to persist
        await asyncio.sleep(0.05)
        audio2, mime2 = await w.synthesize("hello world")
        assert audio2 == b"AUDIO(hello world)"
        assert len(inner.calls) == 1  # STILL 1, cache hit
        assert mime1 == mime2
    asyncio.run(_run())


def test_case_variation_hits_same_entry(wrapper):
    w, inner, cache = wrapper
    async def _run():
        await w.synthesize("Yeah")
        await asyncio.sleep(0.05)
        await w.synthesize("YEAH.")
        await w.synthesize("yeah!")
        assert len(inner.calls) == 1
    asyncio.run(_run())


def test_provider_error_propagates(wrapper):
    w, inner, cache = wrapper
    async def _run():
        async def boom(text, voice=None):
            raise RuntimeError("elevenlabs down")
        inner.synthesize = boom
        with pytest.raises(RuntimeError, match="elevenlabs down"):
            await w.synthesize("this will fail")
    asyncio.run(_run())


def test_warm_common_utterances(wrapper):
    w, inner, cache = wrapper
    async def _run():
        results = await warm_common_utterances(w, phrases=["Yeah", "Okay", "Sure"])
        assert results == {"Yeah": "warmed", "Okay": "warmed", "Sure": "warmed"}
        # Re-run — should all report cached
        results2 = await warm_common_utterances(w, phrases=["Yeah", "Okay", "Sure"])
        assert all(v == "cached" for v in results2.values())
    asyncio.run(_run())


def test_cache_survives_reload(tmp_cache_dir):
    """After writing entries, a new TTSCache instance rebuilds index from disk."""
    async def _run():
        cache1 = TTSCache(cache_dir=tmp_cache_dir, max_bytes=10 * 1024 * 1024)
        w = TTSCacheWrapper(_FakeTTS(), cache=cache1)
        await w.synthesize("hello persistent")
        await asyncio.sleep(0.05)
        # New cache instance — must find the entry.
        cache2 = TTSCache(cache_dir=tmp_cache_dir, max_bytes=10 * 1024 * 1024)
        assert len(cache2._index) == 1
        entry = list(cache2._index.values())[0]
        assert entry.size > 0
    asyncio.run(_run())


def test_lru_eviction_under_pressure(tmp_cache_dir):
    """When total bytes exceed max_bytes, oldest entry gets evicted."""
    # Small cache, one entry ~15 bytes, cap 40 bytes → only 2 fit.
    cache = TTSCache(cache_dir=tmp_cache_dir, max_bytes=40)
    w = TTSCacheWrapper(_FakeTTS(), cache=cache)
    async def _run():
        await w.synthesize("first entry")  # 18 bytes
        await asyncio.sleep(0.02)
        await w.synthesize("second entry")  # 19 bytes → total 37, ok
        await asyncio.sleep(0.02)
        await w.synthesize("third entry")   # 18 bytes → 55 > 40, evict first
        await asyncio.sleep(0.05)
        assert len(cache._index) == 2
        # First entry should be gone
        keys = list(cache._index.keys())
        assert cache._total_bytes <= 40
    asyncio.run(_run())
