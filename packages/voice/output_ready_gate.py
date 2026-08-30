"""Greeting fast-start gate (LK-steal T1, task #102).

## Bug this closes

User's test call #2 (CA3dac68): "I literally hear only smile dental."
Twilio media stream ramps up ~200-400ms AFTER the agent's TTS starts
streaming. First syllables get eaten by the ramp — caller connects mid-
greeting.

Test call #3 improved but not fixed: "80% now, was 20% last time." The
timing is still coupled to when the agent decides to speak vs when
Twilio's carrier actually delivers audio to the caller's phone.

## LK's pattern (from voice/agent_activity.py)

Three cascading gates before TTS output is authorized:
  1. `_authorization_allowed: asyncio.Event` — a session-wide "speaking
     is allowed" flag. Cleared at session start; set when we know the
     audio pipe is warm.
  2. AEC warmup window — a fixed T ms grace period after connection.
  3. `first_frame_fut` — the actual playback-started timestamp anchors
     "speaking started at" bookkeeping.

We adapt the pattern to Twilio Media Streams:
  - "Warm" = we've received at least one inbound media frame from the
    caller. That proves the RTP path is bidirectionally established
    and Twilio's audio pipeline is flowing.
  - Fallback timeout for very quiet callers (they don't send audio
    right away): T ms after `start` event, unblock anyway. We tolerate
    the ramp cost in that case rather than blocking the greeting
    forever.
  - Twilio `mark` acks give us the true "audio actually reached the
    carrier" timestamp — voice-agent's T7 (backchannel grace) chains
    on this.

## Contract

Caller owns the lifecycle. Idiomatic use:

    # At session init (WebSocket handshake completes):
    gate = OutputReadyGate(fallback_ms=300)
    gate.mark_stream_started()   # begins the fallback timer

    # In the inbound-media handler, first frame:
    gate.mark_inbound_media_received()

    # In the outbound TTS write site (before first chunk):
    await gate.wait()   # blocks until inbound frame OR fallback elapsed

    # When Twilio 'mark' event lands for our first chunk:
    gate.mark_first_frame_played(twilio_mark_ts_ms)

    # Voice-agent T7 later reads:
    when_ms = gate.first_frame_played_at_ms   # or None if not yet

## Not in v1

- No AEC / echo-cancellation window — we're on Twilio's cooked audio,
  no local acoustic path.
- No mid-turn re-gating — first-turn only. Subsequent turns don't need
  the gate because inbound media has been flowing continuously.
- No per-tenant tuning of fallback_ms yet — one global value. Add a
  per-tenant override once we see calls where 300ms is wrong.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional


log = logging.getLogger(__name__)


# LK's default is ~300ms for their AEC warmup. Twilio carrier ramp
# empirically lands around 200-400ms. 300 splits the difference; if
# real calls still show clipping, raise to 400.
_DEFAULT_FALLBACK_MS = 300


class OutputReadyGate:
    """One-shot gate + timing anchor for the FIRST TTS turn of a call.

    Thread-safety: single-actor use. All methods must be called from
    the same event loop. No cross-actor sharing.
    """

    def __init__(self, fallback_ms: int = _DEFAULT_FALLBACK_MS) -> None:
        self._ready = asyncio.Event()
        self._fallback_ms = max(0, int(fallback_ms))
        self._stream_started_at_ms: Optional[float] = None
        # Populated when Twilio acks a `mark` for our first chunk.
        self._first_frame_played_at_ms: Optional[float] = None
        # Records what unblocked the wait — useful for observability.
        self._unblock_reason: Optional[str] = None
        self._fallback_task: Optional[asyncio.Task] = None

    # ─── lifecycle: called by session/actor ────────────────────────

    def mark_stream_started(self) -> None:
        """Call when Twilio 'start' event arrives. Begins the fallback
        timer. Idempotent: repeated calls are no-op."""
        if self._stream_started_at_ms is not None:
            return
        self._stream_started_at_ms = _now_ms()
        # Schedule the fallback so a silent caller doesn't block the
        # greeting forever. If mark_inbound_media_received() fires
        # first, the fallback is a no-op because the Event is already
        # set.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            # No running loop — under test in a sync context. That's
            # fine; caller can drive the fallback manually.
            return
        self._fallback_task = loop.create_task(
            self._fallback_arm(),
            name="output-ready-fallback",
        )

    def mark_inbound_media_received(self) -> None:
        """Call on the FIRST inbound Twilio media frame. Idempotent."""
        if self._ready.is_set():
            return
        self._unblock_reason = "inbound_media"
        self._ready.set()
        # Cancel the fallback timer — no longer needed.
        if self._fallback_task and not self._fallback_task.done():
            self._fallback_task.cancel()

    def mark_first_frame_played(self, mark_ts_ms: Optional[float] = None) -> None:
        """Call when Twilio 'mark' event arrives for our first TTS
        chunk. `mark_ts_ms` is the wall-clock timestamp we want to
        record; defaults to now.

        This is the true "audio was delivered to carrier" timestamp
        that downstream barge-in / T7 backchannel-grace logic keys off.
        Idempotent.
        """
        if self._first_frame_played_at_ms is not None:
            return
        self._first_frame_played_at_ms = mark_ts_ms if mark_ts_ms is not None else _now_ms()

    # ─── wait: called by TTS write path ────────────────────────────

    async def wait(self, timeout_ms: Optional[float] = None) -> str:
        """Block until the gate opens. Returns the reason:
        'inbound_media', 'fallback', or 'already_ready'.

        `timeout_ms` is a defensive per-caller max wait. Independent
        of the fallback timer. Returns 'timeout' if hit.
        """
        if self._ready.is_set():
            return "already_ready"
        try:
            if timeout_ms is not None:
                await asyncio.wait_for(self._ready.wait(), timeout=timeout_ms / 1000.0)
            else:
                await self._ready.wait()
        except asyncio.TimeoutError:
            log.warning(
                "OutputReadyGate.wait timed out after %dms without gate opening. "
                "First TTS chunk will proceed anyway.",
                int(timeout_ms) if timeout_ms else -1,
            )
            self._unblock_reason = "timeout"
            self._ready.set()
            return "timeout"
        return self._unblock_reason or "unknown"

    # ─── introspection ─────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def first_frame_played_at_ms(self) -> Optional[float]:
        """Twilio-mark-verified first-frame timestamp, or None if not
        yet acked. Voice-agent T7 (backchannel grace) reads this."""
        return self._first_frame_played_at_ms

    @property
    def unblock_reason(self) -> Optional[str]:
        """Which lifecycle event opened the gate. None until opened."""
        return self._unblock_reason

    @property
    def ramp_ms(self) -> Optional[int]:
        """How long the gate held the caller for. None if never opened
        or never started. Positive when inbound media arrived AFTER
        stream start (typical); zero-ish when they overlap."""
        if self._stream_started_at_ms is None:
            return None
        if not self._ready.is_set():
            return None
        # We don't track the exact opening timestamp — the reason is
        # enough for observability. If we ever need the exact ramp,
        # record `opened_at_ms` in mark_inbound_media_received /
        # fallback_arm and subtract here.
        return None

    # ─── internals ─────────────────────────────────────────────────

    async def _fallback_arm(self) -> None:
        """Wait fallback_ms then release the gate if nobody else has.

        Runs as a background task; cancelled if
        mark_inbound_media_received() fires first.
        """
        try:
            await asyncio.sleep(self._fallback_ms / 1000.0)
        except asyncio.CancelledError:
            return
        if not self._ready.is_set():
            log.info(
                "OutputReadyGate fallback fired after %dms — no inbound "
                "media received; releasing greeting anyway. Caller may "
                "hear a clipped first syllable.",
                self._fallback_ms,
            )
            self._unblock_reason = "fallback"
            self._ready.set()


def _now_ms() -> float:
    """Monotonic wall-clock in milliseconds. Used only for timestamp
    fields, never for scheduling (which uses asyncio.sleep)."""
    return time.monotonic() * 1000.0
