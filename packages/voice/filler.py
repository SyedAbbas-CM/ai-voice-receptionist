"""Pre-synthesized filler audio pool.

When the brain fires a tool call (Google Calendar, GHL, DB write), there's
a 300-800ms gap of silence. Users perceive >800ms silence as broken. Vapi
targets 2-4 filler phrases per call and reports big satisfaction wins.

We pre-synth a small pool at server startup so the file bytes are already
in memory when the brain needs one. Playing a filler adds ~10ms overhead
vs synthesizing at the moment of the tool call, which would add whatever
your TTS's cold-first-audio time is (300ms for ElevenLabs, ~10s+ for local
Qwen3 on M1).

Usage:
    pool = FillerPool()
    await pool.warm(tts)                # once at startup
    audio, mime = pool.pick()           # returns bytes ready to send
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from apps.api.app.providers.base import TTSProvider


log = logging.getLogger(__name__)


# Short, natural, verb-tense-neutral so they slot before any tool.
# Order matters — first phrase is picked when random module can't be trusted
# (rare, but explicit).
DEFAULT_FILLERS = [
    "One sec.",
    "Let me check that.",
    "Okay, just a moment.",
    "Mhm, checking now.",
    "Alright, one second.",
]


@dataclass
class FillerClip:
    text: str
    audio: bytes
    mime: str


@dataclass
class FillerPool:
    phrases: list[str] = field(default_factory=lambda: list(DEFAULT_FILLERS))
    clips: list[FillerClip] = field(default_factory=list)
    _last_index: int = -1

    async def warm(self, tts: "TTSProvider", voice: Optional[str] = None) -> int:
        """Pre-synthesize every filler phrase. Returns count of successful
        synths. Errors are logged and skipped — an incomplete pool is still
        useful."""
        self.clips = []
        for phrase in self.phrases:
            try:
                audio, mime = await tts.synthesize(phrase, voice=voice)
                if audio and mime != "text/x-browser-speak":
                    self.clips.append(FillerClip(text=phrase, audio=audio, mime=mime))
            except Exception as e:
                log.warning("filler synth failed for %r: %s", phrase, e)
        log.info("filler pool warmed: %d/%d clips ready", len(self.clips), len(self.phrases))
        return len(self.clips)

    def pick(self) -> Optional[FillerClip]:
        """Round-robin pick to avoid repeating the same filler on back-to-back
        tool calls. Returns None if pool is empty."""
        if not self.clips:
            return None
        # Round-robin with a tiny random offset so it doesn't feel mechanical.
        offset = random.choice([1, 2]) if len(self.clips) > 2 else 1
        self._last_index = (self._last_index + offset) % len(self.clips)
        return self.clips[self._last_index]

    def is_warm(self) -> bool:
        return bool(self.clips)


# Module-level singleton — one pool per server process
_pool_singleton: Optional[FillerPool] = None


def get_pool() -> FillerPool:
    global _pool_singleton
    if _pool_singleton is None:
        _pool_singleton = FillerPool()
    return _pool_singleton


async def warm_default_pool(tts: "TTSProvider", voice: Optional[str] = None) -> int:
    """Convenience: warm the singleton at app startup."""
    return await get_pool().warm(tts, voice=voice)
