"""Sprint 9a: CallActor-backed Twilio Media Stream handler.

The legacy `TwilioStreamSession` in `twilio.py` grew organically — it
mixes protocol parsing, VAD, STT batching, LLM invocation, TTS chunking,
barge-in classification and playback all inside one class with an
`interrupt_flag` Boolean.  That works for one call but has three
problems that the Sprint 8b/9 kernel exists to fix:

  1. Cancellation is a single Boolean — no notion of generation IDs, so
     a late STT partial from a superseded turn can still fire the brain.
  2. LLM history is appended with the FULL synthesized reply text even
     when the caller barged in halfway.  The model's next turn thinks
     it said things it did not actually say.
  3. Every turn spawns bare `asyncio.create_task(...)` with no owner.
     Nothing to cancel on hang-up beyond the websocket close.

This module presents the SAME wire-level behaviour (µ-law in, µ-law
out, backchannel vs. interrupt classification) but routes every signal
through a `CallActor` — so the ledger tracks heard vs. generated, and
`bump_turn` cancels in-flight work by generation ID.

Feature-gated by `settings.twilio_use_actor` — flip on to route
inbound calls through this path.  Legacy path stays as fallback.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import WebSocket

from app.core import session_manager
from app.core.config import settings
from app.providers import get_stt

from packages.runtime import (
    AudioChunk,
    CallActor,
    CallEvent,
    CallState,
    EventSource,
    StreamingSTTBridge,
    TurnEventKind,
    TurnManager,
    TurnManagerConfig,
    get_registry,
)
from packages.runtime import telemetry as _tel

# Sprint 9e: two-planner path.  Imports are conditional (VPL isn't
# required unless the flag is on) but pulled here so the type checker
# sees them.  Real gating happens in _speak below.
from packages.voice.vpl import (
    SpeechAct,
    VPLUtterance,
    default_delivery_for,
    validate_vpl,
    VPLValidationError,
)
from packages.voice.vpl.validator import validate_vpl_and_repair
from packages.voice.vpl.compilers import compile_elevenlabs
from packages.core_agent.planners import PerformancePlanner
from packages.core_agent.planners.semantic import _infer_speech_act


def _apply_mulaw_gain(mulaw: bytes, gain_db: float) -> bytes:
    """Apply a linear gain (in dB) to µ-law audio.

    Fix for 2026-08-04 quiet-phone-voice complaint.  µ-law is a
    non-linear compander so we decode → apply linear multiplier →
    re-encode.  Uses the stdlib `audioop` module (audioop-lts on 3.13+).
    Clips to int16 range on overflow rather than raising."""
    if abs(gain_db) < 0.01:
        return mulaw
    try:
        import audioop
        # µ-law → linear16 (2 bytes/sample)
        linear = audioop.ulaw2lin(mulaw, 2)
        # 10^(dB/20) = linear amplitude ratio
        factor = 10.0 ** (gain_db / 20.0)
        # audioop.mul accepts a floatish factor; clips to [-32768, 32767]
        boosted = audioop.mul(linear, 2, factor)
        return audioop.lin2ulaw(boosted, 2)
    except Exception as e:
        log.warning("mulaw gain failed (gain_db=%.1f): %s — sending unchanged",
                    gain_db, e)
        return mulaw


def _infer_speech_act_from_payload(payload: dict) -> str:
    """Post-hoc inference from the session_manager payload.

    This is a bridge until the brain prompt is extended to emit
    speech_act directly.  The equivalent logic in
    packages.core_agent.planners.semantic._infer_speech_act consumes a
    BrainTurnResult; here we adapt the session-manager JSON."""
    reply = (payload.get("reply") or "")
    tool_results = payload.get("tool_results") or []
    escalated = bool(payload.get("escalated"))

    # Build a minimal object with the shape _infer_speech_act needs
    class _Adapter:
        pass
    adapter = _Adapter()
    adapter.reply = reply
    adapter.speech_act = payload.get("speech_act", "neutral")
    adapter.escalated = escalated
    adapter.tool_results = tool_results

    return _infer_speech_act(adapter).value


log = logging.getLogger(__name__)


def _looks_like_agent_echo(transcript: str, recent_agent: list[str]) -> bool:
    """Sprint 12 Track B: return True if `transcript` is almost certainly
    the mic picking up the agent's own speaker output rather than a
    real caller utterance.

    Heuristic: normalize both to lowercase word bags and check that
    the transcript's meaningful-word set is a subset of any recent
    agent utterance with a high overlap ratio.  We only reject if:
      - transcript is at least 3 words long (avoid nuking short real
        answers like "yes" / "cleaning" / "tomorrow")
      - AND ≥60% of the transcript's words appeared in a recent agent
        utterance
    Short caller turns pass through even if they happen to match a
    common word in the agent's speech."""
    import re as _re
    words = _re.findall(r"[a-z']+", transcript.lower())
    if len(words) < 3:
        return False
    trans_set = set(words)
    for agent_utt in recent_agent:
        agent_words = set(_re.findall(r"[a-z']+", agent_utt.lower()))
        if not agent_words:
            continue
        overlap = len(trans_set & agent_words)
        if overlap / max(len(trans_set), 1) >= 0.6:
            return True
    return False


TWILIO_SAMPLE_RATE = 8000
TWILIO_FRAME_MS = 20
SILENCE_HANG_MS = 700
MAX_UTTERANCE_MS = 12000
MIN_UTTERANCE_MS = 400
BARGE_MIN_AUDIO_BYTES = 2400   # ~300ms of µ-law @ 8kHz
BARGE_CHECK_INTERVAL_MS = 500


# ── I/O adapter ─────────────────────────────────────────────────────
#
# The actor is transport-agnostic.  This class owns the Twilio-specific
# bits: websocket send, base64 framing, mark bookkeeping.  It emits
# CallEvents into the actor's mailbox and reacts to actor state.

