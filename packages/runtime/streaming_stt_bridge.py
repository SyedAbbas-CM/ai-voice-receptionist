"""Streaming STT bridge — Sprint 10 C1.

Consumes an async stream of inbound audio frames (from the Twilio
adapter or browser widget) and produces CallEvents into the actor's
mailbox as partials/finals arrive.

Replaces the batch path where the actor buffers audio, waits for
silence, sends the whole WAV to Deepgram, blocks for the response.
Batch path was the ~800ms of dead air after every caller utterance.

Architecture:

    Twilio media frames ──┐
                          ├──► StreamingSTTBridge ──► CallActor.emit(STT event)
    Browser PCM frames  ──┘        │
                                   ├── partial hypothesis  (drives barge-in)
                                   ├── final hypothesis    (commits to brain)
                                   ├── speech_start        (turn manager signal)
                                   └── speech_end          (endpoint signal)

Contract:
  * Bridge OWNS one Deepgram WS per call.  Reconnects on transient
    failure.  Fails-closed after N reconnect attempts (falls back to
    batch STT for the next utterance).
  * Bridge is CANCELLABLE.  Call stop() to close the WS + drain the
    audio iterator.  Actor calls this on hangup.
  * Bridge stamps each event with the actor's CURRENT turn_generation
    so late partials get dropped by the actor's generation guard.

Not tied to Deepgram — the STTProvider.transcribe_stream contract is
the boundary.  Wiring a different streaming provider (AssemblyAI,
Speechmatics) means implementing the same async iterator interface.
"""
from __future__ import annotations

import asyncio
import audioop
import logging
import time
from typing import AsyncIterator, Optional

from .call_actor import CallActor
from .call_event import CallEvent, EventSource

log = logging.getLogger(__name__)


