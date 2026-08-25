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
#
# 2026-08-18: expanded from 5 → 12 for variety on longer calls where the
# same "Okay, just a moment." was firing back-to-back.  All ≤4 syllables
# so the filler ends before the real reply's first byte lands.
DEFAULT_FILLERS = [
    "One second.",
    "Let me check that.",
    "Okay, just a moment.",
    "Mhm, checking now.",
    "Alright, one second.",
    "Gotcha.",
    "Yep, on it.",
    "Sure, hold on.",
    "Right, let me see.",
    "Okay.",
    "Hmm, one moment.",
    "Let's see.",
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
    # 2026-08-18: track the last N picked indices so pick() can avoid
    # repeating any of them.  Previously the round-robin-with-offset
    # could hit the same clip within 2-3 turns; users perceived that as
    # "the agent keeps saying 'okay just a moment' over and over."
    _recent_indices: list[int] = field(default_factory=list)
    _recent_window: int = 3

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
        """Random-with-recency-avoidance.  Never returns a clip whose
        index appears in the last `_recent_window` picks (as long as the
        pool is large enough to satisfy that)."""
        if not self.clips:
            return None
        n = len(self.clips)
        # Avoid the last `_recent_window` picks; if the pool is smaller
        # than the window+1, avoid whatever we can.
        avoid_count = min(self._recent_window, max(0, n - 1))
        avoid = set(self._recent_indices[-avoid_count:]) if avoid_count > 0 else set()
        candidates = [i for i in range(n) if i not in avoid]
        if not candidates:
            candidates = list(range(n))
        idx = random.choice(candidates)
        self._last_index = idx
        self._recent_indices.append(idx)
        # Bound the recency buffer so it doesn't grow unbounded.
        if len(self._recent_indices) > self._recent_window * 2:
            self._recent_indices = self._recent_indices[-self._recent_window:]
        return self.clips[idx]

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
