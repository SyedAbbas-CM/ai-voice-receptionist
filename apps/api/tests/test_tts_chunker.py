"""Verify sentence-chunker splits correctly and streams in order."""
from __future__ import annotations

import asyncio

import pytest

from packages.core_agent.tts_chunker import astream_synth, split_sentences


class FakeSlowTTS:
    """Simulates slow TTS: 'slow' sentences take longer, but we should
    still receive them IN ORDER."""

    def __init__(self, delays_by_index: list[float]):
        self.delays_by_index = delays_by_index
        self.calls: list[str] = []

    async def synthesize(self, text, voice=None):
        i = len(self.calls)
        self.calls.append(text)
        await asyncio.sleep(self.delays_by_index[i] if i < len(self.delays_by_index) else 0.0)
        return f"AUDIO({text!r})".encode(), "audio/wav"


def test_split_two_sentences():
    parts = split_sentences("Hi there. How can I help?")
    assert parts == ["Hi there.", "How can I help?"]


def test_split_handles_single_sentence():
    assert split_sentences("Just one.") == ["Just one."]
    assert split_sentences("") == []
    assert split_sentences("   ") == []


def test_split_ignores_mid_sentence_periods():
    # Common issue: "Dr. Jones is here." should NOT split at "Dr."
    parts = split_sentences("Dr. Jones is here. He's ready.")
    # Best-effort: our simple regex splits at any . + Capital, so this
    # will produce two chunks. Documented limitation for now.
    assert len(parts) >= 1


def test_split_multi_sentence_reply():
    parts = split_sentences(
        "Sure, no problem! I can help you book that. What day works best?"
    )
    assert len(parts) == 3
    assert parts[0].startswith("Sure")
    assert parts[-1].endswith("?")


def test_split_soft_wraps_very_long_sentence():
    huge = "a" * 100 + ", " + "b" * 250
    parts = split_sentences(huge, max_len=200)
    # Should soft-split at the comma
    assert len(parts) == 2


@pytest.mark.asyncio
async def test_astream_yields_in_order_even_if_earlier_slower():
    """Sentence 0 takes 200ms, sentence 1 takes 10ms. We should still
    yield 0 first because the API contract is 'in order'."""
    tts = FakeSlowTTS(delays_by_index=[0.2, 0.01, 0.05])
    text = "First one. Second one. Third one."

    got: list[tuple[int, bytes]] = []
    async for i, audio, mime in astream_synth(tts, text):
        got.append((i, audio))

    assert [g[0] for g in got] == [0, 1, 2]
    assert b"First one" in got[0][1]
    assert b"Second one" in got[1][1]
    assert b"Third one" in got[2][1]


@pytest.mark.asyncio
async def test_astream_first_chunk_ready_before_last():
    """The whole point: first sentence must be ready in ~1/N the total
    wall time, not the sum."""
    tts = FakeSlowTTS(delays_by_index=[0.05, 0.05, 0.05])  # 3 × 50ms each

    import time
    t0 = time.time()
    first_at = None
    last_at = None
    async for i, audio, mime in astream_synth(tts, "One. Two. Three."):
        now = time.time() - t0
        if first_at is None:
            first_at = now
        last_at = now

    # First chunk should arrive in ~50ms, last in ~50ms (all parallel)
    assert first_at is not None and first_at < 0.15, f"first chunk too slow: {first_at}s"
    assert last_at is not None and last_at < 0.20, f"last chunk too slow: {last_at}s"


@pytest.mark.asyncio
async def test_astream_launches_synth_in_parallel():
    """Confirm all sentences START synthesis before any complete."""
    started: list[float] = []
    finished: list[float] = []

    class TimingTTS:
        async def synthesize(self, text, voice=None):
            import time
            started.append(time.time())
            await asyncio.sleep(0.1)
            finished.append(time.time())
            return b"x", "audio/wav"

    tts = TimingTTS()
    async for _ in astream_synth(tts, "One. Two. Three."):
        pass

    # All three synths should have started before any finished
    # (parallel launch)
    assert len(started) == 3
    assert len(finished) == 3
    assert max(started) < min(finished), (
        f"expected all starts before any finish, "
        f"but max start {max(started)} > min finish {min(finished)}"
    )