class TwilioActorSession:
    """One per Twilio Media Stream.  Bridges protocol frames <-> CallActor.

    Lifecycle:
        session = TwilioActorSession(ws, stream_sid, call_id, tenant_id)
        await session.start()                     # spins up actor + greeting
        await session.on_media(mulaw_frame)       # per inbound frame
        await session.on_mark_ack(mark_id)        # per Twilio mark webhook
        await session.stop("hangup")              # cleanup
    """

    def __init__(
        self,
        ws: WebSocket,
        stream_sid: str,
        call_id: str,
        tenant_id: str,
        session_id: Optional[str] = None,
    ) -> None:
        self.ws = ws
        self.stream_sid = stream_sid
        self.call_id = call_id
        self.tenant_id = tenant_id
        # session_id is the brain's session key — keeps back-compat with
        # session_manager which was built pre-actor.
        self.session_id = session_id or f"twilio_{call_id}"

        # VAD-based utterance framing (unchanged from legacy path)
        self._buffer = bytearray()
        self._last_voiced_ms: Optional[float] = None
        self._utterance_started_ms: Optional[float] = None

        # Barge-in scratch state (still owned by the adapter — the actor
        # only sees the events it emits)
        self._barge_buffer = bytearray()
        self._barge_last_voiced_ms: Optional[float] = None
        self._barge_last_check_ms = 0.0

        # Per-mark bookkeeping so we can map Twilio's mark webhook back
        # to the audio chunk it acknowledges.
        self._mark_counter = 0

        # Current turn's telemetry span (opened on utterance close,
        # finalized when the reply's first byte hits the wire).
        self._current_turn_span: Optional[_tel.TurnSpan] = None
        self._turn_span_cm = None
        # Wall-clock of the moment the caller's utterance closed —
        # anchor for the media_in mark.
        self._turn_start_ns: Optional[int] = None

        # Sprint 9e: per-turn speech_act inferred by the semantic
        # planner; consumed by _stream_tts if TWO_PLANNER_ENABLED=true.
        # Stashed here (rather than plumbed through method args) to
        # keep the barge-in path unchanged.
        self._current_speech_act: Optional[str] = None

        # Sprint 9e: performance planner is lazily constructed on first
        # use so tests can substitute _perf_planner directly.
        self._perf_planner = None

        # Sprint 9f: two-stage barge-in state.
        # ducked=True: _send_mulaw_frames skips outbound frames.
        # stage2_deadline_task: scheduled coroutine that unducks if the
        # classifier hasn't fired within barge_stage2_deadline_ms.
        self._ducked = False
        self._stage2_deadline_task: Optional[asyncio.Task] = None

        # Sprint 10 STREAMING WIRING: bridge + turn manager.  Owned by
        # the session so start()/stop() lifecycle mirrors the call.
        self._stt_bridge: Optional[StreamingSTTBridge] = None
        self._turn_manager: Optional[TurnManager] = None
        # Rolling text buffer captured from streaming STT so END_OF_TURN
        # has a final utterance to feed the brain.
        self._streaming_utterance_text = ""

        # Idle-followup: after the agent finishes speaking, we wait
        # for the caller.  If they stay silent, we prompt once ("Anything
        # else?"), then say goodbye + hangup on the next silence window.
        self._idle_task: Optional[asyncio.Task] = None
        self._idle_prompted: bool = False

        # Sprint 12 Track B addendum: echo suppression.  Track the last
        # 3 agent utterances (a short rolling buffer) so we can drop
        # STT finals that are actually just the mic hearing our own
        # speaker.  Only reject finals that overlap significantly with
        # something the agent JUST said.
        self._recent_agent_utterances: list[str] = []

        self.actor: Optional[CallActor] = None

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind or create the CallActor + fire the greeting."""
        self.actor = await get_registry().get_or_create(
            call_id=self.call_id,
            tenant_id=self.tenant_id,
            setup=self._wire_handlers,
        )

        # Sprint 10 STREAMING WIRING: spin up the STT bridge + turn
        # manager BEFORE the greeting so caller barge-in during the
        # greeting flows through the same semantic pipeline.
        if settings.streaming_stt_enabled:
            try:
                from app.providers import get_stt
                self._stt_bridge = StreamingSTTBridge(
                    actor=self.actor, stt_provider=get_stt(),
                    mulaw_input=True,
                )
                await self._stt_bridge.start()
                log.info("streaming STT bridge started for call=%s", self.call_id)
            except Exception as e:
                log.warning("streaming STT bridge disabled: %s", e)
                self._stt_bridge = None
        if settings.turn_manager_enabled:
            try:
                self._turn_manager = TurnManager(
                    actor=self.actor, config=TurnManagerConfig(),
                )
                log.info("turn manager attached for call=%s", self.call_id)
            except Exception as e:
                log.warning("turn manager disabled: %s", e)
                self._turn_manager = None

        # Kick greeting through the same code path as normal replies so
        # ledger + generation tracking apply from turn 0.
        state, brain = session_manager.start_session_with_id(
            self.session_id, tenant_id=self.tenant_id,
        )
        greeting = await session_manager.run_greeting(state, brain)
        self.actor.transition(CallState.GREETING)
        await self._speak(greeting)

    async def stop(self, reason: str = "hangup") -> None:
        self._close_turn_span()
        # Don't leak the idle-followup timer past the call
        self._cancel_idle_followup()
        # Sprint 9f: don't leak the stage-2 deadline task on hangup
        if self._stage2_deadline_task and not self._stage2_deadline_task.done():
            self._stage2_deadline_task.cancel()
            self._stage2_deadline_task = None
        # Sprint 10 STREAMING WIRING: shut the STT bridge cleanly
        if self._stt_bridge is not None:
            try:
                await self._stt_bridge.stop()
            except Exception:
                pass
            self._stt_bridge = None
        try:
            await session_manager.end_session_async(
                self.session_id, tenant_id=self.tenant_id,
            )
        except Exception:
            pass
        await get_registry().stop(self.call_id, self.tenant_id, reason=reason)

    # ── handler wiring (called once at actor creation) ──────────────

    def _wire_handlers(self, actor: CallActor) -> None:
        """Register the (source, kind) → coroutine table on the actor.

        Kept as closures over `self` so handlers can reach the websocket.
        Actor calls these serially in mailbox order under the current
        turn_generation guard, so a stale STT partial from a superseded
        turn never reaches _on_stt_final."""
        actor.handlers[(EventSource.MEDIA, "utterance_ready")] = self._on_utterance_ready
        actor.handlers[(EventSource.STT, "barge_candidate")] = self._on_barge_candidate
        actor.handlers[(EventSource.PLAYBACK, "mark_ack")] = self._on_mark_ack_handler

        # Sprint 10 STREAMING WIRING: subscribe to streaming STT + turn
        # events.  Each fires _tel counter + call event log write for
        # demo observability, then routes to the specific handler.
        if settings.streaming_stt_enabled:
            actor.handlers[(EventSource.STT, "partial")] = self._on_stt_partial
            actor.handlers[(EventSource.STT, "final")] = self._on_stt_final
            actor.handlers[(EventSource.STT, "speech_start")] = self._on_stt_speech_signal
            actor.handlers[(EventSource.STT, "speech_end")] = self._on_stt_speech_signal
            actor.handlers[(EventSource.STT, "stream_failed")] = self._on_stt_stream_failed
        if settings.turn_manager_enabled:
            actor.handlers[(EventSource.CONTROL, TurnEventKind.EAGER_END_OF_TURN.value)] = self._on_turn_event
            actor.handlers[(EventSource.CONTROL, TurnEventKind.END_OF_TURN.value)] = self._on_turn_event_end
            actor.handlers[(EventSource.CONTROL, TurnEventKind.TURN_RESUMED.value)] = self._on_turn_event
            actor.handlers[(EventSource.CONTROL, TurnEventKind.BACKCHANNEL.value)] = self._on_turn_event_backchannel
            actor.handlers[(EventSource.CONTROL, TurnEventKind.INTERRUPTION.value)] = self._on_turn_event_interruption
            actor.handlers[(EventSource.CONTROL, TurnEventKind.USER_REQUESTED_PAUSE.value)] = self._on_turn_event_pause
            actor.handlers[(EventSource.CONTROL, TurnEventKind.FALSE_INTERRUPTION.value)] = self._on_turn_event_false_int
        # Sprint 12 Track A: brain + speech job completion handlers.
        # These fire from control events emitted BY the supervised jobs
        # spawned from _on_turn_event_end (nonblocking path).
        actor.handlers[(EventSource.CONTROL, "brain_completed")] = self._on_brain_completed
        actor.handlers[(EventSource.CONTROL, "brain_failed")] = self._on_brain_failed
        actor.handlers[(EventSource.CONTROL, "speech_completed")] = self._on_speech_completed

    # ── inbound events (called by the /twilio/stream loop) ──────────

    async def on_media(self, mulaw_frame: bytes) -> None:
        """One inbound Twilio media frame.  Routes to either the
        utterance-buffering path (idle) or the barge-detection path
        (agent speaking).

        Sprint 10 STREAMING WIRING: when streaming_stt_enabled, ALSO
        feed the bridge on every frame regardless of state.  Streaming
        STT runs in parallel to VAD-based batching until we're
        confident the streaming path works — then we drop the batch
        path entirely."""
        if self.actor is None:
            return

        # Feed bridge on every inbound frame (idempotent, no-op if disabled)
        if self._stt_bridge is not None:
            self._stt_bridge.feed(mulaw_frame)

        # Sprint 12 Track B: kill the split-brain barge system.  When
        # streaming STT + turn manager are the authority, the legacy
        # VAD/batch _buffer_barge_frame path just duplicates work,
        # hammers the Deepgram REST endpoint (causing 408 timeouts),
        # and fires the brain twice on the same interruption.
        # Only run the legacy path when the streaming path is OFF.
        streaming_barge_active = (
            settings.streaming_stt_enabled
            and settings.turn_manager_enabled
            and self._stt_bridge is not None
            and self._turn_manager is not None
        )
        if self.actor.state == CallState.SPEAKING:
            if not streaming_barge_active:
                await self._buffer_barge_frame(mulaw_frame)
            return

        # Sprint 10 STREAMING WIRING: when turn_manager is enabled, the
        # TurnManager's END_OF_TURN event triggers the brain, not the
        # VAD silence-close.  Skip the batch utterance-buffering path
        # in that mode.
        if settings.turn_manager_enabled and self._turn_manager is not None:
            return

        await self._buffer_utterance_frame(mulaw_frame)

    async def on_mark_ack(self, mark_id: str) -> None:
        """Twilio's `mark` webhook fired — the mark has been played out."""
        if self.actor is None:
            return
        await self.actor.emit(CallEvent.new(
            call_id=self.call_id,
            tenant_id=self.tenant_id,
            source=EventSource.PLAYBACK,
            turn_generation=self.actor.turn_generation,
            speech_generation=self.actor.speech_generation,
            kind="mark_ack",
            payload=mark_id,
        ))

    # ── utterance framing (VAD + silence detection) ─────────────────

    async def _buffer_utterance_frame(self, mulaw_frame: bytes) -> None:
        from app.routes.twilio import _get_vad
        now = time.time() * 1000
        is_speech = bool(mulaw_frame) and _get_vad().is_speech(
            mulaw_frame, sample_rate=TWILIO_SAMPLE_RATE, mime="audio/mulaw",
        )

        if is_speech:
            if self._utterance_started_ms is None:
                self._utterance_started_ms = now
            self._last_voiced_ms = now
            self._buffer.extend(mulaw_frame)
        elif self._utterance_started_ms is not None:
            self._buffer.extend(mulaw_frame)

        if self._utterance_started_ms is None:
            return

        duration_ms = now - self._utterance_started_ms
        silence_ms = (now - self._last_voiced_ms) if self._last_voiced_ms else 0
        should_close = (
            duration_ms >= MAX_UTTERANCE_MS
            or (duration_ms >= MIN_UTTERANCE_MS and silence_ms >= SILENCE_HANG_MS)
        )

        if should_close:
            utterance = bytes(self._buffer)
            self._buffer.clear()
            self._utterance_started_ms = None
            self._last_voiced_ms = None
            # Bump the turn generation BEFORE emitting so the handler
            # runs under the new turn; late partials from turn N are
            # then dropped by the actor's generation guard.
            await self.actor.bump_turn(reason="utterance-end")
            # Open a per-turn telemetry span.  Finalized in _stream_tts
            # when the reply's first byte hits the wire (or on next
            # bump_turn / hangup, whichever comes first).
            self._open_turn_span(self.actor.turn_generation)
            if self._current_turn_span is not None:
                self._current_turn_span.mark("media_in")
            await self.actor.emit(CallEvent.new(
                call_id=self.call_id,
                tenant_id=self.tenant_id,
                source=EventSource.MEDIA,
                turn_generation=self.actor.turn_generation,
                speech_generation=self.actor.speech_generation,
                kind="utterance_ready",
                payload=utterance,
            ))

    def _open_turn_span(self, turn_gen: int) -> None:
        """Start a fresh TurnSpan context and stash the CM so we can
        exit it when the turn completes."""
        # Close any previous unfinalized span first (shouldn't happen
        # in normal flow, defensive).
        self._close_turn_span()
        cm = _tel.turn_span(
            call_id=self.call_id,
            tenant_id=self.tenant_id,
            turn_generation=turn_gen,
        )
        self._turn_span_cm = cm
        try:
            self._current_turn_span = cm.__enter__()
        except Exception:
            self._current_turn_span = None
            self._turn_span_cm = None

    def _close_turn_span(self) -> None:
        if self._turn_span_cm is not None:
            try:
                self._turn_span_cm.__exit__(None, None, None)
            except Exception:
                pass
            self._turn_span_cm = None
            self._current_turn_span = None

    # ── barge-in detection (agent speaking, caller might interrupt) ──

    async def _buffer_barge_frame(self, mulaw_frame: bytes) -> None:
        from app.routes.twilio import _get_vad
        if not mulaw_frame:
            return

        is_speech = _get_vad().is_speech(
            mulaw_frame, sample_rate=TWILIO_SAMPLE_RATE, mime="audio/mulaw",
        )
        now = time.time() * 1000

        # Sprint 9f: stage 1 — first speech frame during SPEAKING duck
        # immediately.  Runs BEFORE we buffer / classify so caller
        # perceives the pause sub-40ms rather than waiting for STT.
        if (
            is_speech
            and settings.two_stage_barge_in_enabled
            and not self._ducked
            and self.actor is not None
            and self.actor.state == CallState.SPEAKING
        ):
            self._begin_duck()

        if is_speech:
            self._barge_buffer.extend(mulaw_frame)
            self._barge_last_voiced_ms = now
        elif self._barge_last_voiced_ms is not None:
            self._barge_buffer.extend(mulaw_frame)

        if len(self._barge_buffer) < BARGE_MIN_AUDIO_BYTES:
            return
        if (now - self._barge_last_check_ms) < BARGE_CHECK_INTERVAL_MS:
            return

        self._barge_last_check_ms = now
        snapshot = bytes(self._barge_buffer)
        # STT+classify happens off the actor's task; result lands as an
        # event.  That keeps the actor's mailbox drain fast.
        asyncio.create_task(self._classify_barge(snapshot))

    async def _classify_barge(self, mulaw: bytes) -> None:
        """Runs STT + backchannel classifier off-actor.  Emits a
        BARGE_CANDIDATE event with the classification result."""
        try:
            from app.routes.twilio import _mulaw_frames_to_wav
            wav = _mulaw_frames_to_wav(mulaw)
            stt = get_stt()
            text = await stt.transcribe(
                wav, sample_rate=TWILIO_SAMPLE_RATE, mime="audio/wav",
            )
        except Exception as e:
            log.warning("actor barge STT failed: %s", e)
            return
        if not text.strip() or self.actor is None:
            return
        from packages.voice import classify_barge
        action = classify_barge(text)
        await self.actor.emit(CallEvent.new(
            call_id=self.call_id,
            tenant_id=self.tenant_id,
            source=EventSource.STT,
            turn_generation=self.actor.turn_generation,
            speech_generation=self.actor.speech_generation,
            kind="barge_candidate",
            payload={"text": text, "action": action.value},
        ))

    # ── actor handlers (invoked serially by the actor's run loop) ────

    async def _on_utterance_ready(
        self, actor: CallActor, event: CallEvent,
    ) -> bool:
        """Final utterance audio ready.  Runs STT + brain under the
        actor's current turn generation.  If a barge-in advances the
        turn again mid-flight, the generation guard drops our follow-up
        speak call."""
        mulaw: bytes = event.payload
        if len(mulaw) < 8000:  # <1s
            return True

        actor.transition(CallState.THINKING)
        turn_gen = event.turn_generation

        # Register the brain task so bump_turn can cancel it if the
        # caller starts talking again before we finish.
        brain_task = asyncio.create_task(
            self._run_brain(mulaw, turn_gen),
            name=f"brain-{self.call_id}-{turn_gen}",
        )
        actor.register_turn_task(brain_task)
        try:
            await brain_task
        except asyncio.CancelledError:
            log.info("brain cancelled by newer turn call_id=%s gen=%d",
                     self.call_id, turn_gen)
        return True

    async def _run_brain(self, mulaw: bytes, turn_gen: int) -> None:
        from app.routes.twilio import _mulaw_frames_to_wav
        span = self._current_turn_span
        try:
            wav = _mulaw_frames_to_wav(mulaw)
            stt = get_stt()
            transcript = await stt.transcribe(
                wav, sample_rate=TWILIO_SAMPLE_RATE, mime="audio/wav",
            )
            if span is not None:
                # We don't have provider-level partial-vs-final marks
                # (batch STT); record final as both.
                span.mark("stt_first_partial")
                span.mark("stt_final")
            if not transcript.strip():
                return

            log.info("actor %s turn=%d heard: %s",
                     self.session_id, turn_gen, transcript)
            handle = session_manager.get_session(
                self.session_id, tenant_id=self.tenant_id,
            )
            if handle is None:
                state, brain = session_manager.start_session_with_id(
                    self.session_id, tenant_id=self.tenant_id,
                )
            else:
                state, brain = handle

            payload = await session_manager.run_user_turn(state, brain, transcript)
            if span is not None:
                span.mark("llm_first_token")
            reply = (payload.get("reply") or "").strip()
            # Sprint 9e: extract speech_act from the brain payload if
            # present; otherwise infer deterministically from tool
            # results + text patterns.  Stashed for _stream_tts to pick
            # up (kept off the _speak signature to avoid touching the
            # barge-in interrupt-turn path in _on_barge_candidate).
            self._current_speech_act = _infer_speech_act_from_payload(payload)
            if reply:
                await self._speak(reply)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("actor _run_brain failed: %s", e)

    async def _on_barge_candidate(
        self, actor: CallActor, event: CallEvent,
    ) -> bool:
        """Classifier returned.  On INTERRUPT: bump_turn (cancels TTS,
        advances generation), clear Twilio buffer, and queue the
        caller's text as the next brain turn.  On CONTINUE (backchannel):
        do nothing — TTS keeps playing.

        Two-stage barge-in with acoustic ducking lands in Sprint 9f;
        this is still one-stage lexical classification, just now under
        proper generation control."""
        text = event.payload["text"]
        action = event.payload["action"]

        if action == "INTERRUPT":
            log.info("actor %s INTERRUPT: %r", self.session_id, text)
            # Sprint 9b: record barge severity BEFORE we clear the
            # generation.  The gauge answers "how much of the reply did
            # the caller hear before cutting us off?".
            gen = actor.speech_generation
            heard = actor.ledger.heard_text_for(gen)
            generated = ""
            try:
                # Ledger keeps _generations dict private; peek to grab
                # the full utterance for the ratio calc.
                entry = actor.ledger._generations.get(gen)  # type: ignore[attr-defined]
                if entry is not None:
                    generated = entry.full_text
            except Exception:
                pass
            _tel.record_heard_vs_generated(
                tenant_id=self.tenant_id,
                heard_chars=len(heard),
                generated_chars=len(generated),
            )
            _tel.record_barge_in(self.tenant_id)

            # Sprint 9f: resolve the duck (if any) BEFORE clear+bump so
            # the metric bookkeeping runs while we're still YIELDING.
            self._end_duck("confirmed_interrupt")

            # Sprint 10 C3 (the audit's called-out moat): reconcile
            # the LLM's transcript BEFORE bump_turn.  Otherwise the
            # brain's next context still thinks the full planned reply
            # was heard.  Rewrite the assistant turn to what the
            # ledger says was actually delivered.
            try:
                from packages.runtime import reconcile_transcript_on_interrupt
                handle_for_state = session_manager.get_session(
                    self.session_id, tenant_id=self.tenant_id,
                )
                if handle_for_state is not None:
                    state_for_reconcile, _brain = handle_for_state
                    reconciled = reconcile_transcript_on_interrupt(
                        state_for_reconcile, actor.ledger, gen,
                    )
                    if reconciled is not None:
                        log.info(
                            "call=%s reconciled transcript: %d chars heard of %d planned",
                            self.call_id, len(reconciled), len(generated),
                        )
            except Exception as _e:
                log.warning("heard-text reconciliation failed: %s", _e)

            await self._send_twilio_clear()
            await actor.bump_turn(reason="barge-in")
            # Barge-in also invalidates any open turn span — the reply
            # never got its first audible byte.
            self._close_turn_span()
            self._barge_buffer.clear()
            self._barge_last_voiced_ms = None
            # Kick a new brain turn for the interrupt text.  Same
            # generation guard applies — if the caller keeps talking,
            # this brain call gets cancelled too.
            handle = session_manager.get_session(
                self.session_id, tenant_id=self.tenant_id,
            )
            if handle is not None:
                state, brain = handle
                try:
                    payload = await session_manager.run_user_turn(state, brain, text)
                    reply = (payload.get("reply") or "").strip()
                    if reply:
                        await self._speak(reply)
                except Exception as e:
                    log.exception("interrupt-turn failed: %s", e)
        elif action == "CONTINUE":
            _tel.record_backchannel(self.tenant_id)
            # Sprint 9f: backchannel confirmed → release the duck so
            # outbound frames flow again.  No state change beyond
            # YIELDING → SPEAKING (handled inside _end_duck).
            self._end_duck("backchannel_unduck")
            self._barge_buffer.clear()
            self._barge_last_voiced_ms = None
        # IGNORE — leave buffer, wait for more audio.  Duck (if any)
        # stays engaged until the stage-2 deadline resolves it as a
        # false trigger.
        return True

    async def _on_mark_ack_handler(
        self, actor: CallActor, event: CallEvent,
    ) -> bool:
        actor.ledger.mark_ack(actor.speech_generation, event.payload)
        return True

    # ── outbound TTS ─────────────────────────────────────────────────

    # ── Idle-followup: prompt then hangup on caller silence ──────────

    _IDLE_FIRST_PROMPT_S: float = 15.0
    _IDLE_HANGUP_AFTER_PROMPT_S: float = 15.0
    _IDLE_FAREWELL: str = "Alright, thanks for calling Smile Dental. Have a great day!"
    _IDLE_PROMPT: str = "Anything else I can help you with?"

    def _arm_idle_followup(self) -> None:
        """Start the idle-timeout ladder.  Cancels any previous idle
        task so successive agent turns reset the clock."""
        self._cancel_idle_followup()
        self._idle_prompted = False
        self._idle_task = asyncio.create_task(
            self._idle_followup_loop(),
            name=f"idle-{self.call_id}",
        )

    def _cancel_idle_followup(self) -> None:
        """Caller spoke — kill the pending idle prompt/hangup."""
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None
        self._idle_prompted = False

    async def _idle_followup_loop(self) -> None:
        try:
            # First silence window — nudge if the caller stays quiet.
            await asyncio.sleep(self._IDLE_FIRST_PROMPT_S)
            if self.actor is None or self.actor.state != CallState.LISTENING:
                return
            self._idle_prompted = True
            await self._speak(self._IDLE_PROMPT)
            # Second window — say goodbye and hangup.
            await asyncio.sleep(self._IDLE_HANGUP_AFTER_PROMPT_S)
            if self.actor is None or self.actor.state != CallState.LISTENING:
                return
            await self._speak(self._IDLE_FAREWELL)
            # Give the farewell time to actually stream out before we tear down.
            await asyncio.sleep(2.0)
            await self.stop(reason="idle_timeout")
        except asyncio.CancelledError:
            pass

    async def _speak(self, text: str) -> None:
        """Synthesize `text`, chunk it, send to Twilio, register each
        chunk in the ledger with a mark ID.  Cancellable — bump_turn
        or bump_speech cancels this task and drops queued audio."""
        actor = self.actor
        if actor is None:
            return

        # Sprint 12 Track B addendum: remember what the agent just said
        # so we can filter STT finals that are actually mic-hearing-speaker
        # echo.  Rolling buffer of last 3 utterances (~15 sec at typical
        # pace) since Deepgram lag can arrive multi-utterance-late.
        self._recent_agent_utterances.append(text)
        if len(self._recent_agent_utterances) > 3:
            self._recent_agent_utterances.pop(0)

        # Log utterance so /debug/call/{id}/timeline shows what the agent said
        try:
            from packages.observability.call_event_log import (
                get_call_event_log, CallEvent as _CE, EventSourceKind as _SK,
            )
            get_call_event_log().write(_CE(
                call_id=self.session_id, tenant_id=self.tenant_id,
                source=_SK.TTS, kind="utterance",
                payload={"text": text},
                turn_generation=actor.turn_generation,
            ))
        except Exception:
            pass

        actor.transition(CallState.SPEAKING)
        gen = actor.speech_generation
        actor.ledger.start_generation(gen, text)

        speech_task = asyncio.create_task(
            self._stream_tts(text, gen),
            name=f"tts-{self.call_id}-{gen}",
        )
        actor.register_speech_task(speech_task)
        try:
            await speech_task
        except asyncio.CancelledError:
            log.info("speech cancelled call_id=%s gen=%d", self.call_id, gen)
        finally:
            if actor.state == CallState.SPEAKING:
                actor.transition(CallState.LISTENING)
            # After the agent finishes speaking, arm an idle-followup
            # timer.  If the caller stays silent for 15s we nudge with
            # "Anything else?"; another 15s of silence → say goodbye
            # and hang up.  Cancelled the moment END_OF_TURN fires
            # (i.e. the caller says something).
            self._arm_idle_followup()

    async def _stream_tts(self, text: str, gen: int) -> None:
        """Do the actual synth + send.  Broken out so it's a
        cancellable Task registered with the actor.

        Sprint 9e: when settings.two_planner_enabled=true AND the
        current TTS provider is ElevenLabs, we run through the VPL
        compiler path.  Everything else falls through to the direct
        synthesize(text) path so browser/greeting/legacy callers stay
        untouched."""
        from app.routes.twilio import _get_telephony_tts
        span = self._current_turn_span
        try:
            if span is not None:
                span.mark("tts_request")
            tts = _get_telephony_tts()

            audio_bytes: bytes
            mime: str
            if settings.two_planner_enabled and self._provider_supports_vpl(tts):
                audio_bytes, mime = await self._vpl_synthesize(text, tts)
            else:
                audio_bytes, mime = await tts.synthesize(text)

            if span is not None:
                span.mark("tts_first_byte")
                # Finalize the turn span: this is the boundary the doc's
                # latency budget targets (end-of-turn → first audible
                # response byte).  Everything after is playback timing.
                self._close_turn_span()
            if mime == "text/x-browser-speak":
                log.warning("browser TTS can't drive telephony")
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("actor speak failed: %s", e)
            return

        # Ledger entry sized by the PCM bytes going out.  Duration math is
        # bytes / (rate * bytes_per_sample / 1000) — the outbound sender
        # knows how to encode to whatever wire format the transport needs.
        self._mark_counter += 1
        mark_id = f"m{gen}-{self._mark_counter}"
        chunk = AudioChunk(
            generation_id=f"gen-{gen}",
            sequence=0,
            audio_bytes=len(audio_bytes),
            duration_ms=int(len(audio_bytes) / 32),  # 16kHz s16le = 32 bytes/ms
            text=text,
            text_start=0,
            text_end=len(text),
            mark_id=mark_id,
            is_final=True,
        )
        if self.actor is not None:
            self.actor.ledger.queue_chunk(gen, chunk)

        await self._send_audio_frames(audio_bytes, mime)
        await self._send_twilio_mark(mark_id)

    # ── Sprint 9e: two-planner + VPL compilation path ──────────────

    def _provider_supports_vpl(self, tts) -> bool:
        """We only VPL-compile for providers whose compiler exists.
        Currently ElevenLabs.  Cartesia compiler is written but the
        provider integration lands in Sprint 10."""
        return getattr(tts, "name", "") == "elevenlabs"

    def _ensure_perf_planner(self):
        """Lazy singleton — one PerformancePlanner per session, wrapping
        a dedicated Groq 8B client.  Constructed on first use so tests
        can override _perf_planner directly before it's touched.

        Audit-3 fix (2026-08-04): the previous version temporarily
        mutated the global `settings.groq_model` to sneak a smaller
        model into GroqLLM.__init__ — that's a race under concurrent
        calls.  GroqLLM now accepts `model=` explicitly.

        We use GroqLLM directly (not the router) because the perf
        planner has a strict 200ms budget — router cool-down + fallback
        blows past that.  If Groq is down, the planner just fails and
        _vpl_synthesize uses default_delivery_for(speech_act)."""
        if self._perf_planner is not None:
            return self._perf_planner
        try:
            from app.providers.llm.groq_llm import GroqLLM
            llm = GroqLLM(
                raise_on_rate_limit=True,
                model=settings.performance_planner_model,
            )
        except Exception as e:
            log.warning("perf planner Groq build failed: %s", e)
            return None
        self._perf_planner = PerformancePlanner(
            llm=llm,
            timeout_ms=settings.performance_planner_timeout_ms,
            model=settings.performance_planner_model,
        )
        return self._perf_planner

    async def _vpl_synthesize(self, text: str, tts) -> tuple[bytes, str]:
        """Two-planner path: perf-plan Delivery, build VPL, compile,
        send to provider.  Returns (audio_bytes, mime).

        On any per-step failure we degrade toward the direct
        synthesize(text) path — the caller still hears a well-formed
        reply, just without the VPL delivery tuning."""
        speech_act_str = self._current_speech_act or "neutral"
        try:
            speech_act = SpeechAct(speech_act_str)
        except ValueError:
            speech_act = SpeechAct.NEUTRAL

        # 1. Performance planner — best-effort, always returns a Delivery
        planner = self._ensure_perf_planner()
        if planner is None:
            delivery = default_delivery_for(speech_act)
            hit, latency_ms = False, 0
        else:
            business_name = getattr(self, "business_name", "") or ""
            perf_plan = await planner.plan(text, speech_act, business_name)
            delivery = perf_plan.delivery
            hit = not perf_plan.used_fallback
            latency_ms = perf_plan.latency_ms
        _tel.record_two_planner_hit(
            tenant_id=self.tenant_id, hit=hit, latency_ms=latency_ms,
        )

        # 2. Build + validate the utterance
        try:
            utt = VPLUtterance(
                text=text, speech_act=speech_act, delivery=delivery,
            )
            utt, repairs = validate_vpl_and_repair(utt)
            if repairs:
                log.debug("VPL repaired for call=%s: %s", self.call_id, repairs)
        except Exception as e:
            log.warning("VPL construction failed, falling back to direct synth: %s", e)
            return await tts.synthesize(text)

        # 3. Compile to provider payload
        try:
            voice_id = getattr(tts, "default_voice", None) or ""
            plan = compile_elevenlabs(
                utt,
                voice_id=voice_id,
                model=getattr(tts, "model", "eleven_turbo_v2_5"),
                output_format=getattr(tts, "output_format", "ulaw_8000"),
            )
        except Exception as e:
            log.warning("VPL compile failed, falling back to direct synth: %s", e)
            return await tts.synthesize(text)

        # 4. Send.  If the provider doesn't implement synthesize_from_plan
        # (compat), degrade again.
        if not hasattr(tts, "synthesize_from_plan"):
            log.warning("provider missing synthesize_from_plan; direct synth")
            return await tts.synthesize(text)
        try:
            return await tts.synthesize_from_plan(plan)
        except Exception as e:
            log.warning("synthesize_from_plan failed, direct synth fallback: %s", e)
            return await tts.synthesize(text)

    # ── Sprint 10 STREAMING WIRING: STT + turn event handlers ───────

    async def _on_stt_partial(self, actor: CallActor, event: CallEvent) -> bool:
        """Streaming STT partial hypothesis.  Feeds turn manager +
        keeps rolling utterance text current."""
        text = event.payload.get("text", "")
        if text:
            self._streaming_utterance_text = text
        _tel.record_stream_event(self.tenant_id, kind="stt_partial")
        if self._turn_manager is not None:
            await self._turn_manager.on_stt_event("partial", text=text)
        return True

    async def _on_stt_final(self, actor: CallActor, event: CallEvent) -> bool:
        """Streaming STT final hypothesis.  Passes to turn manager
        which decides EAGER_END_OF_TURN vs INTERRUPTION vs redundant.

        `is_final=True` means "text won't be revised"; it can still be a
        mid-sentence endpoint.  `speech_final=True` means VAD confirmed
        the utterance is truly over — only then is END_OF_TURN safe."""
        text = event.payload.get("text", "")
        speech_final = event.payload.get("speech_final", False)
        if text:
            self._streaming_utterance_text = text
            # Caller spoke a real chunk — kill any pending idle prompt/hangup.
            # Cancel only on speech_final so echo/noise fragments don't
            # reset the idle timer between agent responses.
            if speech_final:
                self._cancel_idle_followup()
        _tel.record_stream_event(self.tenant_id, kind="stt_final")
        if self._turn_manager is not None:
            await self._turn_manager.on_stt_event(
                "final", text=text, is_final=True, speech_final=speech_final,
            )
        return True

    async def _on_stt_speech_signal(self, actor: CallActor, event: CallEvent) -> bool:
        """speech_start / speech_end from Deepgram VAD.  Forward to
        turn manager for false-interruption + endpoint detection."""
        kind = event.kind   # "speech_start" or "speech_end"
        _tel.record_stream_event(self.tenant_id, kind=kind)
        if self._turn_manager is not None:
            await self._turn_manager.on_stt_event(kind)
        return True

    async def _on_stt_stream_failed(self, actor: CallActor, event: CallEvent) -> bool:
        """Streaming STT gave up after N reconnects.  We fall back to
        the batch path on the next utterance (buffered VAD)."""
        log.warning(
            "stream failed on call=%s: %s — falling back to batch STT",
            self.call_id, event.payload,
        )
        _tel.record_stream_event(self.tenant_id, kind="stream_failed")
        # Drop the bridge so we don't keep reconnecting
        if self._stt_bridge is not None:
            await self._stt_bridge.stop()
            self._stt_bridge = None
        return True

    async def _on_turn_event(self, actor: CallActor, event: CallEvent) -> bool:
        """Generic no-op handler for turn events that are informational
        (EAGER_END_OF_TURN, TURN_RESUMED).  Metric bump + log; the
        actual action fires from END_OF_TURN / INTERRUPTION / etc."""
        _tel.record_turn_event(self.tenant_id, kind=event.kind)
        return True

    async def _on_turn_event_end(self, actor: CallActor, event: CallEvent) -> bool:
        """END_OF_TURN — caller committed their turn.

        Sprint 12 Track A: MUST return quickly.  Brain runs as a
        supervised job that emits control.brain_completed back to the
        actor when done.  A subsequent INTERRUPTION event won't queue
        behind a 2-second LLM call.

        Legacy inline behavior available under
        settings.actor_nonblocking_handlers=False for rollback."""
        _tel.record_turn_event(self.tenant_id, kind=event.kind)
        text = event.payload.get("text") or self._streaming_utterance_text
        if not text or not text.strip():
            return True

        await actor.bump_turn(reason="end-of-turn")
        self._open_turn_span(actor.turn_generation)
        if self._current_turn_span is not None:
            self._current_turn_span.mark("media_in")
            self._current_turn_span.mark("stt_final")

        turn_gen = actor.turn_generation
        # Reset the utterance buffer for next turn
        self._streaming_utterance_text = ""

        if settings.actor_nonblocking_handlers:
            # New path: spawn brain job, return immediately.  Job emits
            # control.brain_completed when done.
            actor.spawn_supervised(
                self._brain_job(text, turn_gen),
                generation=turn_gen,
                name=f"brain-{self.call_id}-{turn_gen}",
            )
            return True

        # Legacy path: inline await for rollback safety.
        brain_task = asyncio.create_task(
            self._run_brain_from_text(text, turn_gen),
            name=f"brain-{self.call_id}-{turn_gen}",
        )
        actor.register_turn_task(brain_task)
        try:
            await brain_task
        except asyncio.CancelledError:
            log.info("brain cancelled by newer turn call=%s gen=%d",
                     self.call_id, turn_gen)
        return True

    async def _brain_job(self, transcript: str, turn_gen: int) -> None:
        """Sprint 12 Track A: brain runs as a supervised job (off the
        mailbox).  On success, emits control.brain_completed with the
        reply.  On failure, emits control.brain_failed.  Handler
        _on_brain_completed then spawns _speech_job."""
        # Sprint 12 Track B addendum: filter echo before spending an
        # LLM turn on it.  If the transcript matches recent agent
        # utterances closely, it's the mic picking up our own speaker.
        if _looks_like_agent_echo(transcript, self._recent_agent_utterances):
            log.info("dropping echo turn=%d text=%r", turn_gen, transcript[:80])
            self._streaming_utterance_text = ""
            self._arm_idle_followup()
            return

        from packages.observability.call_event_log import (
            get_call_event_log, CallEvent as _CE, EventSourceKind as _SK,
        )
        try:
            _elog = get_call_event_log()
            _elog.write(_CE(
                call_id=self.session_id, tenant_id=self.tenant_id,
                source=_SK.STT, kind="final",
                payload={"text": transcript}, turn_generation=turn_gen,
            ))
        except Exception:
            _elog = None

        try:
            log.info("brain-job %s turn=%d heard: %s",
                     self.session_id, turn_gen, transcript)
            handle = session_manager.get_session(
                self.session_id, tenant_id=self.tenant_id,
            )
            if handle is None:
                state, brain = session_manager.start_session_with_id(
                    self.session_id, tenant_id=self.tenant_id,
                )
            else:
                state, brain = handle

            payload = await session_manager.run_user_turn(state, brain, transcript)
            reply = (payload.get("reply") or "").strip()
            escalated = bool(payload.get("escalated"))
            tool_results = payload.get("tool_results") or []
            speech_act = _infer_speech_act_from_payload(payload)

            if _elog is not None:
                try:
                    _elog.write(_CE(
                        call_id=self.session_id, tenant_id=self.tenant_id,
                        source=_SK.LLM, kind="reply",
                        payload={
                            "reply": reply,
                            "escalated": escalated,
                            "tool_results": tool_results,
                        },
                        turn_generation=turn_gen,
                    ))
                except Exception:
                    pass

            if self.actor is not None:
                self.actor.emit_local(CallEvent.new(
                    call_id=self.call_id, tenant_id=self.tenant_id,
                    source=EventSource.CONTROL,
                    turn_generation=turn_gen,
                    speech_generation=self.actor.speech_generation,
                    kind="brain_completed",
                    payload={
                        "reply": reply,
                        "escalated": escalated,
                        "tool_results": tool_results,
                        "speech_act": speech_act,
                        "turn_gen": turn_gen,
                    },
                    source_epoch=turn_gen,
                ))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("brain job failed: %s", e)
            if _elog is not None:
                try:
                    _elog.write_error(
                        call_id=self.session_id, tenant_id=self.tenant_id,
                        message=str(e), exc_type=type(e).__name__,
                        turn_generation=turn_gen,
                    )
                except Exception:
                    pass
            if self.actor is not None:
                self.actor.emit_local(CallEvent.new(
                    call_id=self.call_id, tenant_id=self.tenant_id,
                    source=EventSource.CONTROL,
                    turn_generation=turn_gen,
                    speech_generation=self.actor.speech_generation,
                    kind="brain_failed",
                    payload={
                        "error": str(e),
                        "exc_type": type(e).__name__,
                        "turn_gen": turn_gen,
                    },
                    source_epoch=turn_gen,
                ))

    async def _on_brain_completed(self, actor: CallActor, event: CallEvent) -> bool:
        """Brain job finished.  Save speech-act for VPL, spawn a
        supervised speech job for the reply text."""
        payload = event.payload or {}
        reply = (payload.get("reply") or "").strip()
        self._current_speech_act = payload.get("speech_act")
        if not reply:
            # No reply text — just arm idle followup so we don't hang.
            self._arm_idle_followup()
            return True
        turn_gen = payload.get("turn_gen", actor.turn_generation)
        actor.spawn_supervised(
            self._speech_job(reply, turn_gen),
            generation=turn_gen,
            name=f"speech-{self.call_id}-{turn_gen}",
        )
        return True

    async def _on_brain_failed(self, actor: CallActor, event: CallEvent) -> bool:
        """Brain job errored.  Log-only for now — caller can retry.
        Don't play a fallback string; silence is better than confusion
        for demo debugging."""
        payload = event.payload or {}
        log.warning("brain job failed turn=%s: %s (%s)",
                    payload.get("turn_gen"),
                    payload.get("error"), payload.get("exc_type"))
        self._arm_idle_followup()
        return True

    async def _speech_job(self, text: str, turn_gen: int) -> None:
        """Sprint 12 Track A: TTS+playback runs as a supervised job.
        On completion emits control.speech_completed."""
        try:
            await self._speak(text)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("speech job failed: %s", e)
        if self.actor is not None:
            self.actor.emit_local(CallEvent.new(
                call_id=self.call_id, tenant_id=self.tenant_id,
                source=EventSource.CONTROL,
                turn_generation=turn_gen,
                speech_generation=self.actor.speech_generation,
                kind="speech_completed",
                payload={"turn_gen": turn_gen},
                source_epoch=turn_gen,
            ))

    async def _on_speech_completed(self, actor: CallActor, event: CallEvent) -> bool:
        """Speech job finished — arm idle followup so we prompt if the
        caller stays silent."""
        self._arm_idle_followup()
        return True

    async def _run_brain_from_text(self, transcript: str, turn_gen: int) -> None:
        """Streaming-path brain execution.  Same shape as _run_brain
        but skips the WAV→STT step (we already have text)."""
        span = self._current_turn_span
        # Direct log-event calls so /debug/call/{id}/timeline reflects
        # streaming-path brain activity, not just kernel_wiring hooks.
        try:
            from packages.observability.call_event_log import (
                get_call_event_log, CallEvent as _CE, EventSourceKind as _SK,
            )
            _elog = get_call_event_log()
            _elog.write(_CE(
                call_id=self.session_id, tenant_id=self.tenant_id,
                source=_SK.STT, kind="final",
                payload={"text": transcript}, turn_generation=turn_gen,
            ))
        except Exception:
            _elog = None
        try:
            log.info("stream-brain %s turn=%d heard: %s",
                     self.session_id, turn_gen, transcript)
            handle = session_manager.get_session(
                self.session_id, tenant_id=self.tenant_id,
            )
            if handle is None:
                state, brain = session_manager.start_session_with_id(
                    self.session_id, tenant_id=self.tenant_id,
                )
            else:
                state, brain = handle

            payload = await session_manager.run_user_turn(state, brain, transcript)
            if span is not None:
                span.mark("llm_first_token")
            reply = (payload.get("reply") or "").strip()
            self._current_speech_act = _infer_speech_act_from_payload(payload)
            if _elog is not None:
                try:
                    _elog.write(_CE(
                        call_id=self.session_id, tenant_id=self.tenant_id,
                        source=_SK.LLM, kind="reply",
                        payload={
                            "reply": reply,
                            "escalated": bool(payload.get("escalated")),
                            "tool_results": payload.get("tool_results") or [],
                        },
                        turn_generation=turn_gen,
                    ))
                except Exception:
                    pass
            if reply:
                await self._speak(reply)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("stream-brain failed: %s", e)
            if _elog is not None:
                try:
                    _elog.write_error(
                        call_id=self.session_id, tenant_id=self.tenant_id,
                        message=str(e), exc_type=type(e).__name__,
                        turn_generation=turn_gen,
                    )
                except Exception:
                    pass

    async def _on_turn_event_backchannel(self, actor: CallActor, event: CallEvent) -> bool:
        """Caller said 'yeah'/'mm-hm' during agent speech — unduck
        (if ducked), don't stop the agent, don't fire brain."""
        _tel.record_turn_event(self.tenant_id, kind=event.kind)
        _tel.record_backchannel(self.tenant_id)
        if self._ducked:
            self._end_duck("backchannel_unduck")
        # Reset the streaming utterance buffer so this doesn't leak
        # into the next real turn
        self._streaming_utterance_text = ""
        return True

    async def _on_turn_event_interruption(self, actor: CallActor, event: CallEvent) -> bool:
        """Confirmed content-bearing interruption.  Send Twilio clear,
        reconcile transcript to heard-text, bump_turn, and run the
        brain with the interruption text as the next caller turn."""
        _tel.record_turn_event(self.tenant_id, kind=event.kind)
        text = event.payload.get("text") or self._streaming_utterance_text
        _tel.record_barge_in(self.tenant_id)

        # Ledger reconciliation BEFORE bump_turn (audit's moat)
        gen = actor.speech_generation
        heard = actor.ledger.heard_text_for(gen)
        generated = ""
        try:
            entry = actor.ledger._generations.get(gen)  # type: ignore[attr-defined]
            if entry is not None:
                generated = entry.full_text
        except Exception:
            pass
        _tel.record_heard_vs_generated(
            tenant_id=self.tenant_id,
            heard_chars=len(heard),
            generated_chars=len(generated),
        )
        try:
            from packages.runtime import reconcile_transcript_on_interrupt
            handle_for_state = session_manager.get_session(
                self.session_id, tenant_id=self.tenant_id,
            )
            if handle_for_state is not None:
                state_for_reconcile, _brain = handle_for_state
                reconcile_transcript_on_interrupt(
                    state_for_reconcile, actor.ledger, gen,
                )
        except Exception as e:
            log.warning("interrupt reconcile failed: %s", e)

        if self._ducked:
            self._end_duck("confirmed_interrupt")
        await self._send_twilio_clear()
        await actor.bump_turn(reason="turn-manager-interruption")
        self._close_turn_span()

        # Run the brain on the interruption text as the next real turn
        if text and text.strip():
            self._open_turn_span(actor.turn_generation)
            if self._current_turn_span is not None:
                self._current_turn_span.mark("media_in")
                self._current_turn_span.mark("stt_final")
            turn_gen = actor.turn_generation
            brain_task = asyncio.create_task(
                self._run_brain_from_text(text, turn_gen),
                name=f"brain-interrupt-{self.call_id}-{turn_gen}",
            )
            actor.register_turn_task(brain_task)
            try:
                await brain_task
            except asyncio.CancelledError:
                pass
        self._streaming_utterance_text = ""
        return True

    async def _on_turn_event_pause(self, actor: CallActor, event: CallEvent) -> bool:
        """Caller said 'hold on' / 'give me a sec'.  Stay silent —
        do NOT respond with 'sure!'.  If mid-speech, duck cleanly.
        Fires no brain call."""
        _tel.record_turn_event(self.tenant_id, kind=event.kind)
        # If agent is speaking, treat pause like a backchannel unduck
        # — caller wants us quiet, not interrupted.  If listening,
        # nothing to do (already silent).
        if self._ducked:
            self._end_duck("backchannel_unduck")
        # Reset the utterance buffer so 'hold on' doesn't become the
        # next brain input if turn manager later fires END_OF_TURN.
        self._streaming_utterance_text = ""
        return True

    async def _on_turn_event_false_int(self, actor: CallActor, event: CallEvent) -> bool:
        """VAD tripped but no content materialized.  Unduck if we
        ducked speculatively."""
        _tel.record_turn_event(self.tenant_id, kind=event.kind)
        if self._ducked:
            self._end_duck("false_trigger")
        return True

    async def _send_audio_frames(self, audio_bytes: bytes, mime: str) -> None:
        """Stream audio out to the transport.

        For Twilio-format calls (stream_sid starts with 'MZ' or the ws
        is a real Twilio Media Streams socket): downsample to µ-law 8kHz
        and send in 20ms frames.

        For browser-format calls (stream_sid starts with 'browser_'):
        send raw PCM base64 with a rate marker; widget plays at native
        rate.  Zero encoding loss.

        Sprint 9f duck logic + gain logic preserved for the Twilio path."""
        is_browser = self.stream_sid.startswith("browser_")

        if is_browser:
            await self._send_browser_pcm_frames(audio_bytes, mime)
            return

        # ----- Twilio path: encode PCM → µ-law 8kHz at the wire -----
        from app.routes.twilio import _tts_bytes_to_mulaw
        mulaw = _tts_bytes_to_mulaw(audio_bytes, mime)

        # Pre-apply gain to the whole buffer once (cheaper than per-frame).
        gain_db = settings.telephony_output_gain_db
        if abs(gain_db) > 0.01:
            mulaw = _apply_mulaw_gain(mulaw, gain_db)

        frame_bytes = int(TWILIO_SAMPLE_RATE * (TWILIO_FRAME_MS / 1000))
        for i in range(0, len(mulaw), frame_bytes):
            chunk = mulaw[i:i + frame_bytes]
            if len(chunk) < frame_bytes:
                chunk = chunk + b"\xff" * (frame_bytes - len(chunk))
            if not self._ducked:
                await self.ws.send_text(json.dumps({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(chunk).decode("ascii")},
                }))
            await asyncio.sleep(TWILIO_FRAME_MS / 1000)

    async def _send_browser_pcm_frames(self, audio_bytes: bytes, mime: str) -> None:
        """Browser transport: ship PCM s16le as-is, 40ms per frame,
        with an explicit `format` field so the widget knows how to
        play it."""
        # Extract rate from the MIME (e.g. "audio/pcm;rate=16000").
        sample_rate = 16000
        if "rate=" in (mime or ""):
            try:
                sample_rate = int(mime.split("rate=", 1)[1].split(";")[0].strip())
            except (ValueError, IndexError):
                pass
        # 40ms frames — bigger than Twilio's 20ms because network
        # overhead is the cost, not latency budget (browser is local).
        bytes_per_ms = sample_rate * 2 / 1000  # s16le = 2 bytes/sample
        frame_bytes = int(bytes_per_ms * 40)
        for i in range(0, len(audio_bytes), frame_bytes):
            chunk = audio_bytes[i:i + frame_bytes]
            if not self._ducked:
                await self.ws.send_text(json.dumps({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {
                        "format": f"pcm_s16le_{sample_rate}",
                        "payload": base64.b64encode(chunk).decode("ascii"),
                    },
                }))
            await asyncio.sleep(0.04)

    async def _send_twilio_mark(self, mark_id: str) -> None:
        """Ask Twilio to fire a mark event when this point in the stream
        has actually been played out.  Mark events come back on the
        `mark` message type and drive ledger.mark_ack()."""
        try:
            await self.ws.send_text(json.dumps({
                "event": "mark",
                "streamSid": self.stream_sid,
                "mark": {"name": mark_id},
            }))
        except Exception as e:
            log.debug("mark send failed: %s", e)

    async def _send_twilio_clear(self) -> None:
        """Flush Twilio's buffered audio for this stream.  Sent on
        confirmed barge-in so nothing more gets played."""
        try:
            await self.ws.send_text(json.dumps({
                "event": "clear",
                "streamSid": self.stream_sid,
            }))
        except Exception:
            pass

    # ── Sprint 9f: stage-1 ducking ──────────────────────────────────

    def _begin_duck(self) -> None:
        """Fire the stage-1 duck: stop new outbound frames, schedule the
        stage-2 deadline that will auto-unduck on false trigger.

        Sync method so the VAD frame handler pays zero await cost — we
        just flip the flag and schedule.  The classifier task in
        _classify_barge is already running off-actor and will emit the
        stage-2 outcome as a BARGE_CANDIDATE event when it completes."""
        if self._ducked:
            return
        self._ducked = True
        actor = self.actor
        if actor is not None:
            actor.transition(CallState.YIELDING)
        _tel.record_stage1_duck(self.tenant_id, "pending")
        log.debug("stage-1 duck engaged call=%s", self.call_id)

        # Schedule the deadline unducker.  If the classifier fires
        # first (BARGE_CANDIDATE → _on_barge_candidate) it cancels this
        # task before it runs.
        deadline_ms = settings.barge_stage2_deadline_ms
        self._stage2_deadline_task = asyncio.create_task(
            self._stage2_deadline(deadline_ms),
            name=f"stage2-deadline-{self.call_id}",
        )

    async def _stage2_deadline(self, deadline_ms: int) -> None:
        """Sleep deadline_ms; if we're still ducked without a
        classifier resolution, treat it as a false trigger (noise, TV,
        cough) and unduck."""
        try:
            await asyncio.sleep(deadline_ms / 1000.0)
            if self._ducked:
                log.info("stage-2 deadline hit → false trigger, unducking call=%s",
                         self.call_id)
                self._end_duck("false_trigger")
        except asyncio.CancelledError:
            # Classifier resolved before deadline — normal path
            pass

    def _end_duck(self, outcome: str) -> None:
        """Release the duck and record the outcome.

        Called from three paths:
          * classifier CONTINUE → outcome=backchannel_unduck
          * classifier INTERRUPT → outcome=confirmed_interrupt
          * deadline reached → outcome=false_trigger
        """
        if not self._ducked:
            return
        self._ducked = False
        # Cancel the deadline task if it's still pending (INTERRUPT and
        # backchannel paths both hit this).
        if self._stage2_deadline_task and not self._stage2_deadline_task.done():
            self._stage2_deadline_task.cancel()
        self._stage2_deadline_task = None
        _tel.record_stage1_duck(self.tenant_id, outcome)

        actor = self.actor
        if actor is None:
            return
        # Backchannel + false-trigger → back to SPEAKING; confirmed
        # interrupt path handles its own state transition after
        # bump_turn (LISTENING → THINKING).
        if outcome in ("backchannel_unduck", "false_trigger") and \
           actor.state == CallState.YIELDING:
            actor.transition(CallState.SPEAKING)


