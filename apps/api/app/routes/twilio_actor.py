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
from packages.core_agent.streaming import SentenceBuffer


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


def _text_matches_for_speculative(speculative: str, confirmed: str) -> bool:
    """2026-08-10 (task #284): return True if the confirmed END_OF_TURN
    text is a safe match for a speculative EAGER_END_OF_TURN we already
    fired.  A fragment-merge or trailing punctuation is fine.  Adding
    a whole new clause is not.

    Rule: the confirmed text must contain the speculative text as a
    prefix, AND any extra content is short (<= 3 words).  Deepgram
    typically appends 1-2 word fragments during the confirm window
    (\"and one more thing\", trailing punctuation).  If more arrives
    we treat as a real change and cancel the speculative reply."""
    import re as _re
    def _norm(s: str) -> str:
        return _re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()
    a = _norm(speculative)
    b = _norm(confirmed)
    if not a or not b:
        return False
    if a == b:
        return True
    if b.startswith(a):
        extra = b[len(a):].strip().split()
        return len(extra) <= 3
    if a.startswith(b):  # confirmed is shorter — Deepgram revised down
        return True
    return False


def _looks_like_agent_echo(transcript: str, recent_agent: list[str]) -> bool:
    """Return True only if the transcript is a near-exact CONTIGUOUS
    subsequence of a recent agent utterance — i.e. Deepgram literally
    caught the speaker feed.

    2026-08-09 FIX: previous version used set-overlap ≥ 60%.  That
    killed real callers who mirror greeting words ("Hello. Is this
    Smile Dental?" vs the agent's "Hello! You've reached Smile
    Dental...").  A caller's opener SHARES words with the greeting
    by design — bag-of-words is the wrong signal.

    Real speaker-echo has: (a) high contiguous word-run match AND
    (b) not much extra content the agent didn't say.  A caller adding
    a question of their own breaks the contiguous run OR adds too
    many novel words."""
    import re as _re
    words = _re.findall(r"[a-z']+", transcript.lower())
    if len(words) < 3:
        return False
    for agent_utt in recent_agent:
        agent_words = _re.findall(r"[a-z']+", agent_utt.lower())
        if not agent_words:
            continue
        # Longest contiguous word-run of transcript found inside agent utterance
        best_run = 0
        for i in range(len(words)):
            for j in range(len(agent_words)):
                k = 0
                while (i + k < len(words) and j + k < len(agent_words)
                       and words[i + k] == agent_words[j + k]):
                    k += 1
                if k > best_run:
                    best_run = k
        # Echo if the run covers ≥ 80% of the transcript AND transcript
        # has almost no novel words vs the agent utterance.
        novel = sum(1 for w in words if w not in set(agent_words))
        if best_run / len(words) >= 0.8 and novel <= 1:
            return True
    return False


