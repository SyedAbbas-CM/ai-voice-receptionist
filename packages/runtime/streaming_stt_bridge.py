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
        # Bounded audio queue — bridge drops OLD frames if it can't
        # keep up (better than growing unbounded and processing seconds-
        # old speech that no longer helps the conversation).
        # 2026-08-21 NET-04: dropped 800→100. Previous 16-second cap
        # meant a stalled event loop could accumulate seconds of stale
        # audio then feed it to Deepgram all at once — Deepgram would
        # transcribe past speech and fire turn-taking events against
        # historical context.
        # 2026-08-22 NET Ship 3: raised 100→150 (3s hard cap). On
        # CAa7effd6273 the 100-frame cap was hit within the first
        # 1.4 seconds of the call (first-media burst) — dropped 140ms
        # of caller audio before speech even happened. 3s gives more
        # headroom for burst arrivals + event-loop spikes during long
        # TTS replies while still bounding stale-audio damage.
        self._audio_queue: asyncio.Queue = asyncio.Queue(maxsize=150)
        # 2026-08-21 NET-04: backpressure telemetry.
        # dropped_audio_ms grows every time queue-full forces us to
        # discard an old frame. peak_backlog_frames = high-water mark
        # across the call. last_backlog_log_at rate-limits the
        # STT_BACKLOG_STATE line so a sustained backlog doesn't spam.
        self._dropped_audio_ms: float = 0.0
        self._peak_backlog_frames: int = 0
        self._last_backlog_log_at: float = 0.0
        self._backlog_watchdog_task: Optional[asyncio.Task] = None
        # 2026-08-22 NET Ship 3: first-burst frame count.  Twilio can
        # deliver 1-2 seconds of media frames in one microtask cluster
        # at call start (buffered up during WS handshake).  If burst
        # exceeds the queue cap the frames drop before any real audio
        # is heard.  Track the first ~2 seconds of feeds so a spike is
        # visible in the log without needing DEBUG-level tracing.
        self._first_burst_frames: int = 0
        self._first_burst_deadline_ns: Optional[int] = None
        self._first_burst_logged: bool = False
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
        # 2026-08-21 NET-02: persistent ratecv state so 8k→16k upsample
        # doesn't reset between frames (which would click at every boundary).
        self._smartturn_rate_state = None

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
        # 2026-08-21 NET-04: periodic backlog gauge. Wakes every 1s,
        # logs STT_BACKLOG_STATE only when qsize > 25 frames (500ms of
        # backpressure). Terminates when stop_event is set.
        self._backlog_watchdog_task = asyncio.create_task(
            self._backlog_watchdog(),
            name=f"stt-backlog-{self._actor.call_id}",
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
        # 2026-08-21 NET-04: emit final backlog summary if we ever
        # dropped audio during this call — durable evidence for post-
        # call review even if the sustained-state logs never fired.
        if self._dropped_audio_ms > 0 or self._peak_backlog_frames > 25:
            log.info(
                "STT_BACKLOG_SUMMARY call=%s peak_frames=%d peak_backlog_ms=%.0f "
                "dropped_frames=%d dropped_ms=%.0f",
                self._actor.call_id, self._peak_backlog_frames,
                self._peak_backlog_frames * 20.0,
                int(self._dropped_audio_ms / 20.0),
                self._dropped_audio_ms,
            )
        # Cancel the backlog watchdog before waiting on the consumer.
        if self._backlog_watchdog_task is not None and not self._backlog_watchdog_task.done():
            self._backlog_watchdog_task.cancel()
        if self._consumer_task is not None:
            try:
                await asyncio.wait_for(self._consumer_task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._consumer_task.cancel()

    async def _backlog_watchdog(self) -> None:
        """2026-08-21 NET-04: periodic backpressure gauge.

        Wakes every 1s. If the STT audio queue has more than 25 frames
        (500ms of backpressure) queued, log STT_BACKLOG_STATE so operators
        can spot sustained realtime slippage. Terminates on stop_event.
        Rate-limiting is implicit — one log per second only when the
        queue is meaningfully backlogged.
        """
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    return
                qsize = self._audio_queue.qsize()
                if qsize > 25:
                    log.warning(
                        "STT_BACKLOG_STATE call=%s qsize=%d backlog_ms=%.0f "
                        "peak_frames=%d dropped_ms=%.0f",
                        self._actor.call_id, qsize, qsize * 20.0,
                        self._peak_backlog_frames, self._dropped_audio_ms,
                    )
        except asyncio.CancelledError:
            return

    def feed(self, frame: bytes) -> None:
        """Push one inbound audio frame into the bridge.  Non-blocking;
        drops on backpressure (with a rate-limited log)."""
        if not frame or self._stop_event.is_set():
            return
        # 2026-08-22 NET Ship 3: first-burst frame count.  On the first
        # feed, start a 2-second deadline.  Count every feed within
        # that window and emit STT_FIRST_BURST at end so ops can see
        # if Twilio dumped a huge cluster at call-start.  Deadline uses
        # monotonic_ns to avoid clock drift.
        if self._first_burst_deadline_ns is None:
            self._first_burst_deadline_ns = time.monotonic_ns() + 2_000_000_000
        if not self._first_burst_logged:
            _now_ns = time.monotonic_ns()
            if _now_ns < self._first_burst_deadline_ns:
                self._first_burst_frames += 1
            else:
                self._first_burst_logged = True
                # Only log if the burst was meaningful (>50 frames = 1s
                # of audio in the first 2s — cleanly above steady-state
                # 50 fps rate).
                if self._first_burst_frames > 50:
                    log.info(
                        "STT_FIRST_BURST call=%s frames_in_first_2s=%d "
                        "(steady-state=100)",
                        self._actor.call_id, self._first_burst_frames,
                    )
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
            # freshest 2s of audio even under backpressure.
            try:
                self._audio_queue.get_nowait()
                # 2026-08-21 NET-04: track cumulative dropped audio.
                # Every Twilio frame is 20ms of μ-law audio.
                self._dropped_audio_ms += 20.0
            except asyncio.QueueEmpty:
                pass
            try:
                self._audio_queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass
            # 2026-08-21 NET-04: log on power-of-two thresholds so
            # short bursts get one line and sustained backlog escalates
            # (log at 1, 8, 64, 512, 4096 dropped frames).
            _dropped_frames = int(self._dropped_audio_ms / 20.0)
            if _dropped_frames > 0 and (_dropped_frames & (_dropped_frames - 1)) == 0:
                log.warning(
                    "STT_DROP call=%s qsize=%d dropped_frames=%d dropped_ms=%.0f",
                    self._actor.call_id, self._audio_queue.qsize(),
                    _dropped_frames, self._dropped_audio_ms,
                )
        # 2026-08-21 NET-04: track peak backlog high-water mark.
        _now_qsize = self._audio_queue.qsize()
        if _now_qsize > self._peak_backlog_frames:
            self._peak_backlog_frames = _now_qsize

        # S13-A smart-turn: keep a rolling 16kHz PCM buffer.
        # 2026-08-21 NET-02 FIX: `payload` here is RAW μ-LAW (since the
        # 2026-08-08 Deepgram-native-mulaw change removed the ulaw2lin
        # step). ratecv(width=2) expects 16-bit linear PCM — feeding it
        # raw μ-law produced garbage bytes, which meant SmartTurn has
        # been classifying malformed audio for every turn since Aug 8.
        # Correct pipeline: μ-law@8k → linear16@8k → linear16@16k, with
        # rate_state persisted across frames so no click at boundaries.
        try:
            if self._mulaw_input:
                lin8k = audioop.ulaw2lin(payload, 2)
            else:
                lin8k = payload
            pcm16k, self._smartturn_rate_state = audioop.ratecv(
                lin8k, 2, 1, 8000, 16000, self._smartturn_rate_state,
            )
        except Exception:
            pcm16k = b""
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

    # ── Flux-only audio pipeline ───────────────────────────────────
    #
    # 2026-08-20: Deepgram Flux (v2/listen) closes the WS with code
    # 1005 after ~3-5 s when we send raw Twilio mulaw@8k (verified on
    # trace CA560c5d and Deepgram GH issue #649).  Their reference
    # `flux-twilio-voice-assistant` upsamples to linear16@48k and
    # sends 80 ms chunks, so we mirror that shape here.  Nova-3 path
    # is untouched — this iterator is only used when the resolved
    # STT provider is `deepgram_flux`.
    #
    # Input:  raw Twilio mulaw@8k, ~20 ms per frame (160 bytes).
    # Output: linear16@48k, exactly 80 ms per emitted chunk
    #         (48000 * 0.08 * 2 = 7680 bytes).

    _FLUX_CHUNK_MS: int = 80
    # 48000 samples/sec * 0.080 sec * 2 bytes/sample = 7680 bytes
    _FLUX_CHUNK_BYTES: int = 7680

    async def _flux_audio_iter(self, source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        buf = bytearray()
        rate_state = None  # persistent for audioop.ratecv across frames
        async for mulaw in source:
            if not mulaw:
                continue
            try:
                # 1. mulaw@8k → linear16@8k (2× byte count)
                lin8k = audioop.ulaw2lin(mulaw, 2)
                # 2. linear16@8k → linear16@48k (upsample 6×)
                lin48k, rate_state = audioop.ratecv(
                    lin8k, 2, 1, 8000, 48000, rate_state,
                )
            except Exception as e:
                log.warning("flux audio convert failed: %s", e)
                continue
            buf.extend(lin48k)
            # 3. Flush every 80 ms boundary.  Multiple boundaries per
            # loop iteration are possible only if upstream buffered.
            while len(buf) >= self._FLUX_CHUNK_BYTES:
                yield bytes(buf[: self._FLUX_CHUNK_BYTES])
                del buf[: self._FLUX_CHUNK_BYTES]
        # Tail: emit any partial buffer at shutdown (better than dropping).
        if buf:
            yield bytes(buf)

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
                # 2026-08-21 NET-24: if the call is already shutting
                # down, do NOT try to reconnect. Flux commonly closes
                # its WS with 1005 during graceful hangup; treating
                # that as an error and triggering 3 reconnect attempts
                # spams the log + burns Deepgram request slots + can
                # produce spurious warnings on every successful call.
                if self._stop_event.is_set():
                    log.info(
                        "STT bridge close during shutdown call_id=%s (%s) — no reconnect",
                        self._actor.call_id, type(e).__name__,
                    )
                    return
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

        # 2026-08-20: Flux-specific audio path.  Deepgram Flux (v2)
        # rejects raw mulaw@8k after ~3-5s of audio with WS close 1005
        # (verified on CA560c5d and Deepgram issue #649).  Their
        # reference `flux-twilio-voice-assistant` upsamples Twilio's
        # mulaw@8k → linear16@48k and chunks at 80ms — the docs also
        # call out "80ms audio chunks strongly recommended".  We
        # convert here ONLY for Flux; Nova-3 keeps the native mulaw
        # zero-transcode path.
        stt_name = getattr(self._stt, "name", "")
        use_flux_audio = self._mulaw_input and stt_name == "deepgram_flux"
        if use_flux_audio:
            sample_rate = 48000
            encoding = "linear16"
            audio_source = self._flux_audio_iter(_audio_iter())
            log.info(
                "STT bridge: FLUX audio path (mulaw8k → linear16@48k, 80ms chunks) call=%s",
                self._actor.call_id,
            )
        else:
            audio_source = _audio_iter()

        async for stt_ev in self._stt.transcribe_stream(
            audio_source,
            sample_rate=sample_rate,
            encoding=encoding,
        ):
            if self._stop_event.is_set():
                return
            kind = stt_ev.kind
            text = getattr(stt_ev, "text", "") or ""
            is_final = getattr(stt_ev, "is_final", False)
            speech_final = getattr(stt_ev, "speech_final", False)
            # 2026-08-20: diagnostic — INFO log every final + every N-th
            # partial. Otherwise the "6.5s dead zone between agent stop
            # and brain fire" is invisible. text[:80] keeps line short.
            # 2026-08-20 (b): also surface speech_start / speech_end
            # (Deepgram SpeechStarted / UtteranceEnd) at INFO with the
            # call_id so the per-call log picks them up — without them
            # we can't see the "6.5s dead zone" between agent-stop
            # and first partial.
            if kind in ("speech_start", "speech_end"):
                log.info(
                    "STT_VAD call=%s gen=%d kind=%s",
                    self._actor.call_id, self._actor.turn_generation, kind,
                )
                # 2026-08-24: reset first-partial tracker on new turn boundary.
                if kind == "speech_start":
                    self._first_partial_this_turn = True
            elif is_final or speech_final:
                log.info(
                    "STT_FINAL call=%s gen=%d speech_final=%s is_final=%s text=%r",
                    self._actor.call_id, self._actor.turn_generation,
                    speech_final, is_final, text[:80],
                )
                # 2026-08-24: reset for next turn.
                self._first_partial_this_turn = True
            elif text:
                # 2026-08-24 ChatGPT audit item #1: log the FIRST partial
                # per turn (was every 5th, so measurements using "first
                # partial" as a landmark were all wrong — showing partial
                # #5 not #1). Now log first + every 5th after.
                _n = getattr(self, "_partial_log_count", 0) + 1
                self._partial_log_count = _n
                _first_of_turn = getattr(self, "_first_partial_this_turn", True)
                if _first_of_turn or _n % 5 == 0:
                    log.info(
                        "STT_PARTIAL call=%s gen=%d n=%d first=%s text=%r",
                        self._actor.call_id, self._actor.turn_generation,
                        _n, _first_of_turn, text[:60],
                    )
                    self._first_partial_this_turn = False
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
