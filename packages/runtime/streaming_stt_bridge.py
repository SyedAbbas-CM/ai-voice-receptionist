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
        # Convert mulaw → linear16 if needed (Deepgram wants linear16
        # for the encoding=linear16 param).  We keep the option to
        # skip conversion + set encoding=mulaw on the STT side, but
        # linear16 is more portable across providers.
        payload = frame
        if self._mulaw_input:
            try:
                payload = audioop.ulaw2lin(frame, 2)
            except Exception:
                return
        try:
            self._audio_queue.put_nowait(payload)
        except asyncio.QueueFull:
            # Drop.  Rate-limited log so we don't flood.
            if self._audio_queue.qsize() % 100 == 0:
                log.warning(
                    "STT bridge queue full call_id=%s; dropping frame",
                    self._actor.call_id,
                )

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
        # The linear16 sample rate is 8000 for Twilio, 16000 for browser.
        # Bridge doesn't know; caller sets via `mulaw_input`.
        sample_rate = 8000 if self._mulaw_input else 16000
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
                payload={"text": text, "is_final": is_final},
            ))
