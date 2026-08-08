"""Boot-time warmup: ensure common backchannels/fillers are cached.

Called once at server startup.  For each phrase not already on disk,
synth it via the real provider + persist.  Cache hits at runtime skip
ElevenLabs entirely.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable, Optional

from .cache import TTSCacheWrapper, _hash_key


log = logging.getLogger(__name__)


# Speech acts we want warm on boot.  Ordered by expected usage frequency
# so partial warmups still cover the common cases.
DEFAULT_BACKCHANNELS: tuple[str, ...] = (
    "Mm-hmm.",
    "Okay.",
    "Sure.",
    "Got it.",
    "Right.",
    "Yeah.",
    "Alright.",
    "One second.",
    "Let me see.",
    "Hmm.",
    "No problem.",
    "Perfect.",
    "Absolutely.",
    "Of course.",
    "Sounds good.",
)


async def warm_common_utterances(
    wrapper: TTSCacheWrapper,
    phrases: Optional[Iterable[str]] = None,
    parallelism: int = 3,
) -> dict[str, str]:
    """Ensure every phrase in `phrases` is cached.  Returns a dict of
    phrase → status ('cached'|'warmed'|'error')."""
    phrases = list(phrases) if phrases is not None else list(DEFAULT_BACKCHANNELS)
    results: dict[str, str] = {}
    inner_provider = getattr(wrapper._inner, "name", "tts")
    voice = getattr(wrapper._inner, "default_voice", "default")
    fmt = getattr(wrapper._inner, "output_format", "unknown")

    sem = asyncio.Semaphore(parallelism)

    async def _warm(phrase: str) -> None:
        key = _hash_key(voice, phrase, fmt, inner_provider)
        if wrapper._cache.has(key):
            results[phrase] = "cached"
            return
        async with sem:
            try:
                # Synth through the wrapper — miss path writes to cache.
                await wrapper.synthesize(phrase)
                results[phrase] = "warmed"
                log.info("tts_cache warmup ✓ %r", phrase)
            except Exception as e:
                results[phrase] = f"error:{type(e).__name__}"
                log.warning("tts_cache warmup FAILED %r: %s", phrase, e)

    await asyncio.gather(*[_warm(p) for p in phrases])
    ok = sum(1 for v in results.values() if v in ("cached", "warmed"))
    log.info("tts_cache warmup complete: %d/%d ready", ok, len(phrases))
    return results