class StreamingSTTBridge:
    """One instance per active call.  Consumes inbound frames, pushes
    STT events into the actor.

    Usage:

        bridge = StreamingSTTBridge(
            actor=actor, stt_provider=stt, mulaw_input=True,
        )
        await bridge.start()
        # every inbound frame:
        bridge.feed(mulaw_frame)
        # on hangup:
        await bridge.stop()
    """

    def __init__(
        self,
        actor: CallActor,
        stt_provider,
        mulaw_input: bool = True,
        max_reconnects: int = 3,
        reconnect_backoff_s: float = 0.5,
    ) -> None:
        self._actor = actor
        self._stt = stt_provider
        self._mulaw_input = mulaw_input
        self._max_reconnects = max_reconnects
        self._reconnect_backoff_s = reconnect_backoff_s
        # Bounded audio queue — bridge drops old frames if it can't
        # keep up (better than growing unbounded).  8000 mulaw bytes
        # = 1 second at 8kHz.  10s cap.
        self._audio_queue: asyncio.Queue = asyncio.Queue(maxsize=800)
        self._stop_event = asyncio.Event()
        self._consumer_task: Optional[asyncio.Task] = None
        self._reconnect_count = 0
        self._started_at_monotonic_ns: Optional[int] = None

        # S13-A smart-turn: rolling 16kHz PCM buffer of the last ~8 sec
        # of caller audio.  Fed on every frame; consulted when we need
        # a prosodic end-of-turn probability.  Twilio is 8kHz µ-law
        # coming IN; feed() converts to 16-bit LIN 8kHz.  We upsample
        # to 16kHz here (naive linear) because smart-turn was trained
        # on 16kHz.  Browser widget path is already PCM but currently
        # also 8kHz mulaw-emulated — same handling either way.
        # Byte budget: 16000 samples/sec * 2 bytes * 8 sec = 256KB max.
        self._pcm16k_buffer: bytearray = bytearray()
        self._pcm16k_max_bytes: int = 16000 * 2 * 8  # 8 sec of 16 kHz mono

    async def start(self) -> None:
        """Kick off the consumer coroutine.  Idempotent."""
        if self._consumer_task is not None and not self._consumer_task.done():
            return
        self._stop_event.clear()
        self._started_at_monotonic_ns = time.monotonic_ns()
        self._consumer_task = asyncio.create_task(
            self._run(),
            name=f"stt-bridge-{self._actor.call_id}",
        )
        log.info("streaming STT bridge started call_id=%s", self._actor.call_id)

    async def stop(self) -> None:
        """Signal shutdown; drain queue; wait for consumer to exit."""
        self._stop_event.set()
        # Sentinel to unblock the audio iterator
        try:
            self._audio_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        if self._consumer_task is not None:
            try:
                await asyncio.wait_for(self._consumer_task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._consumer_task.cancel()

    def feed(self, frame: bytes) -> None:
        """Push one inbound audio frame into the bridge.  Non-blocking;
        drops on backpressure (with a rate-limited log)."""
        if not frame or self._stop_event.is_set():
            return
        # 2026-08-08: send mulaw DIRECTLY to Deepgram.  Previously we
        # ulaw2lin here + declared encoding=linear16, which triggered
        # Deepgram's "silent discard" format-drift bug (see
        # docs/rnd-2026-08/53-fast-stt-alternatives.md).  Now the
        # transcribe_stream call declares encoding=mulaw and we pass
        # the raw Twilio bytes through unchanged.  Zero conversion,
        # zero drift.
        payload = frame
        try:
            self._audio_queue.put_nowait(payload)
        except asyncio.QueueFull:
            # 2026-08-07: drop OLDEST, not newest.  Old policy discarded
            # incoming frames while stale audio sat queued — Deepgram
            # then saw a 30s gap of "no fresh audio" and closed the WS
            # with 1011 timeout (observed 2026-08-07 PK call).  New:
            # pop the oldest, enqueue the new one so Deepgram gets the
            # freshest 8s of audio even under backpressure.
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._audio_queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass
            if self._audio_queue.qsize() % 200 == 0:
                log.warning(
                    "STT bridge queue full call_id=%s; dropping oldest",
                    self._actor.call_id,
                )

        # S13-A smart-turn: keep a rolling 16kHz PCM buffer.  Inbound
        # is 8kHz LIN after mulaw conversion; upsample x2 to 16kHz
        # by simple sample repeat (adequate for prosody classifier;
        # the model was trained mostly on 16kHz phone-recorded audio
        # of similar quality).
        try:
            pcm16k = audioop.ratecv(payload, 2, 1, 8000, 16000, None)[0]
        except Exception:
            pcm16k = payload  # give up, feed as-is
        self._pcm16k_buffer.extend(pcm16k)
        if len(self._pcm16k_buffer) > self._pcm16k_max_bytes:
            # Trim to last 8 sec.
            drop = len(self._pcm16k_buffer) - self._pcm16k_max_bytes
            del self._pcm16k_buffer[:drop]

    def get_recent_pcm16k(self, seconds: float = 8.0) -> bytes:
        """S13-A: return the last `seconds` of buffered 16 kHz PCM
        for smart-turn inference.  Snapshot — safe to hand off."""
        max_bytes = int(16000 * 2 * seconds)
        buf = bytes(self._pcm16k_buffer[-max_bytes:])
        return buf

    # ── internal run loop ──────────────────────────────────────────

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._stream_once()
                # Clean exit — consumer finished normally
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._reconnect_count += 1
                if self._reconnect_count > self._max_reconnects:
                    log.error(
                        "STT bridge exhausted %d reconnects call_id=%s: %s — "
                        "actor will fall back to batch STT on next utterance",
                        self._max_reconnects, self._actor.call_id, e,
                    )
                    await self._actor.emit(CallEvent.new(
                        call_id=self._actor.call_id,
                        tenant_id=self._actor.tenant_id,
                        source=EventSource.STT,
                        turn_generation=self._actor.turn_generation,
                        speech_generation=self._actor.speech_generation,
                        kind="stream_failed",
                        payload={"error": str(e), "reconnects": self._reconnect_count},
                    ))
                    return
                log.warning(
                    "STT bridge error call_id=%s (reconnect %d/%d): %s",
                    self._actor.call_id, self._reconnect_count,
                    self._max_reconnects, e,
                )
                await asyncio.sleep(self._reconnect_backoff_s * self._reconnect_count)

    async def _stream_once(self) -> None:
        """One STT-provider session.  Loops until stop or provider
        end-of-stream.  Exceptions bubble up to _run for reconnect."""
        # If provider doesn't support streaming, no-op with a warning.
        if not getattr(self._stt, "supports_streaming", False):
            log.warning(
                "STT provider %s doesn't support streaming; bridge is idle",
                getattr(self._stt, "name", "?"),
            )
            await self._stop_event.wait()
            return

        # Build an async iterator over our audio queue
        async def _audio_iter() -> AsyncIterator[bytes]:
            while True:
                frame = await self._audio_queue.get()
                if frame is None or self._stop_event.is_set():
                    return
                yield frame

        # transcribe_stream(audio_iter) → yields STTEvent objects.
        # We remap each to a CallEvent and emit to the actor.
        # 2026-08-08: send mulaw DIRECTLY to Deepgram when the input
        # is Twilio (mulaw_input=True).  Old code converted mulaw→
        # linear16 on our side, which was creating format-drift bugs
        # per docs/rnd-2026-08/53-fast-stt-alternatives.md — Deepgram
        # discards audio when declared encoding doesn't match binary
        # framing, producing 20-40s "silent discard" first-turn hangs.
        # Native mulaw = zero conversion, Deepgram handles it fine.
        # Browser path stays linear16 (already 16kHz PCM).
        if self._mulaw_input:
            sample_rate = 8000
            encoding = "mulaw"
        else:
            sample_rate = 16000
            encoding = "linear16"

        async for stt_ev in self._stt.transcribe_stream(
            _audio_iter(),
            sample_rate=sample_rate,
            encoding=encoding,
        ):
            if self._stop_event.is_set():
                return
            kind = stt_ev.kind
            text = getattr(stt_ev, "text", "") or ""
            is_final = getattr(stt_ev, "is_final", False)
            speech_final = getattr(stt_ev, "speech_final", False)
            # Map STT-provider event kinds to actor event kinds.
            # Everything gets stamped with current generation so late
            # events after a bump_turn get dropped.
            await self._actor.emit(CallEvent.new(
                call_id=self._actor.call_id,
                tenant_id=self._actor.tenant_id,
                source=EventSource.STT,
                turn_generation=self._actor.turn_generation,
                speech_generation=self._actor.speech_generation,
                kind=kind,
                payload={"text": text, "is_final": is_final, "speech_final": speech_final},
            ))
