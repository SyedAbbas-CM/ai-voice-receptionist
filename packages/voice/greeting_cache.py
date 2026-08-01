"""Pre-synthesized greeting cache.

The greeting is deterministic — same text every call. Synthesizing it live at
turn 1 adds 2-3s of cold-start latency (TTS model load + synth). Pre-caching
means turn 1 first-audio latency drops to network-time only (~50-200ms).

Cache is:
  - Keyed by (business_name, greeting_text) hash so a business profile change
    invalidates automatically
  - In-memory (small ~100-500KB per business)
  - Warmed at server startup via `warm_greeting_cache()`

Public API:
    get_cached_greeting(text) -> Optional[tuple[bytes, str]]
    warm_greeting_cache(text, tts) -> None
    clear_cache() -> None
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from apps.api.app.providers.base import TTSProvider


log = logging.getLogger(__name__)


# {sha256(text): (audio_bytes, mime)}
_cache: dict[str, tuple[bytes, str]] = {}


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def get_cached_greeting(text: str) -> Optional[tuple[bytes, str]]:
    """Return cached (audio_bytes, mime) for this exact text, or None if cold."""
    return _cache.get(_key(text))


async def warm_greeting_cache(text: str, tts: "TTSProvider") -> bool:
    """Pre-synthesize a greeting and cache it. Non-fatal on TTS error —
    logs and returns False so startup doesn't block on TTS problems.

    Only warms if not already cached (idempotent). Safe to call from
    multiple startup handlers or on business-profile hot-reload.
    """
    key = _key(text)
    if key in _cache:
        return True   # already warm
    try:
        audio, mime = await tts.synthesize(text)
        _cache[key] = (audio, mime)
        log.info(
            "greeting cache warmed: %d bytes for text %r",
            len(audio), text[:60] + ("..." if len(text) > 60 else ""),
        )
        return True
    except Exception as e:
        log.warning("greeting cache warm failed (%s: %s); will synth on first call",
                    e.__class__.__name__, str(e)[:100])
        return False


def clear_cache() -> None:
    """Drop everything. Use on business-profile hot-reload."""
    _cache.clear()


def cache_size() -> int:
    return len(_cache)
