"""Sentence-chunked TTS wrapper for low first-audio latency.

Local TTS on CPU/MPS is slow — a 30-word reply takes ~15-30s to fully
synthesize on M1 Pro. But callers only need the FIRST SENTENCE to start
playing quickly; the rest can be produced while the caller listens.

split_sentences(text) → list[str]     — punctuation-aware sentence split
astream_synth(tts, text) → AsyncGen   — yields (chunk_index, audio_bytes,
                                        mime) as each sentence is ready.
                                        Sentence 1 is available in ~1/N the
                                        total time.

For the browser call-simulator this means: caller finishes talking →
they hear the first sentence within ~5s instead of waiting 30s for
the full reply. Matters more than any other single trick for
perceived latency."""
from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator, Optional


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def split_sentences(text: str, max_len: int = 200) -> list[str]:
    """Split on . ! ? followed by whitespace + capital letter.

    Falls back to a single chunk if no boundary is found. Long chunks
    (>max_len chars) get soft-split at the nearest comma so we don't
    hand the TTS a paragraph and wait forever."""
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENTENCE_END.split(text) if p.strip()]
    result: list[str] = []
    for p in parts:
        if len(p) <= max_len:
            result.append(p)
            continue
        # Soft-split long parts at commas
        commas = [i for i, ch in enumerate(p) if ch == ","]
        if not commas:
            result.append(p)
            continue
        # Break at the comma nearest the middle
        mid = len(p) // 2
        pivot = min(commas, key=lambda i: abs(i - mid))
        result.append(p[: pivot + 1].strip())
        result.append(p[pivot + 1 :].strip())
    return [r for r in result if r]


async def astream_synth(
    tts,
    text: str,
    voice: Optional[str] = None,
) -> AsyncIterator[tuple[int, bytes, str]]:
    """Split text into sentences and synthesize them in parallel.

    Yields (chunk_index, audio_bytes, mime) tuples IN ORDER as each
    chunk becomes ready. Callers consume with `async for` and start
    playback immediately on the first yield.

    All chunks are launched concurrently, so total wall time equals
    the SLOWEST single-sentence synth, not the sum. But because we
    yield in order, the caller doesn't hear chunk 2 before chunk 1.
    """
    sentences = split_sentences(text)
    if not sentences:
        return

    tasks = [asyncio.create_task(tts.synthesize(s, voice=voice)) for s in sentences]
    for i, task in enumerate(tasks):
        try:
            audio, mime = await task
            if audio:
                yield i, audio, mime
        except Exception:
            # Skip failed chunks — don't break the whole stream
            continue
