"""Cartesia TTS provider.

Sprint 4c (2026-07-28) — rewritten from the HTTP-only stub to use the
official `cartesia` SDK v3+ with SSE streaming. Docs:
https://docs.cartesia.ai/get-started/realtime-text-to-speech-quickstart

Why SSE over WebSocket for now:
  * SSE is unidirectional (server → client), which matches our
    stream-one-utterance-per-turn model exactly.
  * No session-scoped state on the provider — each synth call is
    self-contained, so the existing TTSProvider interface (no session
    handle) doesn't need widening.
  * We still get progressive chunks (~180ms P50 first-chunk latency).
  * If we later need the ~50ms per-turn win from persistent WebSocket
    reuse, we can add `AsyncTTSResourceConnectionManager` later without
    breaking the interface.

Model default: sonic-3 (fastest as of 2026-07). Older sonic-2 still
supported by setting CARTESIA_MODEL=sonic-2 for A/B.

Barge-in: the SDK's SSE iterator is a plain async generator — closing
it from outside the coroutine cancels the underlying HTTP request. Our
stream_sentences() consumer breaks out of the loop when VAD fires;
that's enough to stop synthesis mid-utterance.
"""
from __future__ import annotations

import base64
import logging
from typing import AsyncIterator, Optional

from app.core.config import settings

from ..base import TTSProvider

log = logging.getLogger(__name__)


# Default voice id used when neither CARTESIA_VOICE_ID nor a per-call
# override is set. This is the "Nova" preset — a warm, mid-pitched
# female American English voice, works well for receptionist personas.
# Callers should replace with a cloned founder voice via
# scripts/cartesia_clone_founder_voice.py before shipping to clients.
DEFAULT_VOICE_ID = "a0e99841-438c-4a64-b679-ae501e7d6091"

# PCM at 16kHz mono, 16-bit signed. Same shape Twilio Media Streams
# expects after µ-law transcoding, and what our browser player already
# handles. If a caller wants MP3 for local playback, they can override
# via the OUTPUT_FORMAT_* env vars (not exposed yet — YAGNI).
DEFAULT_OUTPUT_FORMAT = {
    "container": "raw",
    "encoding": "pcm_s16le",
    "sample_rate": 16000,
}


class CartesiaTTS(TTSProvider):
    name = "cartesia"

    # Cartesia's SSE endpoint emits audio in progressive chunks. Set
    # this so the pipeline knows it doesn't need to fall back to the
    # per-sentence sequential default in TTSProvider.stream_sentences.
    supports_streaming = True

    def __init__(self) -> None:
        self.api_key = settings.cartesia_api_key
        self.default_voice = settings.cartesia_voice_id or DEFAULT_VOICE_ID
        # sonic-3 is Cartesia's fastest as of mid-2026 (188ms P50 per Coval).
        # Fall back to sonic-2 if the user pinned an older model for A/B.
        self.model = settings.cartesia_model or "sonic-3"
        self._client = None

    def _get_client(self):
        """Lazy client construction so import-time doesn't fail when the
        key is absent (matters for CI + for local dev when the user
        hasn't signed up yet)."""
        if self._client is None:
            if not self.api_key:
                raise RuntimeError(
                    "CARTESIA_API_KEY not set. Sign up at cartesia.ai/sign-up "
                    "and add CARTESIA_API_KEY=<key> to .env."
                )
            from cartesia import AsyncCartesia
            self._client = AsyncCartesia(api_key=self.api_key)
        return self._client

    async def synthesize(self, text: str, voice: Optional[str] = None) -> tuple[bytes, str]:
        """One-shot synthesis — collects all SSE chunks into a single blob.

        Used by paths that need the full audio at once (greeting cache
        warm, one-shot /voice/tts endpoint). For interactive turn-taking,
        use stream_sentences() instead.
        """
        buf = bytearray()
        async for chunk, _ in self.stream_sentences(text, voice=voice):
            buf.extend(chunk)
        return bytes(buf), self._mime_for_output_format()

    async def stream_sentences(
        self, text: str, voice: Optional[str] = None,
    ) -> AsyncIterator[tuple[bytes, str]]:
        """Stream audio chunks as Cartesia produces them.

        Yields (audio_bytes, mime_type) tuples. Chunks arrive at the pace
        the model generates them — for sonic-3 this is roughly one chunk
        per ~200ms of audio, first chunk in ~180ms.

        NOTE: we bypass the base class's per-sentence chunking. Cartesia
        handles prosody across sentence boundaries better than
        chunk-then-synth, and streaming directly cuts a full sentence-
        split-round-trip out of the hot path.
        """
        client = self._get_client()
        mime = self._mime_for_output_format()
        try:
            events = await client.tts.sse(
                model_id=self.model,
                transcript=text,
                voice={"mode": "id", "id": voice or self.default_voice},
                output_format=DEFAULT_OUTPUT_FORMAT,
                language="en",
            )
            async for event in events:
                # Cartesia SSE events have a `type` discriminator:
                # "chunk" (audio), "timestamps", "done", "error".
                etype = getattr(event, "type", None)
                if etype == "chunk":
                    raw = getattr(event, "data", None)
                    if not raw:
                        continue
                    # SDK 3.5 returns base64-encoded audio in `data`.
                    # Decode to raw PCM bytes for our player.
                    audio = base64.b64decode(raw) if isinstance(raw, str) else raw
                    yield audio, mime
                elif etype == "done":
                    return
                elif etype == "error":
                    msg = getattr(event, "data", "") or "unknown cartesia error"
                    raise RuntimeError(f"cartesia SSE error: {msg}")
                # ignore timestamp events — we don't use word-level timing yet
        except Exception:
            log.exception("cartesia SSE stream failed")
            raise

    def _mime_for_output_format(self) -> str:
        fmt = DEFAULT_OUTPUT_FORMAT
        if fmt.get("container") == "raw" and fmt.get("encoding") == "pcm_s16le":
            return f"audio/pcm;rate={fmt.get('sample_rate', 16000)}"
        if fmt.get("container") == "mp3":
            return "audio/mpeg"
        if fmt.get("container") == "wav":
            return "audio/wav"
        return "application/octet-stream"

    async def close(self) -> None:
        """Best-effort cleanup — call on shutdown so the underlying HTTP
        client releases its connection pool."""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