def _strip_agent_echo_prefix(transcript: str, recent_agent: list[str]) -> str:
    """S13-B extension: when the mic captured the tail of the agent's
    speaker output AND the caller then spoke, Deepgram delivers a
    concatenated transcript like "hear you just fine. Can you hear me
    okay? Yeah. I can hear you too. Am I talking to Smile?"

    Full-drop is wrong (caller's real content lives in the tail).
    Instead, find the LONGEST agent-utterance-word-run at the start
    of the transcript and slice it off.  Return the tail; if no
    significant prefix match, return the original transcript.

    Rule of thumb: require ≥4 consecutive matching words at the start
    to declare a prefix echo — protects short valid caller openers
    that happen to share a word with the agent."""
    import re as _re
    if not recent_agent:
        return transcript
    trans_words = _re.findall(r"\S+", transcript)
    if len(trans_words) < 6:
        return transcript

    def _norm(w: str) -> str:
        return _re.sub(r"[^a-z']", "", w.lower())

    trans_norm = [_norm(w) for w in trans_words]
    best_prefix_len = 0
    for agent_utt in recent_agent:
        agent_norm = [_norm(w) for w in _re.findall(r"\S+", agent_utt) if _norm(w)]
        if len(agent_norm) < 4:
            continue
        # Try to find any run of agent words that appears as a prefix
        # of the transcript (possibly starting mid-agent-utterance,
        # because the mic caught the tail of what the agent was saying).
        for start in range(len(agent_norm)):
            i = 0
            while (
                start + i < len(agent_norm)
                and i < len(trans_norm)
                and agent_norm[start + i] == trans_norm[i]
            ):
                i += 1
            if i >= 4 and i > best_prefix_len:
                best_prefix_len = i

    if best_prefix_len >= 4:
        # Slice off the prefix, strip leading punctuation.
        tail = " ".join(trans_words[best_prefix_len:]).lstrip(" .,!?;:")
        return tail
    return transcript


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
        # 2026-08-08: Deepgram VAD warmer.  While the agent is speaking
        # (greeting, replies) the caller side of Twilio Media Streams
        # goes silent — no inbound frames arrive.  Deepgram's WS then
        # sits idle; when the first real speech frame lands, Deepgram's
        # server-side VAD hasn't been seeded and SpeechStarted often
        # doesn't fire, which means utterance_end_ms can't trigger.
        # Result: first-turn STT hangs 40s until the WS idle-timeout.
        # Fix: pump µ-law silence (0xFF bytes) at 20ms cadence during
        # SPEAKING/GREETING so Deepgram sees a continuous audio stream
        # and its VAD stays warm.  See docs/rnd-2026-08/53-fast-stt-
        # alternatives.md § "Cold-start hang" for the analysis.
        self._silence_pump_task: Optional[asyncio.Task] = None
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

        # 2026-08-10 (task #284): speculative dispatch state.  Set when
        # EAGER_END_OF_TURN fires; cleared on TURN_RESUMED (cancel) or
        # END_OF_TURN (short-circuit, task keeps running).
        self._speculative_task: Optional[asyncio.Task] = None
        self._speculative_text: Optional[str] = None

        # Fragment-merge window: Deepgram sometimes emits two speech_final
        # events within ~500ms when the caller pauses mid-sentence.
        # Instead of spawning two parallel brain jobs (which race and
        # produce the "skipped audio" symptom), we hold the first
        # end-of-turn for FRAGMENT_MERGE_WINDOW_MS and merge any
        # follow-on final into it.
        self._pending_turn_text: str = ""
        self._pending_turn_task: Optional[asyncio.Task] = None
        # Continuation-merge state: remember the last transcript we
        # actually committed to a brain call + when its final arrived,
        # so a follow-on fragment (arriving after the merge window
        # expired but before the previous reply finished) can be
        # merged into a re-planned turn instead of racing.
        self._last_committed_transcript: str = ""
        self._last_final_monotonic: float = 0.0
        # K1: hint for the brain about an incomplete-looking turn.
        # Populated by _flush_pending_turn_after_window, consumed by
        # _brain_job.  NEVER goes into the transcript — that broke
        # continuation-merge earlier.
        self._pending_k1_hint: str = ""
        # K1 (2026-08-06): tracks when we started holding an incomplete
        # turn.  Bounded so we don't hold forever.
        self._incomplete_hold_started_at: Optional[float] = None

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
                # S13-A: install prosodic EOT probability provider.  On
                # every final, TurnManager will call this to consult
                # smart-turn-v3 with the last ~4 sec of caller PCM.  We
                # feed 4 sec (not 8) because the classifier's signal is
                # dominated by the trailing 1-3 sec of prosody — shorter
                # window is cheaper and just as accurate for phone audio.
                if settings.smart_turn_enabled and self._stt_bridge is not None:
                    try:
                        from packages.runtime.smart_turn import SmartTurnDetector
                        det = SmartTurnDetector.get()
                        bridge = self._stt_bridge

                        # 2026-08-07: smart-turn inference is synchronous
                        # ONNX (~17ms warm, ~450ms cold on first call).
                        # Calling it from the async event loop as a plain
                        # function BLOCKED the loop long enough for the
                        # Deepgram audio consumer to starve → 1000+ frames
                        # dropped, greeting delayed 31s, Deepgram closed
                        # its WS with "no audio received."
                        # Wrapping in a lightweight in-process cache with
                        # a 200ms TTL so back-to-back calls (turn manager
                        # can hit us on every partial) don't re-infer.
                        _cache: dict = {"ts": 0.0, "val": 0.5, "failures": 0}

                        def _predict_eot() -> float:
                            # 2026-08-07: hard 25ms budget.  If smart-turn
                            # ever runs slow (ONNX warmup, GC pause, etc)
                            # we return neutral 0.5 rather than block the
                            # event loop and starve Deepgram's audio consumer.
                            # After 3 consecutive failures we disable for
                            # this call entirely.
                            import time as _t
                            if _cache["failures"] >= 3:
                                return 0.5
                            now = _t.monotonic()
                            if now - _cache["ts"] < 0.25:
                                return _cache["val"]
                            pcm = bridge.get_recent_pcm16k(seconds=4.0)
                            if len(pcm) < 16000:
                                return 0.5
                            try:
                                t0 = _t.perf_counter()
                                v = det.predict(pcm)
                                dur_ms = (_t.perf_counter() - t0) * 1000
                                if dur_ms > 60:
                                    _cache["failures"] += 1
                                    log.warning(
                                        "smart-turn slow: %.0fms (failure %d/3)",
                                        dur_ms, _cache["failures"],
                                    )
                                else:
                                    _cache["failures"] = 0
                            except Exception as _e:
                                _cache["failures"] += 1
                                log.debug("smart-turn predict failed: %s", _e)
                                return 0.5
                            _cache["ts"] = now
                            _cache["val"] = v
                            return v
                        self._turn_manager._eot_probability_provider = _predict_eot
                        log.info("smart-turn-v3 EOT provider installed call=%s", self.call_id)
                    except Exception as e:
                        log.warning("smart-turn init failed (falling back to text-only EOT): %s", e)
                log.info("turn manager attached for call=%s", self.call_id)
            except Exception as e:
                log.warning("turn manager disabled: %s", e)
                self._turn_manager = None

        # 2026-08-08: kick off the silence pump so Deepgram sees a
        # continuous audio stream from t=0.  Cancelled when the real
        # caller frames start flowing (Twilio media event) or on stop().
        # See _silence_pump docstring for the root cause.
        if self._stt_bridge is not None:
            self._silence_pump_task = asyncio.create_task(
                self._silence_pump(),
                name=f"silence-pump-{self.call_id}",
            )

        # 2026-08-12 (task #323): fire-and-forget ElevenLabs TLS prewarm.
        # From PK the first TTS request pays ~500ms of TCP+TLS handshake
        # cost.  Kicking a dummy GET while greeting is being prepped
        # means the HTTP/2 client is already hot by the time real audio
        # requests fly.  Uses the persistent shared client that
        # elevenlabs_tts.py maintains — one warmed socket, reused.
        async def _prewarm_elevenlabs():
            try:
                from app.routes.twilio import _get_telephony_tts
                tts = _get_telephony_tts()
                # Reach the real ElevenLabs adapter through cache wrapper
                inner = getattr(tts, "_inner", tts)
                if hasattr(inner, "_get_client"):
                    client = inner._get_client()
                    # Cheap HEAD-style GET to warm TLS + connection pool.
                    api_key = getattr(inner, "api_key", None)
                    if api_key:
                        await client.get(
                            "https://api.elevenlabs.io/v1/models",
                            headers={"xi-api-key": api_key},
                            timeout=3.0,
                        )
                        log.debug("elevenlabs TLS prewarmed call=%s", self.call_id)
            except Exception as _e:
                log.debug("elevenlabs prewarm skipped: %s", _e)
        asyncio.create_task(
            _prewarm_elevenlabs(),
            name=f"11labs-prewarm-{self.call_id}",
        )

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
        # Kill any pending fragment-merge window
        if self._pending_turn_task and not self._pending_turn_task.done():
            self._pending_turn_task.cancel()
        self._pending_turn_task = None
        self._pending_turn_text = ""
        # Sprint 9f: don't leak the stage-2 deadline task on hangup
        if self._stage2_deadline_task and not self._stage2_deadline_task.done():
            self._stage2_deadline_task.cancel()
            self._stage2_deadline_task = None
        # 2026-08-08: kill silence pump if still running
        if self._silence_pump_task is not None and not self._silence_pump_task.done():
            self._silence_pump_task.cancel()
            self._silence_pump_task = None
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
            # 2026-08-11 (task #316): Deepgram Flux native turn events.
            # Nova-3 never emits these kinds; Flux does.  Same handler
            # since turn manager fully absorbs each kind and re-emits
            # the appropriate CONTROL event.
            actor.handlers[(EventSource.STT, "eager_end_of_turn")] = self._on_stt_native_turn
            actor.handlers[(EventSource.STT, "end_of_turn")] = self._on_stt_native_turn
            actor.handlers[(EventSource.STT, "turn_resumed")] = self._on_stt_native_turn
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

    async def _silence_pump(self) -> None:
        """2026-08-08: feed µ-law silence (0xFF) into the STT bridge at
        Twilio's 20ms cadence until real caller frames arrive.

        Root cause this fixes: Deepgram Nova-3's server-side VAD needs
        a continuous audio stream to seed its endpointer.  When the WS
        opens but no bytes flow for 5-10 sec (we're playing a greeting,
        caller is silent), the first real speech bytes are missed —
        SpeechStarted never fires, so utterance_end_ms can't trigger,
        and we hit the ~40s WS idle-timeout.

        0xFF is the µ-law encoding of near-silence (technically the
        smallest-magnitude positive value).  Continuous silence keeps
        the VAD alive without producing spurious transcripts.

        Cancelled by on_media() on the first real frame, or stop()."""
        # 2026-08-08 v3: back to 0xFF (µ-law digital silence).
        # v2 used 0x7F "comfort noise" but Deepgram interpreted that as
        # low-level speech and fired multiple false SpeechStarted events
        # during the greeting, wasting VAD cycles.  Real-call data
        # (CA58790517) showed 8.5 sec gap between real speech starting
        # and DG's first transcript because VAD was confused by our own
        # comfort noise.  0xFF = the reference silence value in µ-law
        # (biased zero).  DG's KeepAlive JSON keeps the WS alive; we
        # only need the audio stream to be non-empty, not "speech-like".
        pattern = bytes([0xFF]) * 160  # 160 bytes = 20ms @ 8kHz mulaw silence
        log.info(
            "silence-pump started call=%s (comfort-noise µ-law, 20ms cadence)",
            self.call_id,
        )
        frames_sent = 0
        try:
            while True:
                if self._stt_bridge is not None:
                    self._stt_bridge.feed(pattern)
                    frames_sent += 1
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            log.info("silence-pump stopped call=%s frames_sent=%d (real audio arrived)",
                     self.call_id, frames_sent)
            return

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

        # 2026-08-08: first real caller frame arrived — cancel the
        # silence pump.  Real audio now flows into the bridge.
        if self._silence_pump_task is not None and not self._silence_pump_task.done():
            self._silence_pump_task.cancel()
            self._silence_pump_task = None

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

    def _streaming_llm_eligible(self, brain) -> bool:
        """Task #283: gate the streaming LLM→TTS path.

        Off unless the flag is on AND the resolved provider has
        stream_complete AND we're on the phone leg AND VPL is off.
        Tool-call turns are auto-fallen-through inside brain.handle_user_turn
        (streaming only fires on the terminal no-tools branch)."""
        if not settings.streaming_llm_to_tts:
            return False
        if settings.two_planner_enabled:
            return False
        if self.stream_sid.startswith("browser_"):
            return False
        if not hasattr(brain.llm, "stream_complete"):
            return False
        return True

    async def _pump_sentence_queue(
        self, queue: "asyncio.Queue", gen: int,
    ) -> None:
        """Consumer: takes sentences off the queue and pipes each into
        _stream_tts_incremental sequentially. Stops when it sees None.
        Runs as a background task spawned from _run_brain_streaming."""
        from app.routes.twilio import _get_telephony_tts
        from packages.voice.speech_sanitizer import sanitize_for_speech
        tts = _get_telephony_tts()
        span = self._current_turn_span
        first = True
        while True:
            sentence = await queue.get()
            if sentence is None:
                break
            if gen != self.speech_generation:
                log.info(
                    "TTS_SENTENCE_DROPPED_STALE call=%s stale_gen=%d cur_gen=%d",
                    self.call_id, gen, self.speech_generation,
                )
                continue
            try:
                clean = sanitize_for_speech(sentence)
                if not clean.strip():
                    continue
                log.info(
                    "TTS_SENTENCE_QUEUED call=%s gen=%d first=%s text=%r",
                    self.call_id, gen, first, clean[:80],
                )
                await self._stream_tts_incremental(tts, clean, gen, span if first else None)
                first = False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("TTS_SENTENCE_FAILED: %s", e)

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

            # Task #283: streaming LLM→TTS branch when eligible.
            if self._streaming_llm_eligible(brain):
                await self._run_brain_streaming(state, brain, transcript, turn_gen, span)
                return

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

    async def _run_brain_streaming(
        self, state, brain, transcript: str, turn_gen: int, span,
    ) -> None:
        """Task #283: streaming LLM→TTS path.

        Callback pushes tokens into a SentenceBuffer. Each complete
        sentence goes onto a queue that a background pumper feeds into
        _stream_tts_incremental in order. When brain finishes:
          - If the returned reply diverges from what we streamed (fake-
            booking guard rewrote it), interrupt in-flight audio and
            speak the safe replacement.
          - Otherwise flush any residual tokens as a final sentence.
        """
        buf = SentenceBuffer(min_first_chars=20)
        queue: asyncio.Queue = asyncio.Queue()
        pumper_task = asyncio.create_task(
            self._pump_sentence_queue(queue, turn_gen),
            name=f"tts-pump-{self.call_id}-g{turn_gen}",
        )
        first_delta = True

        async def on_delta(delta: str):
            nonlocal first_delta
            if first_delta and span is not None:
                span.mark("llm_first_token")
                first_delta = False
            for sentence in buf.push(delta):
                await queue.put(sentence)

        try:
            payload = await session_manager.run_user_turn(
                state, brain, transcript, on_delta=on_delta,
            )
            self._current_speech_act = _infer_speech_act_from_payload(payload)

            # Flush residual (text after the last sentence-ender)
            residual = buf.flush()
            if residual:
                await queue.put(residual)

            # Signal end-of-stream to the pumper
            await queue.put(None)
            await pumper_task

            # If the brain replaced the reply (fake-booking guard),
            # payload["reply"] won't match buf.full_text. Interrupt
            # what we sent + speak the replacement. NOTE: if buf.full_text
            # is empty (streaming path fell through to batch inside brain),
            # payload["reply"] holds the real reply and we speak it now.
            planned = (payload.get("reply") or "").strip()
            streamed = buf.full_text.strip()
            if planned and planned != streamed:
                if not streamed:
                    # Streaming never happened (batch fallback inside brain).
                    log.info(
                        "STREAM_BATCH_FALLBACK call=%s gen=%d — speaking batch reply",
                        self.call_id, turn_gen,
                    )
                    await self._speak(planned)
                else:
                    log.warning(
                        "STREAM_REPLY_REPLACED call=%s gen=%d spoken=%r planned=%r",
                        self.call_id, turn_gen,
                        streamed[:100], planned[:100],
                    )
                    await self._send_twilio_clear()
                    await self._speak(planned)
        except asyncio.CancelledError:
            pumper_task.cancel()
            raise
        except Exception as e:
            log.exception("_run_brain_streaming failed: %s", e)
            pumper_task.cancel()

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

            # 2026-08-12 (task #321): TTS cache-hit shortcut.  Before we
            # even hit the network, check if this exact text is already
            # cached on disk in the right format.  Greeting + fillers +
            # common replies live here.  Hit = ~2ms disk read, MISS =
            # falls through to network.  Fixes the 4.8s "greeting cache
            # bypass" bug (trace CAcd97dff9): cached bytes existed but
            # actor called stream_synthesize anyway.
            try:
                from packages.tts_cache.cache import get_shared_cache, _hash_key
                voice = getattr(tts, "default_voice", "default")
                fmt = getattr(tts, "output_format", "ulaw_8000")
                provider = getattr(tts, "name", "tts")
                # TTSCacheWrapper wraps the real provider; walk one level in
                if hasattr(tts, "_inner"):
                    voice = getattr(tts._inner, "default_voice", voice)
                    fmt = getattr(tts._inner, "output_format", fmt)
                    provider = getattr(tts._inner, "name", provider)
                key = _hash_key(voice, text, fmt, provider)
                hit = await get_shared_cache().get(key)
                if hit is not None:
                    audio_bytes, mime = hit
                    log.info("TTS cache-hit shortcut: %d bytes for %r",
                             len(audio_bytes), text[:60])
                    if span is not None:
                        span.mark("tts_first_byte")
                        self._close_turn_span()
                    await self._send_audio_frames(audio_bytes, mime)
                    return
            except Exception as _e:
                log.debug("TTS cache-hit shortcut skipped: %s", _e)

            # 2026-08-09 SPEED SPRINT (task #281): use streaming TTS when
            # the provider supports it AND we're on the Twilio path (µ-law
            # is chunk-safe).  First audio chunk arrives ~150-200ms after
            # request instead of ~1000-2000ms for the batch endpoint —
            # saves 800-1500ms end-to-end per turn.  Falls back to batch
            # synthesize() when: provider lacks stream_synthesize, we're
            # in VPL two-planner mode, or we're on the browser path.
            can_stream = (
                not settings.two_planner_enabled
                and not self.stream_sid.startswith("browser_")
                and hasattr(tts, "stream_synthesize")
            )
            if can_stream:
                await self._stream_tts_incremental(tts, text, gen, span)
                return

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

        # Ledger entry sized by the audio bytes going out.
        # 2026-08-07: duration math is format-dependent:
        #   µ-law 8kHz  = 8  bytes/ms (native Twilio wire format)
        #   PCM s16 16k = 32 bytes/ms (browser widget, high-quality path)
        # Getting this wrong makes the ledger think a 5-second µ-law
        # clip is only 1.25 seconds, which breaks barge-in reconciliation
        # + heard-vs-generated ratios.
        _mime_lower = (mime or "").lower()
        if "mulaw" in _mime_lower or "ulaw" in _mime_lower or "pcmu" in _mime_lower:
            bytes_per_ms = 8
        else:
            bytes_per_ms = 32
        self._mark_counter += 1
        mark_id = f"m{gen}-{self._mark_counter}"
        chunk = AudioChunk(
            generation_id=f"gen-{gen}",
            sequence=0,
            audio_bytes=len(audio_bytes),
            duration_ms=int(len(audio_bytes) / bytes_per_ms),
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

    async def _stream_tts_incremental(self, tts, text: str, gen: int, span) -> None:
        """2026-08-09: stream TTS chunks from ElevenLabs directly to Twilio.

        Each chunk that arrives from ElevenLabs is immediately dispatched
        to the µ-law outbound path — no waiting for the full utterance.
        Caller hears the first ~150-200ms of audio while the rest is
        still being synthesized upstream.

        2026-08-12 (task #322): if ELEVENLABS_USE_WS is on AND the inner
        provider exposes ws_stream_synthesize, use the bidirectional
        WebSocket. Cuts first-byte ~400ms on high-RTT clients because we
        skip the HTTP /stream request/response setup.

        Trade-off: no full audio_bytes buffer → no ledger sizing at the
        top (ledger entry is written when the stream completes with the
        cumulative byte count).  Cancellation on bump_speech still works
        because we're inside a Task registered with the actor."""
        import time as _t
        first_chunk = True
        cumulative_bytes = 0
        chunk_count = 0
        mime = getattr(tts, "mime", "audio/x-mulaw;rate=8000")
        t_request = _t.perf_counter()

        # Pick WS vs HTTP stream. WS lives on the inner provider (cache
        # wrapper doesn't expose it).
        inner = getattr(tts, "_inner", tts)
        use_ws = (
            settings.elevenlabs_use_ws
            and hasattr(inner, "ws_stream_synthesize")
            and getattr(inner, "name", "") == "elevenlabs"
        )
        stream_source = inner if use_ws else tts
        stream_method = (
            inner.ws_stream_synthesize(text) if use_ws
            else tts.stream_synthesize(text)
        )
        transport = "ws" if use_ws else "http"
        log.info(
            "TTS_STREAM_START call=%s gen=%d transport=%s text=%r",
            self.call_id, gen, transport, text[:60],
        )

        try:
            async for chunk, chunk_mime in stream_method:
                if not chunk:
                    continue
                if first_chunk:
                    first_chunk = False
                    first_byte_ms = (_t.perf_counter() - t_request) * 1000
                    if span is not None:
                        span.mark("tts_first_byte")
                        self._close_turn_span()
                    mime = chunk_mime
                    log.info(
                        "TTS_FIRST_BYTE call=%s gen=%d transport=%s "
                        "first_byte_ms=%.0f mime=%s",
                        self.call_id, gen, transport, first_byte_ms, mime,
                    )
                chunk_count += 1
                cumulative_bytes += len(chunk)
                await self._send_audio_frames(chunk, mime)
        except asyncio.CancelledError:
            log.info(
                "TTS_STREAM_CANCELLED call=%s gen=%d transport=%s "
                "chunks=%d bytes=%d",
                self.call_id, gen, transport, chunk_count, cumulative_bytes,
            )
            raise
        except Exception as e:
            log.exception(
                "TTS_STREAM_FAILED call=%s gen=%d transport=%s err=%s",
                self.call_id, gen, transport, e,
            )
            # If WS failed before the first byte, fall back to HTTP so we
            # don't leave the caller in silence. After first_byte we've
            # already committed audio to the wire — safer to bail.
            if use_ws and first_chunk:
                log.warning(
                    "TTS_STREAM_FALLBACK call=%s gen=%d ws→http",
                    self.call_id, gen,
                )
                try:
                    async for chunk, chunk_mime in tts.stream_synthesize(text):
                        if not chunk:
                            continue
                        if first_chunk:
                            first_chunk = False
                            first_byte_ms = (_t.perf_counter() - t_request) * 1000
                            if span is not None:
                                span.mark("tts_first_byte")
                                self._close_turn_span()
                            mime = chunk_mime
                            log.info(
                                "TTS_FIRST_BYTE call=%s gen=%d transport=http-fallback "
                                "first_byte_ms=%.0f mime=%s",
                                self.call_id, gen, first_byte_ms, mime,
                            )
                        chunk_count += 1
                        cumulative_bytes += len(chunk)
                        await self._send_audio_frames(chunk, mime)
                except Exception as e2:
                    log.exception("TTS_STREAM_FALLBACK_FAILED: %s", e2)
                    return
            else:
                return

        total_ms = (_t.perf_counter() - t_request) * 1000
        log.info(
            "TTS_STREAM_DONE call=%s gen=%d transport=%s chunks=%d "
            "bytes=%d total_ms=%.0f",
            self.call_id, gen, transport, chunk_count, cumulative_bytes,
            total_ms,
        )

        # Ledger entry (approximate — we know the cumulative bytes now).
        _mime_lower = (mime or "").lower()
        bytes_per_ms = 8 if ("mulaw" in _mime_lower or "ulaw" in _mime_lower
                             or "pcmu" in _mime_lower) else 32
        self._mark_counter += 1
        mark_id = f"m{gen}-{self._mark_counter}"
        chunk_ledger = AudioChunk(
            generation_id=f"gen-{gen}", sequence=0,
            audio_bytes=cumulative_bytes,
            duration_ms=int(cumulative_bytes / bytes_per_ms),
            text=text, text_start=0, text_end=len(text),
            mark_id=mark_id, is_final=True,
        )
        if self.actor is not None:
            self.actor.ledger.queue_chunk(gen, chunk_ledger)
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
        turn manager for false-interruption + endpoint detection.

        Cancel the idle-followup the INSTANT the caller opens their
        mouth.  Otherwise the 15s timer armed after the greeting fires
        "Anything else?" the moment their first speech_final lands,
        stepping on the real reply that's still spinning up (observed
        16:08:32 in the debug feed)."""
        kind = event.kind   # "speech_start" or "speech_end"
        _tel.record_stream_event(self.tenant_id, kind=kind)
        if kind == "speech_start":
            self._cancel_idle_followup()
        if self._turn_manager is not None:
            await self._turn_manager.on_stt_event(kind)
        return True

    async def _on_stt_native_turn(self, actor: CallActor, event: CallEvent) -> bool:
        """2026-08-11 (task #316): Deepgram Flux emits native
        eager_end_of_turn / end_of_turn / turn_resumed events.  Forward
        to turn manager which trusts them directly (bypasses our 400ms
        confirm window → saves ~400ms per turn).  Nova-3 never fires
        these kinds so this handler is Flux-only in practice."""
        kind = event.kind
        text = event.payload.get("text", "")
        _tel.record_stream_event(self.tenant_id, kind=kind)
        if self._turn_manager is not None:
            await self._turn_manager.on_stt_event(kind, text=text)
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
        """Handler for EAGER_END_OF_TURN, TURN_RESUMED, and FALSE_INTERRUPTION.

        2026-08-10 (task #284): speculative dispatch on EAGER_END_OF_TURN.
        Fire the brain the moment turn manager thinks the caller is done,
        WITHOUT waiting for the 400ms confirm.  If TURN_RESUMED fires
        (caller keeps talking), cancel the speculative task.  If
        END_OF_TURN confirms, we already have the reply in flight —
        _on_turn_event_end awaits it instead of firing a new one.

        Net saving: ~400ms of brain time on every real turn.  User
        specifically called out this moment: "it responded to me as
        i was talking that felt really damn good."
        """
        _tel.record_turn_event(self.tenant_id, kind=event.kind)

        if event.kind == TurnEventKind.EAGER_END_OF_TURN.value:
            text = (event.payload.get("text") or "").strip()
            if not text or len(text.split()) < 2:
                return True
            # Only speculate if we're not already speaking / thinking.
            if actor.state not in (CallState.LISTENING,):
                return True
            # Don't stack — a prior speculative task means we haven't
            # cleaned up yet; skip this one.
            if getattr(self, "_speculative_task", None) and \
                    not self._speculative_task.done():
                return True
            speculative_turn = actor.turn_generation
            log.info("speculative brain firing call=%s gen=%d text=%r",
                     self.call_id, speculative_turn, text[:80])
            self._speculative_text = text
            self._speculative_task = asyncio.create_task(
                self._run_brain_from_text(text, speculative_turn),
                name=f"speculative-{self.call_id}-{speculative_turn}",
            )
            return True

        if event.kind == TurnEventKind.TURN_RESUMED.value:
            # Caller kept talking — cancel any in-flight speculative
            # brain (the text we sped up on is stale).
            spec = getattr(self, "_speculative_task", None)
            if spec is not None and not spec.done():
                log.info("speculative brain cancelled (TURN_RESUMED) call=%s",
                         self.call_id)
                spec.cancel()
            self._speculative_task = None
            self._speculative_text = None
            return True

        return True

    # Fragment-merge tuning (2026-08-05):
    # Real callers pause 2-4 sec mid-thought.  Deepgram commits each
    # 1200ms-endpointed segment as its own speech_final.  We hold each
    # end-of-turn for this window and merge follow-on finals into one
    # brain call.
    # 2026-08-08: DROPPED 2500 → 400 ms.  With smart-turn-v3 as the
    # EOT authority + utterance_end_ms=1000 on Deepgram, fragments
    # nearly always land within 300 ms of the first speech_final.
    # 2500 was adding 2+ seconds of dead air to EVERY turn "just in
    # case" a fragment arrived.  Real-call data (CAb4a31b) showed
    # brain firing 3 sec after Deepgram's first speech_final — 100%
    # of that was this window.  400 ms still catches genuine
    # fragment splits without the wait.
    _FRAGMENT_MERGE_WINDOW_MS: int = 400
    # Continuation-merge window: if a new final arrives while the agent
    # is STILL speaking or thinking on the previous turn AND less than
    # this many seconds have elapsed since the last final, treat as a
    # continuation of the same thought — cancel the in-flight work,
    # merge, and re-plan.  Prevents "the agent cuts itself off" when
    # the caller adds "...oh and one more thing" mid-agent-reply.
    _CONTINUATION_MERGE_MAX_S: float = 6.0

    async def _on_turn_event_end(self, actor: CallActor, event: CallEvent) -> bool:
        """END_OF_TURN — caller committed their turn.

        Sprint 12 Track A: MUST return quickly.  Brain runs as a
        supervised job that emits control.brain_completed back to the
        actor when done.  A subsequent INTERRUPTION event won't queue
        behind a 2-second LLM call.

        Fragment-merge: if another END_OF_TURN arrives within
        _FRAGMENT_MERGE_WINDOW_MS we concat the text and re-arm the
        window instead of spawning a second brain job (which would
        race the first and produce the "skipped audio" symptom the
        user reported 2026-08-05).

        Continuation-merge: if the previous turn's brain/speech is
        still in flight AND less than _CONTINUATION_MERGE_MAX_S
        elapsed since the last stt.final, treat as a continuation:
        cancel in-flight work, merge with the previous transcript,
        re-plan as one turn.  This is the fix for "I said a whole
        sentence and it cut me up and said something new".

        Legacy inline behavior available under
        settings.actor_nonblocking_handlers=False for rollback."""
        _tel.record_turn_event(self.tenant_id, kind=event.kind)
        text = event.payload.get("text") or self._streaming_utterance_text
        if not text or not text.strip():
            return True

        # Reset the streaming buffer — text is now captured in
        # self._pending_turn_text below.
        self._streaming_utterance_text = ""
        addition = text.strip()

        # Case 1: merge-window still open → append and re-arm.
        if self._pending_turn_task and not self._pending_turn_task.done():
            existing = self._pending_turn_text.rstrip()
            merged = f"{existing} {addition}" if existing else addition
            self._pending_turn_text = merged
            log.info("merged pending fragment call=%s: %r + %r -> %r",
                     self.call_id, existing, addition, merged)
            self._pending_turn_task.cancel()
            self._pending_turn_task = asyncio.create_task(
                self._flush_pending_turn_after_window(),
                name=f"merge-window-{self.call_id}",
            )
            return True

        # Case 2: previous turn already committed but the caller kept
        # talking — treat as continuation.  We used to require actor
        # state SPEAKING/PROCESSING but that's too narrow: the reply
        # can finish before the caller resumes, and the caller can
        # still be continuing the same thought.  Gap alone is the
        # right signal.
        now = time.monotonic()
        gap = now - self._last_final_monotonic if self._last_final_monotonic else 999.0
        prev_transcript = self._last_committed_transcript
        # Safety net: never merge a synthetic system-note into a
        # transcript (guards against any lingering pre-fix corruption).
        if prev_transcript.startswith("[SYSTEM"):
            self._last_committed_transcript = ""
            prev_transcript = ""

        # Dedup: Deepgram sometimes redelivers old text prepended to
        # new text.  If the addition contains the previous transcript
        # or the previous transcript is a prefix of the addition,
        # strip the overlap.
        norm_prev = prev_transcript.strip().lower()
        norm_add = addition.lower()
        if norm_prev and norm_prev in norm_add:
            addition = addition[norm_add.index(norm_prev) + len(norm_prev):].lstrip(" ,.-")
            log.info("stripped duplicated prefix call=%s: keeping %r",
                     self.call_id, addition)
            if not addition:
                # Entire "new" final was just the old transcript replayed.
                return True

        if (
            prev_transcript
            and gap <= self._CONTINUATION_MERGE_MAX_S
        ):
            merged = f"{prev_transcript.rstrip()} {addition}"
            log.info(
                "continuation-merge call=%s gap=%.2fs state=%s: %r + %r -> %r",
                self.call_id, gap, actor.state, prev_transcript, addition, merged,
            )
            # bump_turn cancels in-flight brain + speech; then we
            # re-arm with the merged text through the normal flush path
            # (which also gates via the merge-window in case a third
            # fragment arrives).
            self._pending_turn_text = merged
            self._last_committed_transcript = ""
            self._pending_turn_task = asyncio.create_task(
                self._flush_pending_turn_after_window(),
                name=f"merge-window-{self.call_id}",
            )
            return True

        # Case 3: fresh turn.  Arm the merge window.
        self._pending_turn_text = addition
        self._pending_turn_task = asyncio.create_task(
            self._flush_pending_turn_after_window(),
            name=f"merge-window-{self.call_id}",
        )
        return True

    async def _flush_pending_turn_after_window(self) -> None:
        """Sleep FRAGMENT_MERGE_WINDOW_MS, then commit the pending turn
        to the brain.  Cancelled + re-armed each time a new END_OF_TURN
        arrives inside the window.

        2026-08-11: dynamic window.  When the LAST agent utterance asked
        the caller for STRUCTURED DATA (name, phone, number, address),
        callers naturally pause mid-answer to remember digits or spell
        their name.  400ms cuts them off; use 2000ms so we can splice
        "my name is Abbas" + "and my number is" + "0330-..." into one
        commit.  Observed on trace CA7eb96fd where the agent's phone
        request got 3 separate turn commits over 5 sec.
        """
        window_ms = self._FRAGMENT_MERGE_WINDOW_MS
        last_agent = (self._recent_agent_utterances[-1] if self._recent_agent_utterances else "").lower()
        if any(kw in last_agent for kw in (
            "phone number", "10-digit", "ten-digit", "ten digit",
            "your name", "full name", "your number", "callback",
            "spell", "address", "email",
        )):
            window_ms = 2000
            log.debug("fragment merge window widened to %dms (structured-data ask)", window_ms)
        try:
            await asyncio.sleep(window_ms / 1000.0)
        except asyncio.CancelledError:
            # Another fragment arrived — the new task will handle it.
            return

        text = self._pending_turn_text
        self._pending_turn_text = ""
        self._pending_turn_task = None

        actor = self.actor
        if actor is None or not text.strip():
            return

        # S13-B: strip agent-speech that leaked into the mic before
        # committing.  Handles "hear you just fine. Can you hear me
        # okay? Yeah. I can hear you too. Am I talking to..." where
        # the first half is the agent's own greeting picked up by
        # the mic and the tail is the actual caller turn.
        stripped = _strip_agent_echo_prefix(text, self._recent_agent_utterances)
        if stripped != text:
            log.info("echo-prefix stripped call=%s: %r -> %r",
                     self.call_id, text, stripped)
            text = stripped
            if not text.strip():
                # Entire commit was echo — abort.
                return

        # K1 (2026-08-06, hardened): if transcript ends on an incomplete
        # trailing word AND we haven't held past the max deadline,
        # DON'T fire brain — extend the merge window and wait.  This
        # kills the "brain fires, gets cancelled by next merge, agent
        # speech cut mid-word" cascade observed 22:26:38-22:27:00.
        self._pending_k1_hint = ""
        try:
            from packages.runtime.turn_manager import _INCOMPLETE_TRAILING_WORDS
            stripped = text.strip()
            # 2026-08-07: skip K1 entirely if the sentence has terminal
            # punctuation (? . !).  Deepgram's smart-format only adds
            # these when it's confident the utterance ended — so
            # "who am I speaking with?" should commit immediately, not
            # wait 2 seconds because the last word (before "?") is "with".
            has_terminal_punct = stripped and stripped[-1] in "?.!"
            last_word = stripped.rstrip(".,!?;:").split()[-1].lower() if stripped else ""
            if not has_terminal_punct and last_word in _INCOMPLETE_TRAILING_WORDS:
                # Track how long we've been holding.  Hard cap = 5 sec
                # so a truly incomplete final still gets answered
                # rather than dead-air forever.
                now = time.monotonic()
                if not hasattr(self, "_incomplete_hold_started_at") or \
                        self._incomplete_hold_started_at is None:
                    self._incomplete_hold_started_at = now
                held_s = now - self._incomplete_hold_started_at
                # 2026-08-06: shortened from 5s → 2s.  5s felt like
                # dead air when caller genuinely stopped after "and".
                # 2s still catches natural Deepgram micro-pauses.
                if held_s < 2.0:
                    log.info("K1: HOLD (ends on %r, held %.1fs) call=%s: %r",
                             last_word, held_s, self.call_id, text[:80])
                    # Re-buffer the text and re-arm the merge window.
                    self._pending_turn_text = text
                    self._pending_turn_task = asyncio.create_task(
                        self._flush_pending_turn_after_window(),
                        name=f"merge-window-{self.call_id}",
                    )
                    return
                else:
                    log.info("K1: incomplete word %r but held %.1fs — committing",
                             last_word, held_s)
                self._pending_k1_hint = (
                    f"The caller's turn ended on '{last_word}' — the sentence "
                    f"looks incomplete.  Prefer a short targeted follow-up "
                    f"(like 'for the what?' or 'to which?') over guessing."
                )
        except Exception as e:
            log.debug("K1 completeness check failed: %s", e)

        # K1 hold-timer resets on commit — next turn starts fresh.
        self._incomplete_hold_started_at = None

        # Record the transcript + timestamp so a follow-on fragment
        # arriving after this point (during brain/speech) can be
        # continuation-merged rather than starting a competing turn.
        self._last_committed_transcript = text
        self._last_final_monotonic = time.monotonic()

        # 2026-08-10 (task #284): speculative dispatch short-circuit.
        # If we already fired a speculative brain on EAGER_END_OF_TURN
        # AND the confirmed text matches (or is a trivial prefix/suffix
        # extension), the reply is already in flight.  Its brain_completed
        # event will land on this same turn_generation.  Do NOT bump the
        # turn (that would cancel our own in-flight task) and do NOT
        # spawn a second brain.
        spec_task = getattr(self, "_speculative_task", None)
        spec_text = getattr(self, "_speculative_text", None) or ""
        if spec_task is not None and not spec_task.done() and spec_text:
            if _text_matches_for_speculative(spec_text, text):
                log.info("speculative HIT call=%s: text=%r spec=%r",
                         self.call_id, text[:60], spec_text[:60])
                # Clear the speculative markers; the task itself will
                # emit brain_completed as usual.
                self._speculative_task = None
                self._speculative_text = None
                return True
            # Text drifted — cancel speculative and fall through to
            # normal fire.
            log.info("speculative MISS call=%s: cancelling, text=%r spec=%r",
                     self.call_id, text[:60], spec_text[:60])
            spec_task.cancel()
            self._speculative_task = None
            self._speculative_text = None

        await actor.bump_turn(reason="end-of-turn")
        self._open_turn_span(actor.turn_generation)
        if self._current_turn_span is not None:
            self._current_turn_span.mark("media_in")
            self._current_turn_span.mark("stt_final")

        turn_gen = actor.turn_generation

        if settings.actor_nonblocking_handlers:
            # New path: spawn brain job, return immediately.  Job emits
            # control.brain_completed when done.
            actor.spawn_supervised(
                self._brain_job(text, turn_gen),
                generation=turn_gen,
                name=f"brain-{self.call_id}-{turn_gen}",
            )
            return

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
        # 2026-08-07: cancel any in-flight idle-followup the moment we
        # start thinking.  Otherwise a slow brain (LLM rate-limited,
        # tool loop, provider retry) crosses the 15s idle threshold
        # and "Anything else I can help you with?" fires in the
        # middle of a real response (observed on the just-finished
        # PK call at t+117s).
        self._cancel_idle_followup()

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

            # K1: if a hint was stashed for this turn, wrap it as a
            # synthetic turn-intent so the brain gets it as a fresh
            # system message (never touching transcript state).
            if self._pending_k1_hint:
                from types import SimpleNamespace
                state.last_turn_intent = SimpleNamespace(
                    intent="incomplete_turn",
                    confidence=0.9,
                    matched="",
                    system_note=self._pending_k1_hint,
                )
                self._pending_k1_hint = ""

            # Task B-wire (2026-08-08): reactive brain shadow path.
            # Feature-flagged OFF by default.  When ON, the reactive
            # brain returns structured JSON {should_speak, backchannel,
            # committed_reply, internal_thoughts} and we route:
            #   silent → append notepad + arm idle, no audio
            #   backchannel → play cached "mm-hm" (~10ms)
            #   commit → normal speech job path
            # Full plan: docs/rnd-2026-08/52-reactive-brain-wireup-plan.md
            if getattr(settings, "reactive_brain_enabled", False):
                try:
                    await self._brain_job_reactive(
                        state, brain, transcript, turn_gen, _elog,
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning(
                        "reactive brain failed, falling back to committed: %s", e,
                    )
                    # Fall through to committed path below.

            # 2026-08-08 (task #272): response cache — check BEFORE brain
            # fires.  If the caller asked a repeat-question ("do you take
            # Blue Cross", "what are your hours"), we already have the
            # answer + µ-law bytes on disk.  Skip brain + TTS entirely,
            # play cached audio in <150ms.  Only checks when NOT holding
            # a pending K1 hint (mid-completion turns get real brain).
            business_id = getattr(getattr(state, "business", None), "id", None) \
                or getattr(state, "business_id", None) or "unknown"
            _cache_hit = None
            if not self._pending_k1_hint:
                try:
                    from packages.response_cache import get_shared_response_cache
                    _rcache = get_shared_response_cache()
                    _cache_hit = _rcache.get(business_id, self.tenant_id, transcript)
                except Exception as _ce:
                    log.debug("response-cache lookup failed: %s", _ce)
            if _cache_hit is not None:
                log.info(
                    "RESPONSE_CACHE HIT call=%s biz=%s hits=%d input=%r → reply=%r",
                    self.call_id, business_id, _cache_hit.hits,
                    transcript[:60], _cache_hit.reply_text[:60],
                )
                # Emit brain_completed directly with cached reply — skips
                # LLM + kernel + tool loop entirely.  actor's normal
                # speech_job will TTS the reply (and hit the TTS cache).
                reply = _cache_hit.reply_text
                escalated = False
                tool_results = []
                speech_act = "info"
                if _elog is not None:
                    try:
                        _elog.write(_CE(
                            call_id=self.session_id, tenant_id=self.tenant_id,
                            source=_SK.LLM, kind="reply",
                            payload={
                                "reply": reply, "escalated": False,
                                "tool_results": [], "cache_hit": True,
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
                            "reply": reply, "escalated": False,
                            "tool_results": [], "speech_act": speech_act,
                            "turn_gen": turn_gen, "cache_hit": True,
                        },
                        source_epoch=turn_gen,
                    ))
                return

            # 2026-08-08 (task #271): if the brain takes >1200ms, play a
            # cached filler ("one sec", "let me check") so the caller
            # doesn't panic + Twilio doesn't drop the WS for idle.
            # Fires as a background task, cancelled the instant the brain
            # returns.  Uses the pre-warmed TTS cache — zero synth cost.
            _filler_task = asyncio.create_task(
                self._play_filler_on_slow_brain(turn_gen),
                name=f"filler-{self.call_id}-{turn_gen}",
            )
            try:
                payload = await session_manager.run_user_turn(state, brain, transcript)
            finally:
                if not _filler_task.done():
                    _filler_task.cancel()
            reply = (payload.get("reply") or "").strip()
            # 2026-08-10 FIX: escalated/tool_results were referenced BEFORE
            # being assigned (UnboundLocalError on live calls when the
            # cache-hit branch was skipped).  Assign upfront from payload.
            escalated = bool(payload.get("escalated"))
            tool_results = payload.get("tool_results") or []
            speech_act = _infer_speech_act_from_payload(payload)

            # 2026-08-08 (task #272): cache-write the (input → reply) mapping
            # so future callers with the same question skip brain entirely.
            # Only cache when there were NO tool calls (tool-dependent replies
            # are dynamic — bookings, availability checks — never cache those).
            # Uncacheable inputs (dates, times, PII, names) auto-rejected by
            # normalize_input inside the cache.
            if reply and not tool_results and not escalated:
                try:
                    from packages.response_cache import get_shared_response_cache
                    get_shared_response_cache().put(
                        business_id, self.tenant_id, transcript, reply,
                    )
                except Exception as _ce:
                    log.debug("response-cache put failed: %s", _ce)

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

    # ── Task B-wire: reactive brain (2026-08-08) ────────────────────

    async def _brain_job_reactive(
        self, state, brain, transcript: str, turn_gen: int, _elog,
    ) -> None:
        """Reactive brain path.  Returns structured JSON with 3 lanes:
        silent (understand, no audio), backchannel (cheap ack), commit
        (normal full reply).  See docs/rnd-2026-08/52-reactive-brain-wireup-plan.md."""
        from packages.core_agent.reactive_brain import reactive_turn
        from packages.schemas import TranscriptTurn, TurnRole
        from packages.observability.call_event_log import (
            CallEvent as _CE, EventSourceKind as _SK,
        )

        # 1. Append user turn (committed brain does this internally).
        state.add_turn(TranscriptTurn(role=TurnRole.USER, text=transcript))

        # 2. Build inputs.
        system_prompt = brain.system_prompt
        transcript_messages = state.to_llm_messages()
        notes = list(getattr(state, "_reactive_notes", []) or [])

        # 3. Call reactive brain.
        reply = await reactive_turn(
            llm_provider=brain.llm,
            system_prompt=system_prompt,
            transcript_messages=transcript_messages,
            running_notes=notes,
            tools=None,
            tenant_id=self.tenant_id,
            temperature=0.2,
        )

        # 4. Update notepad (bounded).
        if reply.internal_thoughts:
            notes.append(reply.internal_thoughts[:200])
            state._reactive_notes = notes[-10:]

        # 5. Consecutive-silent streak guard (5+ → force commit).
        streak = getattr(state, "_reactive_silent_streak", 0)
        if reply.lane == "silent":
            state._reactive_silent_streak = streak + 1
        else:
            state._reactive_silent_streak = 0

        log.info(
            "reactive lane=%s bc=%r commit_len=%d thoughts=%r streak=%d",
            reply.lane, reply.backchannel,
            len(reply.committed_reply or ""), reply.internal_thoughts[:80],
            getattr(state, "_reactive_silent_streak", 0),
        )

        # ── silent lane ────────────────────────────────────────────
        if reply.lane == "silent":
            if state._reactive_silent_streak >= 5:
                log.warning("reactive silent streak >=5, escalating to commit")
                # Fall through to committed brain by raising — outer
                # _brain_job's except catches and runs the committed path.
                raise RuntimeError("reactive_silent_streak_cap")
            self._arm_idle_followup()
            return

        # ── backchannel lane ───────────────────────────────────────
        if reply.lane == "backchannel":
            import time as _t
            # Rate-limit: only allow one backchannel per 4 sec.
            last_bc = getattr(self, "_last_backchannel_at", 0.0)
            if _t.monotonic() - last_bc < 4.0:
                log.info("reactive backchannel rate-limited → silent")
                self._arm_idle_followup()
                return
            # Only play backchannels while LISTENING (don't talk over
            # our own committed reply).
            if self.actor is not None and self.actor.state != CallState.LISTENING:
                log.info("reactive backchannel skipped (actor state=%s)",
                         self.actor.state)
                self._arm_idle_followup()
                return
            ok = await self._play_cached_backchannel(reply.backchannel, turn_gen)
            if ok:
                self._last_backchannel_at = _t.monotonic()
                # Track in agent-utterances buffer so caller repeating
                # "mm-hm" gets echo-suppressed.
                self._recent_agent_utterances.append(reply.backchannel)
                if len(self._recent_agent_utterances) > 3:
                    self._recent_agent_utterances.pop(0)
            self._arm_idle_followup()
            return

        # ── commit lane ────────────────────────────────────────────
        reply_text = (reply.committed_reply or "").strip()
        if not reply_text:
            log.warning("reactive commit lane returned empty reply, treating as silent")
            self._arm_idle_followup()
            return

        state.add_turn(TranscriptTurn(role=TurnRole.ASSISTANT, text=reply_text))

        # Log to durable event log same as committed path.
        if _elog is not None:
            try:
                _elog.write(_CE(
                    call_id=self.session_id, tenant_id=self.tenant_id,
                    source=_SK.LLM, kind="reply",
                    payload={"reply": reply_text, "escalated": False,
                             "tool_results": [], "lane": "commit_reactive"},
                    turn_generation=turn_gen,
                ))
            except Exception:
                pass

        # Emit brain_completed so the normal speech-job chain fires.
        if self.actor is not None:
            self.actor.emit_local(CallEvent.new(
                call_id=self.call_id, tenant_id=self.tenant_id,
                source=EventSource.CONTROL,
                turn_generation=turn_gen,
                speech_generation=self.actor.speech_generation,
                kind="brain_completed",
                payload={
                    "reply": reply_text,
                    "escalated": False,
                    "tool_results": [],
                    "speech_act": "inform",  # reactive doesn't infer speech acts yet
                    "turn_gen": turn_gen,
                },
                source_epoch=turn_gen,
            ))

    async def _play_cached_backchannel(self, phrase: str, turn_gen: int) -> bool:
        """Task B-wire: look up a backchannel phrase in the shared TTS
        cache and play the bytes directly.  Cache MISS = degrade to
        silent lane (never synthesise fresh — defeats latency point)."""
        from packages.tts_cache.cache import get_shared_cache, _hash_key
        from app.routes.twilio import _get_telephony_tts

        tts = _get_telephony_tts()
        voice = getattr(tts, "default_voice", "default")
        fmt = getattr(tts, "output_format", "unknown")
        provider = getattr(tts, "name", "tts")

        key = _hash_key(voice, phrase, fmt, provider)
        cache = get_shared_cache()
        hit = await cache.get(key)
        if hit is None:
            log.warning("reactive backchannel cache MISS for %r (voice=%s fmt=%s) — degrading to silent",
                        phrase, voice, fmt)
            return False
        audio, mime = hit
        log.info("reactive backchannel HIT: %r (%d bytes)", phrase, len(audio))
        await self._send_audio_frames(audio, mime)
        return True

    async def _play_filler_on_slow_brain(self, turn_gen: int) -> None:
        """2026-08-08 task #271: play a cached filler ('one sec', 'let me check')
        if the brain hasn't returned within FILLER_DELAY_MS.  Fires as
        a background task; the caller cancels it when the brain returns.

        Uses the pre-warmed filler pool (packages/voice/filler.py) so the
        audio is instant off disk — no synth latency + no additional LLM
        cost.  Only plays ONCE per turn (not looped) to avoid stepping
        on the real reply."""
        FILLER_DELAY_MS = 1200
        try:
            await asyncio.sleep(FILLER_DELAY_MS / 1000.0)
        except asyncio.CancelledError:
            return  # brain returned in time — no filler needed
        if self.actor is None or self.actor.turn_generation != turn_gen:
            return  # turn moved on
        # Pick a random cached filler line.  Pool warmed at boot.
        try:
            from packages.voice.filler import DEFAULT_FILLERS
            import random as _rand
            phrase = _rand.choice(DEFAULT_FILLERS)
        except Exception:
            phrase = "One sec."
        log.info("filler firing on slow brain call=%s turn=%d phrase=%r",
                 self.call_id, turn_gen, phrase)
        try:
            await self._play_cached_backchannel(phrase, turn_gen)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("filler play failed: %s", e)

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
        caller stays silent, and clear the continuation-merge anchor
        (the reply landed cleanly; the next caller turn is a fresh
        thought, not a continuation of the previous one)."""
        self._last_committed_transcript = ""
        self._last_final_monotonic = 0.0
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

            # Task #283: streaming LLM→TTS branch when eligible.
            if self._streaming_llm_eligible(brain):
                await self._run_brain_streaming(state, brain, transcript, turn_gen, span)
                if _elog is not None:
                    try:
                        _elog.write(_CE(
                            call_id=self.session_id, tenant_id=self.tenant_id,
                            source=_SK.LLM, kind="reply",
                            payload={"reply": "<streamed>", "streaming": True},
                            turn_generation=turn_gen,
                        ))
                    except Exception:
                        pass
                return

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

        # 2026-08-08 VOICE-BREAKUP FIX (task #270).  Root cause per
        # docs/rnd-2026-08 research: asyncio.sleep(0.02) cumulative
        # drift on a busy event loop = Twilio receives frames in bursts
        # instead of steady 50Hz inflow, its jitter buffer compensates
        # with skip/repeat = audible choppy voice.  Fix pattern is
        # Pipecat-verified + Twilio-endorsed: pre-buffer the whole
        # utterance, blast all frames without pacing, send one mark
        # at the end.  Twilio's playback engine paces to the caller
        # itself — it just needs the bytes promptly.  See:
        # https://github.com/pipecat-ai/pipecat/issues/826
        # https://elevenlabs.io/docs/cookbooks/text-to-speech/twilio
        frame_bytes = int(TWILIO_SAMPLE_RATE * (TWILIO_FRAME_MS / 1000))
        # Pad the trailing partial frame ONCE so every send is exactly
        # 20 ms of audio (Twilio drops or mis-times non-standard sizes).
        pad = (-len(mulaw)) % frame_bytes
        if pad:
            mulaw = mulaw + b"\xff" * pad
        if not self._ducked:
            # 2026-08-08 FIX v2: v1 blasted all frames with no sleep,
            # BUT that broke actor state — _stream_tts returned in 4ms
            # while Twilio was still playing the audio.  Actor flipped
            # SPEAKING → LISTENING immediately → mic un-ducked while
            # greeting still playing → speaker bleed hit the mic →
            # Deepgram VAD fired on our own audio → conversation broke.
            # New approach: burst frames in ~200ms batches (10 frames),
            # then sleep for that batch's real audio duration.  This
            # gives Twilio a steady inflow (no per-frame drift) AND
            # keeps _stream_tts's wall-clock aligned with actual
            # playback so state transitions are honest.
            BATCH_FRAMES = 10   # 10 * 20ms = 200ms batches
            batch_duration_s = BATCH_FRAMES * TWILIO_FRAME_MS / 1000.0
            frames_in_batch = 0
            batch_start = time.monotonic()
            for i in range(0, len(mulaw), frame_bytes):
                chunk = mulaw[i:i + frame_bytes]
                await self.ws.send_text(json.dumps({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(chunk).decode("ascii")},
                }))
                frames_in_batch += 1
                if frames_in_batch >= BATCH_FRAMES:
                    # Pace to real audio duration.  Uses monotonic wall
                    # clock so drift can't accumulate — if we've been
                    # slow, we sleep less; if we've been fast, we sleep
                    # the full batch duration.
                    elapsed = time.monotonic() - batch_start
                    to_sleep = batch_duration_s - elapsed
                    if to_sleep > 0:
                        await asyncio.sleep(to_sleep)
                    frames_in_batch = 0
                    batch_start = time.monotonic()

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