# ── websocket entrypoint (called by twilio.py when flag is on) ──────

async def handle_twilio_stream_via_actor(
    ws: WebSocket,
    *,
    tenant_id: str = "default",
) -> None:
    """Drop-in replacement for the legacy `twilio_stream` loop when
    `settings.twilio_use_actor` is true.  Same wire protocol, same
    events; internally routes through the CallActor kernel."""
    from starlette.websockets import WebSocketDisconnect

    session: Optional[TwilioActorSession] = None
    try:
        while True:
            raw = await ws.receive_text()
            event = json.loads(raw)
            kind = event.get("event")

            if kind == "connected":
                log.info("actor twilio connected: %s", event.get("protocol"))
                continue

            if kind == "start":
                stream_sid = event["start"]["streamSid"]
                call_sid = (event["start"].get("callSid")
                            or f"call_{uuid.uuid4().hex[:8]}")
                session = TwilioActorSession(
                    ws=ws,
                    stream_sid=stream_sid,
                    call_id=call_sid,
                    tenant_id=tenant_id,
                )
                log.info("actor twilio start: %s (%s)", call_sid, stream_sid)
                await session.start()
                continue

            if kind == "media" and session is not None:
                mulaw = base64.b64decode(event["media"]["payload"])
                await session.on_media(mulaw)
                continue

            if kind == "mark" and session is not None:
                mark_name = event.get("mark", {}).get("name")
                if mark_name:
                    await session.on_mark_ack(mark_name)
                continue

            if kind == "stop" and session is not None:
                log.info("actor twilio stop: %s", session.session_id)
                await session.stop("stop-event")
                break

    except WebSocketDisconnect:
        if session:
            await session.stop("ws-disconnect")
        log.info("actor twilio ws disconnected")
    except Exception as e:
        log.exception("actor twilio_stream error: %s", e)
        if session:
            await session.stop("error")
