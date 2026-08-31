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
from packages.core_agent.speech_commit_gate import SpeechCommitGate
from packages.slot_parsers import (
    SlotSource,
    SlotStatus,
    StructuredInputSession,
    get_slot_handlers,
)


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


async def _end_twilio_call(call_sid: str, reason: str = "hangup") -> None:
    """Force-end a Twilio call via the REST API.

    Twilio's Voice API accepts POST /Calls/{Sid}.json with `Status=completed`
    to terminate an in-progress call.  Without this, when we close our
    Media Streams WSS, Twilio may keep the call leg alive until it
    times out (~30s default), leaving the caller listening to silence
    after our goodbye.  User's friend reported "it doesn't hang up"
    on 2026-08-25 — this closes that gap.

    Requires TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN in env (already
    required for voice + SMS).  Skips silently if creds absent.

    Best-effort — logs on failure, never raises to caller.  Caller
    holds no lock while awaiting; safe to call from within stop().
    """
    if not call_sid or not call_sid.startswith("CA"):
        # Non-Twilio call_ids (dev/debug harness) — skip.
        return
    import os
    from base64 import b64encode
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    if not account_sid or not auth_token:
        log.debug(
            "twilio_end_call: TWILIO_ACCOUNT_SID/TOKEN unset — skip call=%s",
            call_sid,
        )
        return
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/"
        f"{account_sid}/Calls/{call_sid}.json"
    )
    creds = b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Basic {creds}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                content=b"Status=completed",
            )
        if resp.status_code == 200:
            log.info(
                "TWILIO_END_CALL_OK call=%s reason=%s status=%d",
                call_sid, reason, resp.status_code,
            )
        elif resp.status_code in (404, 409):
            # 404 = call already ended (caller hung up first).
            # 409 = call in a non-terminatable state (very rare).
            log.debug(
                "twilio_end_call: call=%s already-terminal status=%d",
                call_sid, resp.status_code,
            )
        else:
            log.warning(
                "TWILIO_END_CALL_FAIL call=%s reason=%s status=%d body=%s",
                call_sid, reason, resp.status_code, resp.text[:200],
            )
    except Exception as e:
        log.warning(
            "twilio_end_call exception call=%s: %s", call_sid, e,
        )


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


# 2026-08-23 CAab964e92 fix: caller confirmations that repeat agent-mentioned
# values verbatim ("Yeah. Nine forty five AM." after the agent listed
# "nine forty five am" as a slot) trip _looks_like_agent_echo because 80%+
# of the transcript matches an agent utterance with only one novel word
# ("yeah"). But real mic-echo never starts with a caller acknowledgment
# token — those are caller-initiated. If the transcript begins with one,
# it's a confirmation/correction, NOT echo. Skip the echo filter.
_ACK_PREFIX_TOKENS = {
    "yeah", "yes", "yep", "yup", "sure", "ok", "okay", "no", "nope",
    "right", "correct", "actually", "wait", "hold", "hmm", "uh", "um",
    "well", "so",
}


# 2026-08-24 ChatGPT breakthrough audit: sentinel raised inside the
# K1 completeness check to skip the entire K1 block when Flux is
# authoritative for turn boundaries.  Flux is a semantic EOT detector
# that has already made the same call K1's lexical heuristic would;
# adding K1 on top was manufacturing 2-second dead time on turns like
# "Or day after" (last word "after" is in K1's incomplete-trailing
# words). Using a sentinel avoids restructuring the try/except.
class _K1SkipSentinel(Exception):
    """Signals K1 completeness block should be skipped (Flux authoritative)."""
    pass


# 2026-08-23 CAf535b0dd defense-in-depth: catch LLM output that
# paraphrases internal slot names / prompt-instruction fragments as if
# they were spoken to the caller. Trace: after book_appointment fired,
# the LLM produced "I want to make sure I have that right — caller
# provided name..." and it hit STREAM_BATCH_FALLBACK → TTS. Not a
# JSON leak (our two-layer JSON guards caught real tool JSON); this is
# the model composing text about its own internal state.
#
# Two-part detection: (1) any of the slot-name / instruction fragments
# below appears verbatim in the reply, AND (2) the reply starts with a
# "meta" opener suggesting confirmation/paraphrase rather than a real
# spoken reply. Both together avoid false-positives on legitimate use
# ("Can I get your name?" would trigger the name-token but has no meta
# opener).
_META_LEAK_TOKENS = (
    "caller provided name", "caller_name", "caller name and",
    "caller-provided", "provided name", "provided phone",
    "start_iso", "start iso", "book_appointment",
    "check_availability", "emit_semantic_plan",
    "ai times is a suggested",  # observed variant
    "the caller_", "caller_phone",
)
_META_LEAK_OPENERS = (
    "i want to make sure", "let me confirm the", "let me make sure",
    "to confirm the following", "based on what you provided",
    "using the following", "given the information",
)


def _looks_like_leaked_metadescribe(text: str) -> str | None:
    """Return the matching token if `text` looks like the LLM leaking
    prompt-template slot names as spoken content, else None.

    Uses a two-part heuristic to avoid false positives:
    (a) reply contains a slot-name-shaped token OR internal tool name
    (b) reply starts with a "meta" opener (I want to make sure...) OR
        the slot-name token appears without a natural language shape.
    """
    low = text.lower().lstrip()
    # If any tool name / slot-underscore variant appears at all, it's
    # a leak regardless of opener — no legitimate spoken reply says
    # "caller_name" or "book_appointment" out loud.
    hard_tokens = (
        "caller_name", "caller_phone", "start_iso", "book_appointment",
        "check_availability", "emit_semantic_plan", "caller-provided",
        # Observed on CAf535b0dd variant. No legit spoken reply says
        # "AI times is a suggested time" — that phrasing is the model
        # narrating its own reasoning about slot availability.
        "ai times is a suggested",
    )
    for t in hard_tokens:
        if t in low:
            return t
    # Otherwise require BOTH a soft token AND a meta opener.
    has_soft_token = any(t in low for t in _META_LEAK_TOKENS if t not in hard_tokens)
    has_opener = any(low.startswith(op) for op in _META_LEAK_OPENERS)
    if has_soft_token and has_opener:
        for t in _META_LEAK_TOKENS:
            if t in low:
                return t
    return None


def _starts_with_ack(transcript: str) -> bool:
    """Return True if the transcript begins with a caller acknowledgment
    token (yeah/yes/sure/no/etc.). Used to bypass the echo filter — real
    mic-echo of the agent's own audio never starts with these tokens.

    Match on the FIRST word only, case-insensitive, ignoring punctuation.
    """
    import re as _re
    m = _re.match(r"\s*([a-zA-Z']+)", transcript)
    if not m:
        return False
    return m.group(1).lower() in _ACK_PREFIX_TOKENS


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
        caller_number: Optional[str] = None,
        dialed_number: Optional[str] = None,
        caller_name: Optional[str] = None,
    ) -> None:
        self.ws = ws
        self.stream_sid = stream_sid
        self.call_id = call_id
        self.tenant_id = tenant_id
        # session_id is the brain's session key — keeps back-compat with
        # session_manager which was built pre-actor.
        self.session_id = session_id or f"twilio_{call_id}"

        # R3 P3 (task #370): caller ANI + dialed DNIS, populated from
        # Twilio's start.customParameters (see _twiml_stream_response).
        # Empty string when Twilio didn't supply them (e.g. Media
        # Streams without <Parameter> tags in the TwiML, or unknown
        # blocked-caller IDs).  Used by resolve_ani_candidate() to
        # offer "use the number you're calling from" during phone-slot
        # capture — never auto-committed without caller confirmation.
        self.caller_number: str = (caller_number or "").strip()
        self.dialed_number: str = (dialed_number or "").strip()
        self.caller_name: str = (caller_name or "").strip()

        # 2026-08-31 task #104-followup: in-app audio recorder. Tees
        # µ-law both directions into a per-side buffer, flushes to a
        # stereo MP3 in stop(). Disabled when settings.call_recording_enabled
        # is false (default false — set to true on prod). Wrapped in a
        # try to keep the call alive if the recording package can't load.
        self.recorder = None
        try:
            if getattr(settings, "call_recording_enabled", False):
                from packages.recording import AudioRecorder
                from pathlib import Path as _Path
                self.recorder = AudioRecorder(
                    call_id=self.call_id,
                    tenant_id=self.tenant_id,
                    root_dir=_Path(
                        getattr(settings, "call_recording_dir", "data/recordings")
                    ),
                )
        except Exception as _e:
            log.warning("recorder init failed for %s: %s", self.call_id, _e)
            self.recorder = None

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
        # 2026-08-13 (M1 task #343): event-loop lag watchdog task.
        # Sleeps 20ms in a loop; if wake-up drift exceeds threshold we
        # log a WARN.  Catches the case where create_task(_speak(...))
        # looks non-blocking but sync work between awaits still stalls
        # the receiver.
        self._loop_lag_watchdog_task: Optional[asyncio.Task] = None
        # 2026-08-13 (R1 P0): zombie-SPEAKING watchdog.  Runs alongside
        # loop-lag watchdog.  If actor is SPEAKING for >3s without any
        # outbound wire activity, forcibly transition back to LISTENING
        # (self-healing invariant) + log a WARN so we notice the leak.
        # Belt-and-suspenders behind the pump's try/finally epilogue —
        # if a future new speech code path forgets its lifecycle, this
        # catches it instead of the caller experiencing dead air.
        self._speaking_watchdog_task: Optional[asyncio.Task] = None
        self._last_wire_send_at: Optional[float] = None
        self._speaking_entered_at: Optional[float] = None
        # 2026-08-13 (R5 P0): turn-stall watchdog.  Stamped when a
        # caller final is committed to brain dispatch.  Cleared when
        # ANY response signal fires (TTS, LLM stream start, tool start,
        # state transition to SPEAKING).  If it stays stamped >3s the
        # watchdog logs TURN_STALLED with full context.  Would have
        # diagnosed Hamzah's 40s dead air instantly.
        self._turn_stall_watchdog_task: Optional[asyncio.Task] = None
        self._committed_turn_at: Optional[float] = None
        self._committed_turn_transcript: str = ""
        self._committed_turn_gen: int = -1
        self._turn_stalled_logged: bool = False
        # 2026-08-13 (M1 task #343): outbound wire-to-ear instrumentation.
        # send_wall recorded when we ship the "FIRST40_<n>" mark; the
        # ack on that mark tells us caller actually heard the first
        # 40ms of this reply.  ack_wall - send_wall = TRUE wire-to-ear
        # latency for this call, no guessing.
        self._first40_send_wall: dict[str, float] = {}
        self._first40_counter: int = 0
        # 2026-08-23 AUDIT-S2: FIRST40 is now emitted at most ONCE per
        # reply — not once per audio chunk.  Streaming TTS calls
        # `_send_audio_frames` per EL chunk; without this gate every chunk
        # emitted its own FIRST40 mark (20+ marks per streamed answer),
        # burying the one meaningful measurement in log noise.  The gate
        # is armed at reply-boundary (start of `_stream_tts_incremental`,
        # cache fastpath send, `_play_cached_backchannel`) and disarmed
        # inside `_send_audio_frames_locked` the first time a mark is
        # sent for that source.  See docs/rnd-2026-08/54-chatgpt-audit-
        # response.md, S2.
        self._first40_pending: dict[str, bool] = {}
        # 2026-08-24 CAff590033: TWILIO_FIRST_MEDIA_SENT was also
        # emitting per-frame (same design bug as FIRST40 before I fixed
        # that). Making it one-per-reply-source using same pattern as
        # _first40_pending. Callers must arm this at reply-boundary.
        self._first_media_pending: dict[str, bool] = {}
        # 2026-08-24 ChatGPT audit: per-turn tracking to guarantee
        # FIRST_MEDIA_SENT + FIRST40 fire ONCE per reply, not per
        # sentence. `_stream_tts_incremental` runs per-sentence inside
        # a streaming reply, so its arm site checks this set first.
        # Cleared on turn advance (bump_turn) so a new turn re-arms.
        self._first_media_emitted_turn_gens: set[int] = set()
        # 2026-08-21 NET-03: outbound audio arbiter.  Serializes access to
        # the Twilio Media WebSocket so filler + real answer + greeting
        # can never interleave frames on the wire.  FastAPI's send_text
        # is NOT message-boundary safe when called from concurrent
        # coroutines — a filler frame partway through a real answer
        # would corrupt Twilio's Media Stream JSON parsing.  Any code
        # path that streams audio (fastpath, cache, filler, LLM stream)
        # holds this lock for the duration of its send.
        self._outbound_audio_lock: asyncio.Lock = asyncio.Lock()
        # 2026-08-22 NET Ship 2: turns marked "may-be-partial" by the
        # interruption handler when we couldn't wait for STT_FINAL.
        # The streaming brain completion path checks this set BEFORE
        # writing to RESPONSE_CACHE and suppresses the write.  Prevents
        # cache-poisoning where a barge-in on a partial ("the general
        # appointment") stores a wrong-context reply that later replays
        # when the final promotes to a real END_OF_TURN.
        self._may_be_partial_turns: set[int] = set()
        # 2026-08-21 NET-14-followup: TTS_STREAM_START/DONE watchdog.
        # Every TTS_STREAM_START opens a tracker; TTS_STREAM_DONE closes
        # it.  A watchdog task fires WARN if no DONE arrives within the
        # deadline (spoken duration + 2s grace).  Catches premature-
        # hangup cutoffs, WS drops during synth, cancel/reconnect leaks.
        # Keyed by stream_id "<turn_gen>_<stream_counter>".  Value:
        # {"started_at": monotonic, "text": str preview, "source":
        # "answer"|"filler"|"cache", "watchdog_task": Task, "deadline_s": float}
        self._tts_open_streams: dict[str, dict] = {}
        self._tts_stream_counter: int = 0
        # 2026-08-13 (P0-startup fix): greeting is fired as a background
        # task from session.start() so the WebSocket receive loop can
        # begin consuming inbound caller media immediately.  Tracked so
        # stop() can cancel on hangup.
        self._greeting_task: Optional[asyncio.Task] = None
        # 2026-08-31 CALL-BUG-09: greeting was playing BEFORE Twilio's
        # audio pipe to the caller was fully up. User reported "it also
        # plays the greeting early before i can start hearing." Twilio
        # documents that the WSS `start` event fires when the media
        # stream is established SERVER-SIDE, but the carrier's audio
        # path to the caller may take another 100-500ms to bring up.
        # Cleanest signal: the first inbound `media` frame — that
        # PROVES the caller is on the line and the bidirectional pipe
        # is live. This Event fires from on_media() when the first real
        # µ-law frame arrives; the greeting task awaits it (with a
        # timeout fallback so a totally-silent caller still gets a
        # greeting).
        self._caller_media_arrived: asyncio.Event = asyncio.Event()
        # 2026-08-13 (P0-startup fix): rate-limit counter for Twilio
        # media-event debug logs — the first N frames per call get their
        # media.timestamp + wall-clock delta printed so we can prove the
        # receive loop isn't stalling.
        self._twilio_media_debug_count: int = 0
        # 2026-08-13 (M1 task #343): same-origin skew.  On the FIRST
        # inbound frame we record BOTH the local wall clock and the
        # carrier timestamp; every subsequent frame's skew is computed
        # against those bases.  The previous metric compared local wall
        # (from first-frame-received) against media.timestamp (from
        # stream-start on Twilio's side) — two different zero points →
        # nonsense negative "lag" values.  Cadence skew answers:
        # "is the receive loop keeping up with the sender in real time?"
        self._twilio_base_wall: Optional[float] = None
        self._twilio_base_media_ts: Optional[int] = None
        # 2026-08-13 (N1 task #344): persistent OpenAI Responses WS —
        # opened during session.start() when enabled, warmed concurrently
        # with the greeting playback, closed on stop().  Only used for
        # the terminal no-tools LLM turn; tool-call turns keep routing
        # through the HTTP router until we've soaked the WS path.
        self._openai_ws = None  # OpenAIResponsesWS | None
        self._openai_ws_warm_task: Optional[asyncio.Task] = None
        # Rolling text buffer captured from streaming STT so END_OF_TURN
        # has a final utterance to feed the brain.
        self._streaming_utterance_text = ""

        # 2026-08-20: monotonic wall-time of the most recent STT final
        # with real text.  Used to compute CALLER_LATENCY = time from
        # DG speech_final → first TTS byte leaves us.  Reset each turn.
        self._last_stt_final_perf: Optional[float] = None
        # 2026-08-24 CAff590033 fix: separate timestamp for
        # POST_EOT_HOLD metric. _last_stt_final_perf is consumed +
        # cleared by CALLER_LATENCY emit (line 3354/3550) which runs
        # BEFORE POST_EOT_HOLD emit — so POST_EOT_HOLD kept reading
        # None. This second timestamp is set alongside but only
        # cleared when a new STT_FINAL fires, so POST_EOT_HOLD always
        # sees the real Flux Final EndOfTurn wall time.
        self._last_stt_final_perf_hold: Optional[float] = None
        self._last_stt_final_text: str = ""

        # Idle-followup: after the agent finishes speaking, we wait
        # for the caller.  If they stay silent, we prompt once ("Anything
        # else?"), then say goodbye + hangup on the next silence window.
        self._idle_task: Optional[asyncio.Task] = None
        self._idle_prompted: bool = False
        # 2026-08-23 CAf535b0dd hangup-forever fix:
        # `_maybe_hangup_after_farewell` had a "caller resumed" check
        # gated on `_idle_task is not None and not done()`, but
        # `_arm_idle_followup` runs on EVERY `_speak()` finally — so
        # that check was ALWAYS true after a farewell was spoken, and
        # hangup was ALWAYS aborted. Verified on CA792b1dcf: farewell
        # scheduled → FAREWELL_HANGUP_ABORTED (caller resumed conv) →
        # never actually hangs up. User reported "calls stay on
        # forever". Fix: track whether caller ACTUALLY said something
        # (STT_FINAL fired) between farewell-schedule and now, not
        # whether the idle-followup timer exists.
        self._caller_spoke_since_farewell: bool = False
        # 2026-08-21 NET: internal-flag guard for idle-followup ladder.
        # `_speak()` unconditionally re-arms idle in its finally block.
        # When the idle loop itself calls _speak(_IDLE_PROMPT), that
        # finally would reset `_idle_prompted=False` → next idle window
        # asks "Anything else?" AGAIN instead of the farewell → infinite
        # ladder (verified on CA6cd29489 at 07:15:41 + 07:15:57 both
        # firing the same prompt 15s apart). Setting this to True around
        # the idle-loop's internal _speak() calls makes _arm_idle_followup
        # skip the reset — so the ladder correctly progresses PROMPT →
        # FAREWELL → hangup.
        self._arming_from_idle_loop: bool = False

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
        # 2026-08-13 (double-brain fix): track the transcript that most
        # recently dispatched a brain, and whether that brain has already
        # spoken any audio.  Used by _should_dedupe_dispatch to suppress
        # the "prefix-then-full-sentence" double-fire pattern from
        # Deepgram's aggressive endpointing (see stt/deepgram_stt.py:126).
        self._inflight_dispatch_transcript: str = ""
        self._inflight_dispatch_monotonic: float = 0.0
        self._inflight_has_spoken: bool = False
        # 2026-08-17 (fastpath triple-speak fix): set True when the
        # conv-control fastpath speaks on EAGER; consumed in
        # _commit_final_transcript to suppress arming the
        # continuation-merge anchor.  Otherwise a fresh sentence 5s
        # later gets stitched to the fastpath's original text and
        # re-dispatched, producing fastpath + filler + LLM triple-speak.
        self._suppress_next_continuation_anchor: bool = False
        # 2026-08-18: scheduled hangup after the LLM says goodbye.
        # Tracked so a second farewell within the window doesn't stack.
        self._farewell_hangup_task: Optional[asyncio.Task] = None
        # 2026-08-18 (T2): async smart-turn worker.  Runs det.predict off
        # the receive loop; TurnManager reads the latest cached P(EOT)
        # in O(1).  Tracked here so stop() can cancel on hangup.
        self._smart_turn_worker_task: Optional[asyncio.Task] = None
        # K1: hint for the brain about an incomplete-looking turn.
        # Populated by _flush_pending_turn_after_window, consumed by
        # _brain_job.  NEVER goes into the transcript — that broke
        # continuation-merge earlier.
        self._pending_k1_hint: str = ""
        # K1 (2026-08-06): tracks when we started holding an incomplete
        # turn.  Bounded so we don't hold forever.
        self._incomplete_hold_started_at: Optional[float] = None

        # Task #369 (2026-08-14): per-generation response commit lock.
        # Invariant: ONE generation → at most ONE committed assistant
        # response (speculative revisions may live internally).  Set
        # of turn_generations that already have a response committed
        # to speech.  Any dispatch site checks this before firing;
        # cleared on bump_turn.  Prevents the same-gen double-fire
        # observed on Abdullah's gen=20 (speculative HIT cleared the
        # in-flight marker while the task was still streaming; a
        # follow-on EAGER_END_OF_TURN then slipped through the "no
        # in-flight speculative" guard and dispatched a second brain
        # on the SAME gen).
        self._committed_response_gens: set[int] = set()
        # Bookkeeping for the ChatGPT-requested response-id shape.
        # Kept minimal in v1 — a monotonic counter per gen — so future
        # replacement semantics have a stable label.  Not yet consumed
        # by the ledger/telemetry.
        self._response_revision_counter: dict[int, int] = {}

        # R3 P2 (2026-08-14): structured-input capture mode.  When
        # active, STT finals feed a slot session instead of running the
        # brain, and speculative brain dispatch is suppressed.  Actor
        # is slot-type-agnostic; the WORKFLOW CONTROLLER (not tools)
        # opens capture.  Tools operate on already-validated values.
        self._active_slot: Optional[StructuredInputSession] = None
        self._slot_normalizer = None
        self._slot_on_commit = None          # coro(SlotResult) -> None
        self._slot_on_stall = None           # coro(stage: str, session) -> None
        self._slot_on_confirm_needed = None  # coro(SlotResult) -> None
        self._slot_stall_task: Optional[asyncio.Task] = None
        self._slot_stall_first_prompt_s: float = 6.0
        self._slot_stall_escalate_s: float = 8.0
        # LK steal #7 (2026-08-29): narrow sub-agent prompt attached
        # while a slot capture is active.  Downstream brain turn reads
        # `active_slot_prompt` and injects .instructions as a
        # system-note when non-None.  Cleared on exit_slot_capture.
        self._active_slot_prompt = None

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
                        # ONNX (~17ms warm, ~450ms cold on first call, up
                        # to 260ms tail per ChatGPT audit).
                        # 2026-08-18: refactored from "sync predict on
                        # every call, clamp with a 25ms budget" to a
                        # producer/consumer:
                        #   - a background worker calls det.predict via
                        #     asyncio.to_thread every ~200ms and stashes
                        #     the latest P(EOT) into _cache
                        #   - TurnManager's synchronous provider hook
                        #     returns the cached float in O(1)
                        # Result: ONNX inference NEVER runs on the
                        # asyncio receive loop, so the 260ms EVENT_LOOP_LAG
                        # spikes go away and Deepgram's audio consumer
                        # can't starve.  The cache is stale by at most
                        # one worker interval (~200ms), well below the
                        # smart-turn CONFIRM window.
                        _cache: dict = {
                            "ts": 0.0, "val": 0.5, "failures": 0,
                            "last_dur_ms": 0.0,
                        }

                        def _predict_eot() -> float:
                            # Synchronous, O(1), safe on the event loop.
                            # Returns the last cached value or 0.5 if
                            # the worker hasn't produced one yet.
                            return _cache["val"]

                        async def _smart_turn_worker() -> None:
                            # Runs for the life of the call.  Every 200ms:
                            # snapshot the last ~4s of caller PCM, run
                            # det.predict off the event loop in a thread,
                            # stash the result.  After 3 consecutive
                            # failures we STOP the worker so we don't
                            # keep burning threads on a broken model.
                            import time as _t
                            import asyncio as _aio
                            while True:
                                try:
                                    await _aio.sleep(0.2)
                                except _aio.CancelledError:
                                    return
                                if _cache["failures"] >= 3:
                                    return
                                pcm = bridge.get_recent_pcm16k(seconds=4.0)
                                if len(pcm) < 16000:
                                    continue
                                try:
                                    t0 = _t.perf_counter()
                                    v = await _aio.to_thread(det.predict, pcm)
                                    dur_ms = (_t.perf_counter() - t0) * 1000
                                    _cache["last_dur_ms"] = dur_ms
                                    if dur_ms > 250:
                                        _cache["failures"] += 1
                                        log.warning(
                                            "smart-turn slow: %.0fms (failure %d/3) "
                                            "call=%s",
                                            dur_ms, _cache["failures"], self.call_id,
                                        )
                                    else:
                                        _cache["failures"] = 0
                                    _cache["val"] = float(v)
                                    _cache["ts"] = _t.monotonic()
                                except _aio.CancelledError:
                                    return
                                except Exception as _e:
                                    _cache["failures"] += 1
                                    log.debug("smart-turn predict failed: %s", _e)

                        self._turn_manager._eot_probability_provider = _predict_eot
                        # Track worker task so stop() can cancel it.
                        self._smart_turn_worker_task = asyncio.create_task(
                            _smart_turn_worker(),
                            name=f"smart-turn-worker-{self.call_id}",
                        )
                        log.info(
                            "smart-turn-v3 EOT provider installed call=%s "
                            "(async worker, 200ms interval)",
                            self.call_id,
                        )
                    except Exception as e:
                        log.warning("smart-turn init failed (falling back to text-only EOT): %s", e)
                log.info("turn manager attached for call=%s", self.call_id)
            except Exception as e:
                log.warning("turn manager disabled: %s", e)
                self._turn_manager = None

        # 2026-08-13 (P0-startup fix): silence-pump gated OFF by default.
        # It was created to seed Deepgram's VAD before real caller media
        # arrived — but the real reason media didn't flow was that
        # session.start() blocked the WebSocket receive loop on greeting
        # playback (~2.4s).  Now that greeting is fire-and-forget below,
        # real caller frames flow into on_media() from t=0 continuously
        # and the pump becomes redundant duplication that stacked on
        # top of the queued-burst real audio → confused VAD → wasted
        # 1.8s on an empty speech_final at turn 1.
        # Flag lets us bring it back for A/B if the concurrency fix
        # ever regresses.
        if settings.silence_pump_enabled and self._stt_bridge is not None:
            self._silence_pump_task = asyncio.create_task(
                self._silence_pump(),
                name=f"silence-pump-{self.call_id}",
            )

        # 2026-08-13 (M1 task #343): event-loop lag watchdog.  Runs for
        # the life of the call, ~50Hz, negligible CPU.  Emits WARN when
        # loop drift exceeds 20ms.  If we see EVENT_LOOP_LAG during
        # greeting or during a real turn, some coroutine is doing sync
        # work between awaits.
        self._loop_lag_watchdog_task = asyncio.create_task(
            self._loop_lag_watchdog(),
            name=f"loop-lag-{self.call_id}",
        )
        # 2026-08-13 (R1 P0): zombie-SPEAKING self-heal watchdog.
        # 500ms cadence, forces SPEAKING→LISTENING if state has been
        # SPEAKING with no outbound wire activity for >3s.
        self._speaking_watchdog_task = asyncio.create_task(
            self._speaking_watchdog(),
            name=f"speaking-wd-{self.call_id}",
        )
        # 2026-08-13 (R5 P0): turn-stall watchdog.  ERRORs a
        # TURN_STALLED line if a committed caller turn has no response
        # signal within 3s.  Diagnoses zombie states before the caller
        # notices.
        self._turn_stall_watchdog_task = asyncio.create_task(
            self._turn_stall_watchdog(),
            name=f"turn-stall-wd-{self.call_id}",
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

        # 2026-08-23 AUDIT-S6: prompt version stamping.  Every call log
        # now carries the SHA-256 (12-char prefix) + char-length of the
        # system prompt actually loaded into this call, plus the current
        # OpenAI model name.  Solves the recurring "was this call before
        # or after Ship 6?" archaeology by anchoring each call's log to
        # a specific prompt build.  When we bump the prompt (or model),
        # you can filter logs by CALL_START_PROMPT sha to see only
        # matching calls.  Hash the string, not the file, so runtime
        # substitutions (business profile, tools) that alter the built
        # prompt still register as distinct versions.
        try:
            import hashlib as _hashlib
            _sp = brain.system_prompt or ""
            _sp_sha = _hashlib.sha256(_sp.encode("utf-8")).hexdigest()[:12]
            log.info(
                "CALL_START_PROMPT call=%s prompt_sha=%s prompt_chars=%d "
                "model=%s",
                self.call_id, _sp_sha, len(_sp),
                settings.openai_model or "unknown",
            )
        except Exception:
            # Hashing is telemetry-only; a failure here must never block
            # the call from proceeding.
            log.debug("CALL_START_PROMPT hash failed", exc_info=True)

        # 2026-08-12 UPDATE: LLM prompt-cache prewarm REMOVED.
        # Research (see /tmp research summary): on 1500-token prompts
        # OpenAI cache saves only 10-15% TTFT (~200-300ms).  Prewarm
        # request itself contends for the same HTTP/2 connection as
        # the real turn.  Net effect: neutral to slightly negative.
        # If we go back to gpt-5.4-nano (reasoning model), prewarm
        # doesn't help at all because the "slow" is internal reasoning
        # tokens, not prefix processing.  Kept the code path clean.

        # 2026-08-13 (N1 task #344): open persistent OpenAI Responses WS
        # + warm it in the background so warmup happens concurrent with
        # the greeting playback below.  On any WS failure we log + drop
        # to None; _run_brain_streaming falls back to the HTTP router.
        if settings.openai_persistent_ws_enabled:
            try:
                from app.providers.llm.openai_responses_ws import OpenAIResponsesWS
                self._openai_ws = OpenAIResponsesWS(call_id=self.call_id)

                async def _open_and_warm():
                    try:
                        ok = await self._openai_ws.open(
                            system_prompt=brain.system_prompt,
                            tools=brain.tools,
                            warm=True,
                        )
                        if not ok:
                            self._openai_ws = None
                    except Exception:
                        log.exception("openai persistent WS open/warm failed")
                        self._openai_ws = None

                self._openai_ws_warm_task = asyncio.create_task(
                    _open_and_warm(),
                    name=f"openai-ws-warm-{self.call_id}",
                )
            except Exception:
                log.exception("openai persistent WS init failed")
                self._openai_ws = None

        greeting = await session_manager.run_greeting(state, brain)
        self.actor.transition(CallState.GREETING)
        # 2026-08-13 (P0-startup fix): fire-and-forget the greeting so
        # session.start() returns to the WebSocket receive loop BEFORE
        # the ~2.4s of paced µ-law playback.  Previously we awaited
        # _speak(greeting) here, blocking the outer loop from calling
        # ws.receive_text() — which meant real caller media queued in
        # the WebSocket buffer for the entire greeting.  When the loop
        # unblocked, ~2.4s of stacked media flushed into Deepgram all
        # at once.  Combined with silence-pump's fake µ-law that had
        # been flowing in parallel, DG saw a corrupted VAD startup:
        # empty speech_final at t=3.6s, wasted 1.8s finding the real
        # one at t=5.4s, then finally usable transcript at t=6.6s.
        # That was the 3.7 sec pre-LLM tax on turn 1.
        # By spawning as a task we return immediately; on_media() sees
        # real frames continuously from t=0 and feeds DG in real time.
        # _greeting_task is tracked so stop() can cancel on hangup.
        # 2026-08-31 CALL-BUG-09 (v2): the greeting is TTS-cache-warmed
        # at boot so _speak() returns cached µ-law bytes almost instantly.
        # v1 fix (wait for first-inbound-frame) helped but wasn't enough
        # — even after first frame, the CARRIER's PSTN audio path to the
        # caller's ear takes another ~200-400ms to fully stabilize. User
        # reported still only hearing "how can I help" — the first
        # ~2 seconds got eaten. Real fix: gate on first frame AND then
        # sleep a fixed lead-in so the caller's ear is definitely ready.
        # Total: first frame + 600ms padding. Empirically this is where
        # the carrier reliably has audio flowing back to the caller.
        _GREETING_LEAD_IN_MS = 600
        async def _greet_when_pipe_ready():
            try:
                await asyncio.wait_for(
                    self._caller_media_arrived.wait(),
                    timeout=1.5,
                )
                log.info(
                    "GREETING_PIPE_READY call=%s — first frame arrived, padding %dms before speak",
                    self.call_id, _GREETING_LEAD_IN_MS,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "GREETING_PIPE_TIMEOUT call=%s — no inbound frame in 1.5s, greeting anyway (caller may hear truncated hello)",
                    self.call_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("greeting-gate errored", exc_info=True)
            # Post-first-frame padding so the carrier's PSTN audio to
            # the caller's ear is definitely ready to receive our
            # cached greeting bytes.
            try:
                await asyncio.sleep(_GREETING_LEAD_IN_MS / 1000.0)
            except asyncio.CancelledError:
                raise
            await self._speak(greeting)

        self._greeting_task = asyncio.create_task(
            _greet_when_pipe_ready(),
            name=f"greeting-{self.call_id}",
        )

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
        # 2026-08-13 (M1 task #343): stop the event-loop lag watchdog.
        if self._loop_lag_watchdog_task is not None and not self._loop_lag_watchdog_task.done():
            self._loop_lag_watchdog_task.cancel()
            self._loop_lag_watchdog_task = None
        # 2026-08-13 (R1 P0): stop the zombie-SPEAKING watchdog.
        if self._speaking_watchdog_task is not None and not self._speaking_watchdog_task.done():
            self._speaking_watchdog_task.cancel()
            self._speaking_watchdog_task = None
        # 2026-08-13 (R5 P0): stop the turn-stall watchdog.
        if self._turn_stall_watchdog_task is not None and not self._turn_stall_watchdog_task.done():
            self._turn_stall_watchdog_task.cancel()
            self._turn_stall_watchdog_task = None
        # 2026-08-13 (P0-startup fix): cancel greeting task if caller
        # hung up mid-greeting.
        if self._greeting_task is not None and not self._greeting_task.done():
            self._greeting_task.cancel()
            self._greeting_task = None
        # 2026-08-21 NET-14-followup: close any open TTS stream trackers
        # so hangup during mid-stream doesn't leave phantom watchdogs
        # firing HUNG warnings after the call is dead.
        self._tts_close_all_streams(reason=f"stop:{reason}")
        # 2026-08-18: cancel the pending farewell-hangup task if stop()
        # was reached via another route (idle timeout, caller hangup, ...).
        if self._farewell_hangup_task is not None and not self._farewell_hangup_task.done():
            self._farewell_hangup_task.cancel()
            self._farewell_hangup_task = None
        # 2026-08-18 (T2): stop the async smart-turn worker.
        if self._smart_turn_worker_task is not None and not self._smart_turn_worker_task.done():
            self._smart_turn_worker_task.cancel()
            self._smart_turn_worker_task = None
        # 2026-08-13 (N1 task #344): tear down persistent OpenAI WS.
        # Warmup task may still be in flight — cancel then close.
        if self._openai_ws_warm_task is not None and not self._openai_ws_warm_task.done():
            self._openai_ws_warm_task.cancel()
            self._openai_ws_warm_task = None
        if self._openai_ws is not None:
            try:
                await self._openai_ws.close()
            except Exception:
                log.debug("openai persistent WS close failed", exc_info=True)
            self._openai_ws = None
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

        # 2026-08-31 task #104-followup: finalize the audio recording,
        # persist path/duration/size to sessions row. Never blocks
        # shutdown — swallow errors, we already logged inside finalize.
        if self.recorder is not None:
            try:
                out_path, dur_ms = await self.recorder.finalize()
                if out_path is not None:
                    # Store as path RELATIVE to root_dir, not absolute —
                    # so a config change (dir move) doesn't invalidate
                    # existing DB rows.
                    from pathlib import Path as _Path
                    root = _Path(
                        getattr(settings, "call_recording_dir", "data/recordings")
                    ).resolve()
                    try:
                        rel = out_path.resolve().relative_to(root)
                        rel_str = str(rel)
                    except ValueError:
                        # Not under root_dir — store as-is (shouldn't
                        # happen but survive it).
                        rel_str = str(out_path)
                    size = out_path.stat().st_size if out_path.exists() else None
                    # Persist. sync SQLite writes on the event loop are
                    # tolerable here (call is already ending; NET path
                    # is drained). Run in try — DB errors must not tank
                    # the stop() path.
                    try:
                        from app.db import SessionRow
                        from app.db.session import SessionLocal
                        with SessionLocal() as db:
                            row = db.query(SessionRow).filter(
                                SessionRow.id == self.session_id,
                            ).one_or_none()
                            if row is not None:
                                row.recording_path = rel_str
                                row.recording_duration_ms = dur_ms
                                if size is not None:
                                    row.recording_size_bytes = size
                                db.commit()
                    except Exception:
                        log.warning(
                            "recording metadata persist failed for %s",
                            self.call_id, exc_info=True,
                        )
            except Exception:
                log.warning("recorder finalize errored for %s", self.call_id, exc_info=True)

        await get_registry().stop(self.call_id, self.tenant_id, reason=reason)

        # 2026-08-25: proactively tell Twilio to end the call.
        #
        # Previously, `stop()` only closed OUR side (WSS + brain + session).
        # Twilio's side of the call could remain "in-progress" for up to
        # 30s until Twilio noticed our WSS was gone.  User's friend
        # reported "it doesn't hang up" — that's exactly this: the caller
        # heard silence for many seconds after our goodbye instead of the
        # line dropping cleanly.
        #
        # Fires on ALL stop() paths that would want Twilio to hang up:
        # farewell, idle_timeout, escalate (yes — escalate hands off then
        # ends the leg), hangup.  Explicit skip list: reasons that mean
        # "the caller already dropped" (`caller_hangup`, `ws_closed`,
        # `browser_disconnect`) — Twilio's line is already dead, calling
        # the REST API would just log a 404.
        #
        # Failure policy: best-effort.  Twilio auth error / network drop
        # never blocks or crashes `stop()`.  Old behavior (rely on WSS
        # closure) is our fallback.
        _twilio_kill_skip = {
            "caller_hangup", "ws_closed", "browser_disconnect",
        }
        if reason not in _twilio_kill_skip:
            try:
                await _end_twilio_call(self.call_id, reason=reason)
            except Exception as _e:
                log.warning(
                    "twilio_end_call best-effort failed call=%s: %s",
                    self.call_id, _e,
                )

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

    async def _loop_lag_watchdog(self) -> None:
        """2026-08-13 (M1 task #343): event-loop lag watchdog.

        Sleeps 20ms in a loop.  If the actual sleep drifted longer than
        `threshold_ms` past 20ms, we log a WARN — that means some
        coroutine ran synchronously (or awaited on blocking code) long
        enough to stall the event loop, which would ALSO stall the
        Twilio receive loop that consumes inbound caller media.

        Cheap: ~50 wakes per second, one time.perf_counter() per wake.
        Cancelled on stop().
        """
        import time as _t
        threshold_ms = 20.0
        interval = 0.02
        expected = _t.perf_counter()
        try:
            while True:
                await asyncio.sleep(interval)
                now = _t.perf_counter()
                lag_ms = (now - expected - interval) * 1000.0
                if lag_ms > threshold_ms:
                    log.warning(
                        "EVENT_LOOP_LAG call=%s lag_ms=%.1f",
                        self.call_id, lag_ms,
                    )
                expected = now
        except asyncio.CancelledError:
            return

    def _stamp_turn_committed(self, transcript: str, turn_gen: int) -> None:
        """R5: mark a caller turn as dispatched to brain.  Watchdog will
        fire TURN_STALLED if no response signal within 3s."""
        import time as _t
        self._committed_turn_at = _t.monotonic()
        self._committed_turn_transcript = transcript[:120]
        self._committed_turn_gen = turn_gen
        self._turn_stalled_logged = False

    def _clear_turn_committed(self, reason: str = "response") -> None:
        """R5: response signal fired — clear the stall timer."""
        if self._committed_turn_at is not None:
            self._committed_turn_at = None
            self._committed_turn_transcript = ""
            self._committed_turn_gen = -1
            self._turn_stalled_logged = False

    async def _turn_stall_watchdog(self) -> None:
        """2026-08-13 (R5 P0): scream when a committed caller turn goes
        3+ seconds with no response signal.

        A "response signal" that clears the stall is any of:
          - TTS_SENTENCE_QUEUED  (streaming brain fired)
          - TTS_STREAM_START     (any TTS path fired)
          - CONV_CONTROL_FASTPATH_HIT / RESPONSE_CACHE_STREAM_HIT
          - state transition to SPEAKING
          - tool started (via brain path)
          - explicit clarification queued
        Each of the above calls _clear_turn_committed().

        Would have diagnosed Hamzah's zombie SPEAKING + fake-wait
        combo instantly.  Emits TURN_STALLED with:
          - actor.state
          - actor.turn_generation, actor.speech_generation
          - committed transcript + gen
          - live speech task presence
          - seconds since commit
        Logs at ERROR level so it's grep-obvious.
        """
        import time as _t
        stall_threshold_s = 3.0
        interval_s = 0.5
        try:
            while True:
                await asyncio.sleep(interval_s)
                actor = self.actor
                if actor is None:
                    continue
                committed_at = self._committed_turn_at
                if committed_at is None:
                    continue
                if self._turn_stalled_logged:
                    continue  # already screamed for this turn; don't spam
                idle_s = _t.monotonic() - committed_at
                if idle_s < stall_threshold_s:
                    continue
                speech_task = getattr(actor, "_current_speech_task", None)
                live_speech = speech_task is not None and not speech_task.done()
                log.error(
                    "TURN_STALLED call=%s idle_s=%.1f state=%s "
                    "turn_gen=%d speech_gen=%d live_speech_task=%s "
                    "committed_gen=%d committed_transcript=%r",
                    self.call_id, idle_s,
                    getattr(actor.state, "value", str(actor.state)),
                    actor.turn_generation,
                    actor.speech_generation,
                    live_speech,
                    self._committed_turn_gen,
                    self._committed_turn_transcript,
                )
                self._turn_stalled_logged = True
        except asyncio.CancelledError:
            return

    async def _speaking_watchdog(self) -> None:
        """2026-08-13 (R1 P0): zombie-SPEAKING self-heal.

        Runs alongside the loop-lag watchdog for the whole call.
        Every 500ms it checks the actor.  If the state has been
        SPEAKING with NO wire activity (`_last_wire_send_at`) for
        >= 3 seconds, forcibly transitions to LISTENING and arms
        idle-followup.  This is BELT-AND-SUSPENDERS behind the pump's
        try/finally epilogue — if a future code path introduces a new
        way to reach SPEAKING and forgets to unwind, this catches it
        instead of the caller hearing dead air.

        Killed Hamzah's call on 2026-08-13 (SPEAKING at 19:06:19,
        stuck until 19:07:00 when caller said "Are you still there?"
        which TurnManager mis-flagged as INTERRUPTION-during-speech).

        2026-08-18 (P0 false-kill fix): the previous version compared
        `now - _last_wire_send_at` where `_last_wire_send_at` is a
        GLOBAL last-send timestamp (any generation, any state).  On the
        very next SPEAKING transition after any idle gap (e.g. greeting
        ends → 10s of caller thinking → new turn starts SPEAKING), the
        watchdog immediately saw idle_s >= 3s and killed the fresh
        reply — 137ms into legitimate TTS.  Once the state flipped to
        LISTENING mid-reply, downstream code fired a SECOND TTS on the
        same gen and the two streams' packets interleaved on the
        WebSocket → scrambled audio (call CAbbfbb5f0ee06c0e57a2ae647387c4ea3).

        Fix: track when we entered SPEAKING, and only declare zombie if
        BOTH (a) we've been in SPEAKING for >= threshold AND (b) no
        wire send happened AFTER we entered SPEAKING.  This lets a
        legitimate reply that hasn't produced its first byte yet
        survive TTS provider latency, and prevents cross-generation
        timestamps from killing fresh replies.
        """
        import time as _t
        # Threshold: 3s of SPEAKING with no wire send = zombie.
        # A real reply of any length keeps `_last_wire_send_at` fresh
        # every ~20ms (Twilio frame cadence).  3s is a huge margin.
        zombie_threshold_s = 3.0
        interval_s = 0.5
        # Track when we most recently entered SPEAKING.  Local to the
        # watchdog so we don't have to plumb every SPEAKING transition
        # site — polled state is fine at 500ms.
        speaking_entered_at: Optional[float] = None
        try:
            while True:
                await asyncio.sleep(interval_s)
                actor = self.actor
                if actor is None:
                    speaking_entered_at = None
                    continue
                if actor.state != CallState.SPEAKING:
                    # Reset entry timestamp on any non-SPEAKING poll so
                    # the next SPEAKING transition starts a fresh clock.
                    speaking_entered_at = None
                    continue
                now = _t.monotonic()
                # First poll to see SPEAKING → stamp entry time.
                if speaking_entered_at is None:
                    speaking_entered_at = now
                    # Also mirror onto the instance so debug tooling
                    # can see when we entered.  (Was declared in
                    # __init__ but never assigned.)
                    self._speaking_entered_at = now
                # Zombie predicate:
                #   (a) been in SPEAKING at least `zombie_threshold_s`
                #   (b) last successful media send happened BEFORE we
                #       entered SPEAKING (i.e. no fresh audio this
                #       generation of speech)
                in_speaking_for = now - speaking_entered_at
                last_send = self._last_wire_send_at or 0.0
                stale_wire = last_send < speaking_entered_at
                if in_speaking_for >= zombie_threshold_s and stale_wire:
                    log.warning(
                        "ZOMBIE_SPEAKING call=%s in_speaking=%.1fs last_send_pre_entry=%s "
                        "— forcing SPEAKING→LISTENING",
                        self.call_id, in_speaking_for, stale_wire,
                    )
                    actor.transition(CallState.LISTENING)
                    self._arm_idle_followup()
                    speaking_entered_at = None
        except asyncio.CancelledError:
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

        # 2026-08-31 CALL-BUG-09: signal the greeting task that the
        # audio pipe is up so it can start speaking. Idempotent — Event
        # stays set for the rest of the call.
        if not self._caller_media_arrived.is_set():
            self._caller_media_arrived.set()

        # 2026-08-31 task #104-followup: tee inbound frame into the
        # recorder before we do anything else with it. sync + fast (< 1µs
        # bytearray extend); never raises (recorder swallows internally).
        if self.recorder is not None:
            self.recorder.append_caller(mulaw_frame)

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
        # 2026-08-13 (M1 task #343): FIRST40 marks tell us true wire-to-
        # ear latency for THIS reply's first audible byte.
        # ack_wall - send_wall = time from "we shipped the first 40ms"
        # to "Twilio confirmed caller has heard it."  Playing the 40ms
        # itself is 40ms; the remainder is uplink + Twilio playout jitter.
        if mark_id.startswith("FIRST40_"):
            send_wall = self._first40_send_wall.pop(mark_id, None)
            if send_wall is not None:
                ack_wall = time.monotonic()
                wire_to_ear_ms = int((ack_wall - send_wall) * 1000)
                log.info(
                    "TWILIO_FIRST40_ACK call=%s mark=%s send_to_ack_ms=%d "
                    "(includes 40ms of actual playback)",
                    self.call_id, mark_id, wire_to_ear_ms,
                )
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
        from packages.voice import classify_barge, BargeAction
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
        # 2026-08-30 (task #141): emit typed BargeInDetectedEvent.
        # Map BargeAction → the event's kind field.  IGNORE →
        # 'false_positive' (empty/noise did not signal anything);
        # CONTINUE → 'backchannel' (mhm/yeah signal); INTERRUPT →
        # 'real'.  Word count from classify_barge's normalizer.
        try:
            from packages.observability.humanness_events import (
                BargeInDetectedEvent as _BIE,
                emit_humanness_event as _emit_bie,
            )
            if action == BargeAction.IGNORE:
                _bkind = "false_positive"
            elif action == BargeAction.CONTINUE:
                _bkind = "backchannel"
            else:
                _bkind = "real"
            _wc = len(text.split())
            _emit_bie(_BIE(
                call_id=self.call_id or "?",
                tenant_id=self.tenant_id or "default",
                session_id=self.session_id or "?",
                kind=_bkind,
                word_count=_wc,
            ))
        except Exception:
            pass

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

    async def _try_openai_ws_turn(
        self,
        transcript: str,
        on_delta,
        buf,
    ) -> Optional[dict]:
        """2026-08-13 (N1 task #344): try the persistent OpenAI WS.

        Returns a payload dict (same shape as brain.handle_user_turn)
        on success — caller uses it directly and skips the HTTP router.
        Returns None on any of:
          - WS disabled / not open / warmup not done yet
          - WS raised any error mid-turn
          - WS returned tool_calls (we don't run the tool loop over WS
            yet; hand it back to the brain's HTTP tool-loop for safety)

        Buf is the same SentenceBuffer the HTTP path uses — deltas from
        WS feed into on_delta which pushes into buf/queue, so the TTS
        pipeline downstream is unchanged.
        """
        if not settings.openai_persistent_ws_enabled:
            return None
        if self._openai_ws is None or not self._openai_ws.is_open():
            return None
        # If warmup is still in flight the socket exists but state
        # prep hasn't finished — using it here still works, we just
        # miss the prewarm benefit.  Fine to proceed.
        try:
            reply_text, tool_calls = await self._openai_ws.stream_reply(
                user_text=transcript, on_delta=on_delta,
            )
        except Exception:
            log.exception(
                "OPENAI_PERSISTENT_WS_TURN_FAIL call=%s — falling back to HTTP",
                self.call_id,
            )
            # 2026-08-13 (verified via live probe): validation errors
            # (type="error") leave the socket LOOKING open but the next
            # send hangs then closes.  Safer to tear down + retire the
            # WS for the rest of this call than to try to reuse it.
            if self._openai_ws is not None:
                try:
                    await self._openai_ws.close()
                except Exception:
                    pass
                self._openai_ws = None
            return None
        if tool_calls:
            log.info(
                "OPENAI_PERSISTENT_WS_TOOL_CALLS call=%s n=%d — deferring to HTTP tool loop",
                self.call_id, len(tool_calls),
            )
            # Discard the tokens we streamed (they were partial pre-tool)
            # and let the brain's HTTP path re-run this turn with the
            # full tool loop.
            buf._buf = ""  # type: ignore[attr-defined]
            buf._full = ""  # type: ignore[attr-defined]
            buf._first_emitted = False  # type: ignore[attr-defined]
            # Also clear any sentences already queued for the pumper.
            # The safest way is to signal cancel + return None so the
            # HTTP path runs its own on_delta from scratch.
            return None
        log.info(
            "OPENAI_PERSISTENT_WS_TURN_OK call=%s chars=%d",
            self.call_id, len(reply_text),
        )
        # Shape matches what brain.handle_user_turn returns for a no-
        # tool reply so the downstream code (fake-booking guard check,
        # response-cache put, planned-vs-streamed diff) all still work.
        return {
            "reply": reply_text,
            "tool_results": [],
            "escalated": False,
        }

    def _should_dedupe_dispatch(self, transcript: str) -> bool:
        """2026-08-13: transcript-superset dedupe.

        Deepgram sometimes commits speech_final=True on a mid-sentence
        fragment (150ms endpointing is aggressive), then commits the
        full sentence 500-2000ms later.  Without this gate the brain
        fires twice — once for the fragment, once for the full — and
        the caller hears two replies.

        Rules:
          1. If the new transcript IS the last dispatched one (bytes-
             equal after trim) — always dedupe.  Same turn re-committed.
          2. If the new transcript is a STRICT SUPERSET of the last
             dispatched one AND the prior brain hasn't spoken audio yet
             — do NOT dedupe.  Caller can bump_turn/cancel the prior
             and re-fire with the fuller transcript (normal path).
          3. If the new transcript is a strict superset AND the prior
             brain has already spoken — dedupe.  Otherwise we'd stack
             two replies.  The full sentence just becomes the caller's
             next turn (a natural follow-up).
          4. If prior transcript is a prefix of the new one — same as
             (2)/(3): superset semantics.
          5. Otherwise (different content) — dispatch normally.

        Only applies within a 4-second window of the last dispatch; older
        entries can't be "the same turn re-fragmented."
        """
        import time
        now = time.monotonic()
        prev = (self._inflight_dispatch_transcript or "").strip()
        gap = now - (self._inflight_dispatch_monotonic or 0.0)
        if not prev or gap > 4.0:
            return False
        cur = (transcript or "").strip()
        if not cur:
            return False
        # Case-insensitive comparison so "hello" and "Hello" collapse.
        prev_l = prev.lower()
        cur_l = cur.lower()
        if prev_l == cur_l:
            log.info("DEDUPE_DISPATCH call=%s reason=exact-match gap=%.2fs input=%r",
                     self.call_id, gap, cur[:80])
            return True
        # Superset check both directions.
        if cur_l.startswith(prev_l) or prev_l.startswith(cur_l):
            if self._inflight_has_spoken:
                log.info("DEDUPE_DISPATCH call=%s reason=superset-after-speak gap=%.2fs prev=%r cur=%r",
                         self.call_id, gap, prev[:60], cur[:60])
                return True
            # Prior brain still thinking; let it be cancelled + re-fired.
            log.info("DEDUPE_DISPATCH_ALLOW call=%s reason=superset-prespeak gap=%.2fs prev=%r cur=%r",
                     self.call_id, gap, prev[:60], cur[:60])
            return False
        return False

    def _mark_dispatched(self, transcript: str) -> None:
        """Record that we're firing brain for this transcript now."""
        import time
        self._inflight_dispatch_transcript = transcript
        self._inflight_dispatch_monotonic = time.monotonic()
        self._inflight_has_spoken = False

    # ── task #369: one-gen-one-commit invariant ─────────────────────
    #
    # Speculative revisions may live INSIDE the actor (draft A → draft
    # B → commit) but only one can cross the speech-commit boundary.
    # These helpers make that invariant explicit at every dispatch
    # site (speculative brain, real END_OF_TURN, fastpaths).

    def _try_claim_response_commit(self, gen: int, reason: str) -> bool:
        """Atomically claim the response commit slot for `gen`.
        Returns True if this caller owns the commit; False if the slot
        is already claimed (caller MUST skip its dispatch).

        Single-threaded asyncio: check + set is atomic without a lock.
        """
        if gen in self._committed_response_gens:
            log.info(
                "COMMIT_LOCK_SKIP call=%s gen=%d reason=%s "
                "(slot already claimed)",
                self.call_id, gen, reason,
            )
            return False
        self._committed_response_gens.add(gen)
        # Bookkeeping for future replacement semantics.
        self._response_revision_counter[gen] = (
            self._response_revision_counter.get(gen, 0) + 1
        )
        log.info(
            "COMMIT_LOCK_CLAIM call=%s gen=%d reason=%s revision=%d",
            self.call_id, gen, reason,
            self._response_revision_counter[gen],
        )
        return True

    def _clear_response_commits_before(self, keep_gen: int) -> None:
        """Drop commit-lock entries for stale generations.  Called
        after bump_turn so the new gen starts with a clean slot.
        Keeps memory bounded (long calls otherwise accumulate entries)."""
        stale = {g for g in self._committed_response_gens if g < keep_gen}
        if stale:
            self._committed_response_gens -= stale
            for g in stale:
                self._response_revision_counter.pop(g, None)

    def _matches_conversation_control_intent(self, transcript: str) -> bool:
        """Cheap synchronous predicate — does the transcript match a
        canonical conversation-control intent?  Used by the speculative
        EAGER_END_OF_TURN handler to divert BEFORE claiming the commit
        lock, so the fastpath can win even when speculative would
        otherwise fire an LLM."""
        # 2026-08-20: response_cache_bypass forces every turn to the LLM
        # so we can actually test humanness of the prompt.
        if settings.response_cache_bypass:
            return False
        try:
            from packages.voice import match_conversation_control_intent
            return match_conversation_control_intent(transcript) is not None
        except Exception:
            log.exception("conv-control intent match failed")
            return False

    async def _speak_conversation_control_fastpath(
        self,
        transcript: str,
        turn_gen: int,
    ) -> None:
        """Speculative-safe fastpath speak.  Assumes the caller has
        already claimed the commit lock for `turn_gen` with reason
        'conv_control_fastpath'.  Runs the same match+speak flow as
        `_try_conversation_control_fastpath` but without re-claiming
        the lock.

        2026-08-17 (triple-speak fix): the fastpath spawns on EAGER but
        _commit_final_transcript runs later on END_OF_TURN and sets
        _last_committed_transcript = text, which arms the continuation-
        merge window.  If the caller says a fresh sentence within 6s
        (CONTINUATION_MERGE_MAX_S), the merge stitches the fastpath's
        original text with the new one and re-dispatches — the caller
        then hears fastpath reply + filler + LLM reply for the merged
        text.  We flip a flag here so _commit_final_transcript knows
        NOT to arm the merge anchor, and mark the dispatch through
        the normal transcript-dedupe path."""
        try:
            from packages.voice import match_conversation_control_intent
            reply = match_conversation_control_intent(transcript)
            if reply is None:
                # Should never happen — caller just gated on the match.
                log.warning(
                    "conv_control_fastpath spawned but matcher now returns None "
                    "call=%s gen=%d text=%r",
                    self.call_id, turn_gen, transcript[:80],
                )
                return
            log.info(
                "CONV_CONTROL_FASTPATH_HIT call=%s input=%r → reply=%r",
                self.call_id, transcript[:80], reply[:80],
            )
            # Register the transcript with the dedupe machinery so any
            # superset that arrives within the 4s window collapses.
            self._mark_dispatched(transcript)
            # Suppress continuation-merge anchoring for this turn.
            # _commit_final_transcript reads this flag before setting
            # _last_committed_transcript.
            self._suppress_next_continuation_anchor = True
            self._clear_turn_committed("conv_control_fastpath")
            await self._speak(reply)
            # Flag downstream dedupe: we've spoken, so any superset of
            # `transcript` arriving now should be dropped, not stacked.
            self._inflight_has_spoken = True
            # 2026-08-20 (perf bug fix): release the commit-lock slot as
            # soon as the fastpath audio finishes.  Without this, the
            # SAME turn_generation stays "claimed" until bump_turn on
            # the NEXT caller END_OF_TURN — which means the next
            # speculative dispatch (from the caller's next question)
            # fails _try_claim_response_commit and logs COMMIT_LOCK_SKIP.
            # Observed on CA5668fd (2026-08-20): 808ms of stalemate
            # between DG's speech_final and RESPONSE_CACHE HIT because
            # gen=0 was still stuck in the set from turn 0's fastpath.
            self._committed_response_gens.discard(turn_gen)
            log.info(
                "COMMIT_LOCK_RELEASE call=%s gen=%d reason=conv_control_fastpath_done",
                self.call_id, turn_gen,
            )
        except Exception:
            log.exception("conversation-control fastpath speak failed")

    async def _try_conversation_control_fastpath(
        self,
        transcript: str,
        turn_gen: int,
    ) -> bool:
        """2026-08-13 (A1 patch): conversation-control fastpath.

        Deterministic caller intents ("hello?", "can you hear me?",
        "are you there?") have canonical replies the LLM adds nothing to.
        This bypass runs BEFORE the response cache (which strips 'hi'/
        'hello' as fillers and misses these) and BEFORE any LLM dispatch.

        Warmed at boot into the TTS disk cache, so hits are ~2ms disk +
        wire time.  Turn-1 sub-500ms even on the very first call.

        Returns True if handled.  False → fall through to response cache /
        LLM path unchanged.
        """
        # 2026-08-23: conv-control fastpath is a DETERMINISTIC intent
        # matcher (packages/voice/conversation_control.py:_INTENT_MAP),
        # NOT a learned cache. Previously coupled to
        # response_cache_bypass which caused turn 1 "Hello. Can you
        # hear me?" to hit OpenAI at 1.8s instead of ~50ms disk cache.
        # ChatGPT audit called this out.
        #
        # 2026-08-23 (user requested): while raw-LLM speed testing is
        # active (response_cache_bypass=true), ALSO skip conv-control
        # so caller experiences the true LLM latency floor. This keeps
        # measurement honest — if the fastpath fires on "hear me", we
        # can't see how slow the underlying LLM path is on similar
        # inputs. When speed testing ends and response_cache_bypass
        # goes back to false, the conv-control fastpath re-activates.
        # For production (bypass=false), conv-control still fires —
        # it's a real win, not a measurement artifact.
        if settings.response_cache_bypass:
            log.info(
                "CACHE_BYPASS_ACTIVE call=%s conv_control_fastpath skipped "
                "(raw-LLM measurement mode)",
                self.call_id,
            )
            return False
        try:
            from packages.voice import match_conversation_control_intent
            reply = match_conversation_control_intent(transcript)
            if reply is None:
                return False
            log.info(
                "CONV_CONTROL_FASTPATH_HIT call=%s input=%r → reply=%r",
                self.call_id, transcript[:80], reply[:80],
            )
            self._clear_turn_committed("conv_control_fastpath")
            await self._speak(reply)
            return True
        except Exception:
            log.exception("conversation-control fastpath failed")
            return False

    async def _try_response_cache_fastpath(
        self,
        state,
        transcript: str,
        turn_gen: int,
    ) -> bool:
        """2026-08-13 (ChatGPT audit): response-cache fastpath.

        Common turns ("Hello can you hear me", "what are your hours",
        "where are you located") are pre-seeded in the response cache
        keyed by (business_id, tenant_id, normalized_input).  A hit
        skips the ~2.6s LLM call entirely and goes straight to _speak(),
        which itself hits the TTS cache for a ~<200ms end-to-end reply.

        Returns True if the turn was served from cache and NO further
        brain dispatch is needed.  False otherwise (fall through to LLM).
        """
        if settings.response_cache_bypass:
            log.info("CACHE_BYPASS_ACTIVE call=%s response_cache_fastpath skipped", self.call_id)
            return False
        try:
            from packages.response_cache import get_shared_response_cache

            business_id = (
                getattr(getattr(state, "business", None), "id", None)
                or getattr(state, "business_id", None)
                or "unknown"
            )
            # 2026-08-24 CAff590033 fix: was sync .get() blocking the
            # event loop for 12+ seconds. Use aget() to run in thread pool.
            hit = await get_shared_response_cache().aget(
                business_id,
                self.tenant_id,
                transcript,
            )
            if hit is None:
                return False

            log.info(
                "RESPONSE_CACHE_STREAM_HIT call=%s biz=%s input=%r → reply=%r",
                self.call_id, business_id,
                transcript[:80], hit.reply_text[:80],
            )
            self._clear_turn_committed("response_cache_fastpath")
            await self._speak(hit.reply_text)
            return True
        except Exception:
            log.exception("streaming response-cache lookup failed")
            return False

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
        Runs as a background task spawned from _run_brain_streaming.

        2026-08-12 fix: on the FIRST sentence, transition actor state
        listening → speaking + start_generation on the ledger so barge-in
        and generation tracking work.  _speak() does this in the non-
        streaming path; we have to replicate here.  Missing this was the
        2.4-second gap between TTS_FIRST_BYTE and 'listening → speaking'
        seen on turn 1 of trace CA9a02ad.

        2026-08-13 (R1 P0 — zombie-SPEAKING fix): this pump used to
        transition LISTENING→SPEAKING on the first sentence and then
        never transition back.  If the caller waited politely instead
        of interrupting, the actor sat in SPEAKING forever, TurnManager
        treated the next final as INTERRUPTION-during-speech, idle-
        followup silently no-op'd (it early-returns when state !=
        LISTENING), and the call went dead.  This killed the Hamzah
        call (2026-08-13 19:06:22 → 19:07:00, 34s of dead air).
        Fix: try/finally epilogue mirrors _speak()'s lifecycle exactly:
        SPEAKING → LISTENING + arm idle-followup, but ONLY if WE were
        the ones who transitioned to SPEAKING (tracked via
        `we_transitioned`).  A downstream branch in
        _run_brain_streaming may call _speak(planned) for a fake-
        booking replacement; that path handles its own lifecycle.
        """
        from app.routes.twilio import _get_telephony_tts
        from packages.core_agent.speech_sanitizer import sanitize_for_speech
        tts = _get_telephony_tts()
        span = self._current_turn_span
        first = True
        # R1: track whether THIS pump owns the current SPEAKING transition,
        # so the epilogue only unwinds transitions we actually made.
        we_transitioned = False
        # 2026-08-21: accumulate what we actually spoke, so the finally
        # block can run farewell detection on the FULL reply. Previously
        # _maybe_hangup_after_farewell was only called from _speak() —
        # streaming turns silently skipped farewell detection entirely.
        # Verified on CA5a1ce466: LLM ended with "See you then!" but no
        # FAREWELL_DETECTED line fired because the streaming pump was
        # the speaker, not _speak().
        _pump_spoken_text = []
        try:
            while True:
                sentence = await queue.get()
                if sentence is None:
                    break
                cur_gen = self.actor.speech_generation if self.actor is not None else gen
                if gen != cur_gen:
                    log.info(
                        "TTS_SENTENCE_DROPPED_STALE call=%s stale_gen=%d cur_gen=%d",
                        self.call_id, gen, cur_gen,
                    )
                    continue
                try:
                    # 2026-08-23 CRITICAL: defensive drop for leaked
                    # tool-call JSON. On CAd26f39ef806f07dee8ffc49069ff699b
                    # (turn 7, "Yeah. Sure." → confirm_action), the LLM
                    # emitted `{"name": "emit_semantic_plan", "parameters":
                    # {"operation": "confirm_action", ...}}` as CONTENT
                    # instead of a proper tool_calls structure. The
                    # sanitizer's bracket regex \{[^{}]*\} does not match
                    # nested JSON, so the payload passed through and TTS
                    # started speaking it aloud ("semantic plan confirm
                    # actions facts 330 pm"). Drop at pump entry so no
                    # future model regression can leak tool JSON to the
                    # caller. Log LEAKED_TOOL_JSON loud so we notice.
                    _stripped = sentence.lstrip()
                    if _stripped.startswith(("{", "[")) and (
                        '"name"' in _stripped
                        or '"parameters"' in _stripped
                        or '"function"' in _stripped
                        or '"tool_calls"' in _stripped
                    ):
                        log.warning(
                            "LEAKED_TOOL_JSON call=%s gen=%d dropped=%r",
                            self.call_id, gen, sentence[:120],
                        )
                        continue
                    clean = sanitize_for_speech(sentence)
                    if not clean.strip():
                        continue
                    if first and self.actor is not None:
                        # Mark speaking + open a new speech generation.
                        self.actor.transition(CallState.SPEAKING)
                        we_transitioned = True
                        # 2026-08-13 (double-brain fix): see _speak() note.
                        self._inflight_has_spoken = True
                        # R5: streaming pump entering SPEAKING clears stall.
                        self._clear_turn_committed("pump_start")
                        speech_gen = self.actor.speech_generation
                        self.actor.ledger.start_generation(speech_gen, clean)
                    log.info(
                        "TTS_SENTENCE_QUEUED call=%s gen=%d first=%s text=%r",
                        self.call_id, gen, first, clean[:80],
                    )
                    _pump_spoken_text.append(clean)
                    await self._stream_tts_incremental(tts, clean, gen, span if first else None)
                    first = False
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.exception("TTS_SENTENCE_FAILED: %s", e)
        finally:
            # R1 epilogue — mirror _speak()'s lifecycle.  Runs on
            # normal completion, cancellation, or unexpected error.
            actor = self.actor
            if actor is not None and we_transitioned:
                if actor.state == CallState.SPEAKING:
                    actor.transition(CallState.LISTENING)
                # Arm the idle-followup ladder so a silent caller gets
                # nudged/hung-up eventually.  Idempotent — cancels any
                # prior task.
                # 2026-08-31 CALL-BUG-10: check farewell BEFORE arming
                # idle-followup. If the reply contains a farewell
                # pattern, do NOT arm idle — user reported "anything
                # else" firing 10s AFTER a booking confirmation on
                # CA087a09 because the streaming path armed idle first,
                # and even though _maybe_hangup_after_farewell ran, its
                # 2.5s hangup timer collided with the 10s idle prompt
                # and something (TTS truncation) suppressed the hangup.
                # Order matters: farewell check → arm idle only if not
                # a farewell.
                _full_reply = " ".join(_pump_spoken_text) if _pump_spoken_text else ""
                _is_farewell = False
                if _full_reply:
                    import re as _re_fw
                    _low = _full_reply.lower()
                    for _fw_pat in self._FAREWELL_PATTERNS:
                        if _re_fw.search(rf"\b{_fw_pat}\b", _low):
                            _is_farewell = True
                            break
                if not _is_farewell:
                    self._arm_idle_followup()
                log.info(
                    "PUMP_SPEECH_COMPLETED call=%s gen=%d farewell=%s — SPEAKING→LISTENING",
                    self.call_id, gen, _is_farewell,
                )
                # 2026-08-21: mirror _speak()'s farewell hook. Feed the
                # full accumulated reply so `_maybe_hangup_after_farewell`
                # runs its guards on the whole thing. Without this the
                # streaming path silently skipped farewell detection and
                # the LLM's "See you then!" never scheduled the hangup
                # (verified on CA5a1ce466).
                if _pump_spoken_text:
                    # Mirror _speak()'s rolling utterance buffer so echo
                    # suppression + structured-data-ask widening see
                    # streaming replies too (both check
                    # self._recent_agent_utterances).
                    self._recent_agent_utterances.append(_full_reply)
                    if len(self._recent_agent_utterances) > 3:
                        self._recent_agent_utterances.pop(0)
                    try:
                        self._maybe_hangup_after_farewell(_full_reply)
                    except Exception:
                        log.exception("streaming farewell hook failed")

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

            # 2026-08-13 (double-brain fix): dedupe fragment→full re-fires.
            # If Deepgram already emitted a prefix of this transcript and
            # we spoke a reply for it, drop this superset — it would just
            # stack a second reply.
            if self._should_dedupe_dispatch(transcript):
                return
            # Task #369: enforce one-gen-one-commit.  If another
            # dispatch (usually a HITted speculative) already claimed
            # this generation, we are the redundant fire — bail.  This
            # is the definitive guard against Abdullah's gen=20 same-
            # gen double dispatch; the transcript-based dedupe above
            # catches text-repeats but not fresh transcripts on the
            # same gen slot.
            if not self._try_claim_response_commit(
                turn_gen, reason="run_brain",
            ):
                return
            self._mark_dispatched(transcript)
            # R5 P0: start the stall timer.  Cleared by any downstream
            # response signal (fastpath hit, TTS start, etc.).  Watchdog
            # fires TURN_STALLED at ERROR if it stays stamped >3s.
            self._stamp_turn_committed(transcript, turn_gen)

            # 2026-08-13 (A1 patch): conversation-control fastpath FIRST.
            # Deterministic intents ("can you hear me", "hello", "are you
            # there") skip both the LLM and the response cache — canonical
            # replies are warmed into the TTS disk cache at boot.
            if await self._try_conversation_control_fastpath(
                transcript, turn_gen,
            ):
                return

            # 2026-08-13 (ChatGPT audit fix): response-cache fastpath.
            # Streaming path used to bypass the response cache entirely,
            # forcing "Hello can you hear me" through a 2.6s OpenAI call
            # every time.  Check cache BEFORE any LLM dispatch.
            if await self._try_response_cache_fastpath(
                state, transcript, turn_gen,
            ):
                return

            # Task #283: streaming LLM→TTS branch when eligible.
            if span is not None:
                span.mark("brain_start")
            if self._streaming_llm_eligible(brain):
                if span is not None:
                    span.mark("streaming_path")
                await self._run_brain_streaming(state, brain, transcript, turn_gen, span)
                return

            # LK steal #7 wire: stage slot-capture prompt (if any) on
            # state so brain sees the narrow sub-agent scope.
            self._stage_state_for_brain_dispatch(state)
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
        # 2026-08-13 REV2 (voice-breakup + double-reply fix):
        # min_first_chars=1 broke merge-tiny-openers — every "Sure!" or
        # "Yes." fired its own TTS request, and with PK→US 400-1000ms
        # per RTT the caller heard 3-4 chopped sentences with gaps.
        # min_first_chars=12 restores the merge for very short openers
        # ("Sure!" 5 chars → merged into next sentence, no separate RTT)
        # while still releasing anything ~10+ chars ("One moment." 11
        # chars → emits immediately).  20 (original) was too conservative
        # and held even reasonable openers.
        # 2026-08-22 (Lever C): 12 → 6. LLM's typical first-token cadence
        # from Karachi is ~40-50ms/token, so 12 chars ≈ 400ms of buffer
        # wait after the first sentence completes. Dropping to 6 releases
        # any legit first sentence ("Yeah, we do." 12 chars, "We're open"
        # 10 chars) immediately. Reflex openers ("Sure." 5, "Okay." 5)
        # still fall under the merge-tiny-opener path (< 6//2 = 3? no, 5
        # is >= 3 so emits standalone — but Ship 5 prompt rule bans them
        # at the LLM layer so they shouldn't be emitted in the first
        # place). Defense in depth: prompt says "no reflex opener", buffer
        # would merge if one somehow slipped through as < 3-char first
        # sentence. Realistic saving: 100-200ms on turns with legit short
        # first sentences.
        buf = SentenceBuffer(min_first_chars=6)
        queue: asyncio.Queue = asyncio.Queue()
        pumper_task = asyncio.create_task(
            self._pump_sentence_queue(queue, turn_gen),
            name=f"tts-pump-{self.call_id}-g{turn_gen}",
        )
        # 2026-08-21 NET (Ship 2): register pumper as the current speech
        # task so bump_speech/bump_turn cancel it.  On CA1f11caf57 gen=2
        # kept pumping queued sentences for 12.4s AFTER a barge-in
        # promoted the turn to gen=3, because this task was orphaned.
        # Result: caller heard gen=3's response first, then gen=2's
        # stale confirmation seconds later.  Registering here makes
        # `bump_turn` (which advances speech_generation + calls
        # _cancel_supervised_below) actually reach this task.
        _actor = self.actor
        if _actor is not None:
            _actor.register_speech_task(pumper_task)

        # SpeechCommitGate (task #368): deterministic pre-TTS gate.  All
        # streamed sentences flow through this — SAFE ones queue
        # immediately; WAIT_PROMISE / ACTION_CONFIRMATION are held until
        # the matching tool signal arrives.  Kills Abdullah's regression
        # (multi-utterance stacking when R2 rewrote a fake-wait reply
        # AFTER early sentences were already on the wire).
        async def _release_to_pump(sentence: str) -> None:
            await queue.put(sentence)

        gate = SpeechCommitGate(
            release=_release_to_pump,
            call_id=self.call_id,
            turn_gen=turn_gen,
        )

        first_delta = True
        # 2026-08-23 (defense-in-depth against gpt-5.4-nano regression on
        # CAd26f39ef): the model emitted the emit_semantic_plan tool call
        # as CONTENT instead of proper tool_calls structure. The delta
        # stream carried the raw JSON `{"name": "emit_semantic_plan",
        # "parameters": {...}}` which passed through SentenceBuffer and
        # got spoken aloud by TTS. Networking added a pump-side drop as
        # backstop; this is the earliest-possible drop point (delta
        # accumulation) so we don't even reach the pump on a leak.
        # Fires on the CUMULATIVE buffer, not per-delta, because a leak
        # can arrive as `{`, `"name"`, `: "emit`... — no single delta
        # triggers alone.
        _tool_leak_detected = [False]  # list for closure mutability

        def _looks_like_tool_json_leak(text: str) -> bool:
            """True if the accumulated stream looks like a leaked
            tool-call JSON payload instead of natural speech."""
            s = text.lstrip()
            if not s or s[0] not in "{[":
                return False
            # Signal words that appear in emit_semantic_plan / any tool
            # call structure. Any one is enough — a natural reply won't
            # start with `{` AND contain any of these.
            markers = ('"name"', '"parameters"', '"function"',
                       '"tool_calls"', '"arguments"')
            return any(m in s for m in markers)

        async def on_delta(delta: str):
            nonlocal first_delta
            if first_delta and span is not None:
                span.mark("llm_first_token")
                first_delta = False
            if _tool_leak_detected[0]:
                # Already flagged — silently drop residual deltas.
                return
            # Push first, THEN check accumulated buffer text. `full_text`
            # includes this delta.
            sentences = buf.push(delta)
            if _looks_like_tool_json_leak(buf.full_text):
                _tool_leak_detected[0] = True
                log.warning(
                    "LEAKED_TOOL_JSON_BRAIN call=%s gen=%d prefix=%r len=%d — "
                    "LLM emitted tool payload as content; dropping stream",
                    self.call_id, turn_gen,
                    buf.full_text[:80], len(buf.full_text),
                )
                # Do NOT release the accumulated sentences to the gate.
                # The stream is poisoned; the model-side tool-call retry
                # (if any) or the batch fallback in _run_brain will emit
                # the real reply. Drop and return.
                return
            for sentence in sentences:
                await gate.on_sentence(sentence)

        async def on_tool_call(tool_name: str) -> None:
            # A real tool started → any held "one moment" is honest.
            await gate.on_tool_call_started(tool_name)

        async def on_tool_receipt(tool_name: str, ok: bool) -> None:
            # A tool returned → releases held ACTION_CONFIRMATIONs if ok.
            await gate.on_tool_receipt(tool_name, ok)

        try:
            # 2026-08-13 (N1 task #344): if the persistent OpenAI WS is
            # live for this call, route the terminal LLM turn over it
            # instead of the HTTP router.  Fall back to HTTP on any
            # error or if the WS returned tool_calls (we don't run the
            # tool loop over WS yet — safer to hand tool turns to the
            # existing brain path).
            ws_payload = await self._try_openai_ws_turn(
                transcript, on_delta, buf,
            )
            if ws_payload is not None:
                payload = ws_payload
            else:
                # LK steal #7 wire.
                self._stage_state_for_brain_dispatch(state)
                payload = await session_manager.run_user_turn(
                    state, brain, transcript,
                    on_delta=on_delta,
                    on_tool_call=on_tool_call,
                    on_tool_receipt=on_tool_receipt,
                )
            self._current_speech_act = _infer_speech_act_from_payload(payload)

            # Flush residual (text after the last sentence-ender)
            residual = buf.flush()
            if residual:
                await gate.on_sentence(residual)

            # Close the gate.  Any still-held sentence (unmet wait
            # promise or unmet action confirmation) is DROPPED here —
            # a WARN log records each, and the STREAM_REPLY_REPLACED
            # path below sees a `streamed` value that omits the dropped
            # phrases, which naturally triggers the safe rewrite when
            # the assembled reply diverges from what actually got out.
            dropped_by_gate = await gate.flush()
            if dropped_by_gate:
                log.warning(
                    "SPEECH_GATE_DROPPED call=%s gen=%d count=%d stats=%s",
                    self.call_id, turn_gen, len(dropped_by_gate),
                    gate.stats.as_dict(),
                )
                # 2026-08-30 (task #141): emit typed
                # SpeechGateDroppedEvent per dropped sentence so
                # /trace timeline shows exactly what was gated + why.
                # Category tells us which safety fired (wait_promise
                # vs action_confirmation).
                try:
                    from packages.observability.humanness_events import (
                        SpeechGateDroppedEvent as _SGD,
                        emit_humanness_event as _emit_sgd,
                    )
                    for _dropped in dropped_by_gate:
                        _cat = getattr(
                            _dropped, "kind", None,
                        )
                        _cat_val = (
                            _cat.value
                            if _cat is not None and hasattr(_cat, "value")
                            else str(_cat or "safe")
                        )
                        _preview = str(
                            getattr(_dropped, "text", "")
                            or _dropped
                        )[:120]
                        _emit_sgd(_SGD(
                            call_id=self.call_id or "?",
                            tenant_id=getattr(
                                self, "tenant_id", "default",
                            ),
                            session_id=self.session_id or "?",
                            category=_cat_val,
                            sentence_preview=_preview,
                        ))
                except Exception:
                    pass

            # Signal end-of-stream to the pumper
            await queue.put(None)
            await pumper_task

            # 2026-08-13 (ChatGPT audit fix): populate response cache
            # from streaming path too, so turn N+1 with the same input
            # (or the SAME phrase on a future call) hits the fastpath.
            # Only cache turns with no tool_results (dynamic) and no
            # rewrite (safe).
            # 2026-08-22 NET Ship 2: additionally suppress cache write
            # when the turn was tagged may_be_partial by Ship 1 in the
            # interruption handler.  Poisoning happened on CAa7effd6273:
            # barge fired brain on partial "the general appointment",
            # LLM produced a price answer, cache stored (partial→price),
            # then END_OF_TURN promoted the same STT text and cache
            # replayed the price a third time.  Ship 1 tags the turn;
            # Ship 2 refuses to store the ambiguous-input result.
            try:
                planned_reply = (payload.get("reply") or "").strip()
                tool_results = payload.get("tool_results") or []
                _is_partial_turn = turn_gen in self._may_be_partial_turns
                if _is_partial_turn:
                    log.info(
                        "RESPONSE_CACHE_STREAM_SKIP call=%s gen=%d reason=may_be_partial "
                        "input=%r",
                        self.call_id, turn_gen, transcript[:60],
                    )
                    # Consume the tag so it doesn't leak into a later gen
                    # that happens to reuse the same integer.
                    self._may_be_partial_turns.discard(turn_gen)
                elif (
                    planned_reply
                    and not tool_results
                    and not payload.get("escalated")
                ):
                    from packages.response_cache import get_shared_response_cache
                    business_id = (
                        getattr(getattr(state, "business", None), "id", None)
                        or getattr(state, "business_id", None)
                        or "unknown"
                    )
                    # 2026-08-24 CAff590033 fix: async wrapper avoids
                    # 12s event-loop block from sync SQLite write.
                    key = await get_shared_response_cache().aput(
                        business_id, self.tenant_id,
                        transcript, planned_reply,
                    )
                    if key:
                        log.info(
                            "RESPONSE_CACHE_STREAM_PUT call=%s key=%s input=%r",
                            self.call_id, key[:8], transcript[:60],
                        )
            except Exception:
                log.debug("streaming response-cache put skipped", exc_info=True)

            # If the brain replaced the reply (fake-booking guard) OR
            # the gate dropped held sentences that never got a signal,
            # payload["reply"] won't match what actually reached TTS.
            # Compare against `gate.released_text` (what crossed the
            # gate) — NOT against buf.full_text (raw LLM stream) which
            # may include sentences the gate dropped.  When the gate
            # holds/drops a fake-wait, `streamed` shrinks to just the
            # safe sentences, so the divergence check correctly re-
            # speaks the R2 rewrite for the missing content.
            planned = (payload.get("reply") or "").strip()
            streamed = gate.released_text
            if not planned:
                pass  # nothing to compare against
            elif not streamed:
                # Streaming never happened (batch fallback inside brain).
                # 2026-08-23 CAf535b0dd defense-in-depth: on the batch-
                # fallback path, detect meta-description-shaped text
                # (LLM paraphrasing internal slot names like "caller
                # provided name" or "caller name and phone number" as if
                # they were spoken content).  Voice-agent's prompt fix
                # attacks the source (rewriting the slot-name references
                # in prompt.py:193); this guard prevents future
                # regressions from reaching TTS.  If matched, skip the
                # speak call and log LEAKED_META so we notice.
                _leaked_meta = _looks_like_leaked_metadescribe(planned)
                if _leaked_meta:
                    log.warning(
                        "LEAKED_META call=%s gen=%d dropped_batch_reply=%r "
                        "reason=%s",
                        self.call_id, turn_gen, planned[:120],
                        _leaked_meta,
                    )
                else:
                    log.info(
                        "STREAM_BATCH_FALLBACK call=%s gen=%d — speaking batch reply",
                        self.call_id, turn_gen,
                    )
                    await self._speak(planned)
            else:
                # Normalize both sides through the sanitizer so em-dash /
                # comma / whitespace / abbreviation-expansion noise doesn't
                # trigger a false replacement.  Only re-speak when the
                # canonical text actually diverged (fake-booking guard
                # rewrite or similar policy step).
                from packages.core_agent.speech_sanitizer import sanitize_for_speech
                _norm = lambda s: " ".join(sanitize_for_speech(s).lower().split())
                _n_stream = _norm(streamed)
                _n_plan = _norm(planned)
                # 2026-08-21: contains-check instead of strict equality.
                # Regression on CA5a1ce466 gen=2 (booking tool call):
                #   streamed = preamble("Let me check... One moment...") + final("I've got openings...")
                #   planned  = final only ("I've got openings...")
                # Old code: strict != → treat as divergence → send twilio_clear +
                # re-speak planned → caller heard the slot list TWICE.
                # New rule: if the caller already heard `planned` as a
                # substring of `streamed`, the reply is fully delivered —
                # do NOT re-speak. Only re-speak when `planned` contains
                # meaningful content NOT in `streamed` (real fake-booking
                # rewrite, or gate dropped tail sentences).
                if _n_stream == _n_plan:
                    log.debug(
                        "STREAM_REPLY_MATCH_EXACT call=%s gen=%d",
                        self.call_id, turn_gen,
                    )
                elif _n_plan and _n_plan in _n_stream:
                    # Caller heard everything planned (plus tool-call
                    # preamble). No divergence — do NOT re-speak.
                    log.info(
                        "STREAM_REPLY_PLANNED_SUBSUMED call=%s gen=%d "
                        "streamed_len=%d planned_len=%d",
                        self.call_id, turn_gen, len(_n_stream), len(_n_plan),
                    )
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
                    # LK steal #7 wire.
                    self._stage_state_for_brain_dispatch(state)
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
        task so successive agent turns reset the clock.

        2026-08-21 NET: when armed from inside the idle loop's own
        _speak() finally hook (path: idle loop → _speak(_IDLE_PROMPT)
        → _speak's finally → _arm_idle_followup), DO NOT reset
        `_idle_prompted` — that reset was the root cause of the
        "Anything else?" double-fire ladder. External arms (caller
        turn done → agent reply done → arm) still reset.
        """
        self._cancel_idle_followup()
        if not self._arming_from_idle_loop:
            self._idle_prompted = False
        self._idle_task = asyncio.create_task(
            self._idle_followup_loop(),
            name=f"idle-{self.call_id}",
        )

    # 2026-08-18: farewell → hangup detection.  Patterns are matched
    # against agent-spoken text; if any hits, we schedule a stop() a
    # few seconds after the TTS finishes streaming so the caller
    # actually hears the goodbye before the line drops.
    _FAREWELL_PATTERNS: tuple = (
        "have a great day",
        "have a nice day",
        "have a good day",
        "have a wonderful day",
        "have a lovely day",
        "have a great one",
        "take care",
        "talk to you (soon|later)",
        "bye (now|for now)?",
        "goodbye",
        "see you (then|tomorrow|soon|there|monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
        "we'?ll see you",
    )
    # 2026-08-25: shortened 4.0s → 2.5s.  We now proactively terminate
    # the Twilio call leg via REST after farewell (see _end_twilio_call
    # above) so the caller doesn't need to wait for Twilio to notice the
    # WSS closed.  2.5s buys just enough grace for TTS tail audio to
    # finish playing on the caller's phone before we drop the line.
    _FAREWELL_HANGUP_DELAY_S: float = 2.5

    def _maybe_hangup_after_farewell(self, text: str) -> None:
        """If the just-spoken text contains a farewell, schedule a
        graceful stop() so the caller isn't left listening to dead air
        after the goodbye.

        2026-08-19 (post-US-caller-review): the streaming path calls
        _speak() per SENTENCE, so a farewell inside the middle of a
        longer reply used to fire the hangup schedule prematurely (see
        CA0aee80... 07:00:34 — matched inside a sentence, LLM kept
        speaking).  Two guards now:

          1. Only match if the text looks like a CLOSING sentence
             (short-ish, ends with terminal punctuation `.!?`).  A
             mid-reply fragment ending on `"give us "` won't match.

          2. If a NEW _speak() fires before the delay elapses, cancel
             the pending hangup and re-evaluate.  Subsequent sentences
             of the same reply reset the clock — hangup only fires
             after the LLM actually goes quiet for `_FAREWELL_HANGUP_DELAY_S`.
        """
        if not text:
            return
        import re as _re
        stripped = text.strip()
        # Guard #1: closing-sentence shape.  Skip mid-reply fragments.
        # Must end with terminal punctuation AND the farewell phrase
        # must appear in the LAST sentence (so a farewell that's not
        # actually the goodbye — e.g. "See you tomorrow works, would
        # you like me to book that?" — doesn't fire early hangup).
        # 2026-08-19: dropped the 90-char total cap that was breaking
        # booking-confirmation goodbyes like "You're all set, Abbas!
        # I've got you booked for a new patient exam with X-rays
        # tomorrow at two thirty. See you then!" (115 chars).
        if not stripped or stripped[-1] not in ".!?":
            return
        # Take just the last sentence for pattern matching.
        sentences = _re.split(r"(?<=[.!?])\s+", stripped)
        last_sentence = sentences[-1] if sentences else stripped
        low = last_sentence.lower()
        matched = None
        for pat in self._FAREWELL_PATTERNS:
            if _re.search(rf"\b{pat}\b", low):
                matched = pat
                break
        if matched is None:
            return
        # Guard #2: cancel any previously-scheduled hangup — a fresh
        # farewell means the LLM is still talking, restart the clock.
        existing = getattr(self, "_farewell_hangup_task", None)
        if existing is not None and not existing.done():
            existing.cancel()
        # 2026-08-23 CAf535b0dd fix: reset the "caller spoke since
        # farewell" flag so the abort check below actually reflects
        # whether the caller resumed, not whether the idle timer
        # exists (it always does after _speak).
        self._caller_spoke_since_farewell = False
        log.info(
            "FAREWELL_DETECTED call=%s pattern=%r text=%r — scheduling hangup in %.1fs",
            self.call_id, matched, text[:80], self._FAREWELL_HANGUP_DELAY_S,
        )

        async def _delayed_stop() -> None:
            try:
                await asyncio.sleep(self._FAREWELL_HANGUP_DELAY_S)
                # Bail if a new caller utterance kicked things back off
                # in the meantime — no point hanging up mid-question.
                actor = self.actor
                if actor is None:
                    return
                # 2026-08-21 (cutoff fix — CA3e014ab8): wait for TTS to
                # actually complete before hanging up. Previously we
                # slept a flat extra 1.5s if state was SPEAKING, then
                # hung up regardless — but a booking-confirmation reply
                # can be 15+ sec of audio and the caller heard the tail
                # get cut mid-word ("if you have any questio..."). Now
                # we poll for LISTENING with a bounded max wait so a
                # long trailing sentence completes before we drop the
                # line. Bound protects against stuck-SPEAKING zombies.
                _max_wait_s = 30.0
                _slept = 0.0
                while actor.state == CallState.SPEAKING and _slept < _max_wait_s:
                    await asyncio.sleep(0.25)
                    _slept += 0.25
                if _slept >= _max_wait_s:
                    log.warning(
                        "FAREWELL_HANGUP_TTS_TIMEOUT call=%s waited=%.1fs "
                        "state=%s — proceeding with hangup",
                        self.call_id, _slept, actor.state,
                    )
                elif _slept > 0.0:
                    log.info(
                        "FAREWELL_HANGUP_TTS_DRAINED call=%s waited=%.2fs",
                        self.call_id, _slept,
                    )
                # 2026-08-23 CAf535b0dd fix: check whether the caller
                # ACTUALLY said something since the farewell was
                # scheduled — not whether the idle-followup task
                # exists (it always does after _speak's finally arms
                # it). Old check aborted every farewell hangup =>
                # calls stayed on forever.
                if self._caller_spoke_since_farewell:
                    log.info(
                        "FAREWELL_HANGUP_ABORTED call=%s (caller resumed conversation)",
                        self.call_id,
                    )
                    return
                # 2026-08-24 CAf21b0d5 fix: caller spoke DURING the
                # 0.5s grace sleep and my previous fix missed it. The
                # abort check above fires BEFORE the grace sleep. If
                # the caller starts talking in the grace window, we
                # ignored it and hung up mid-question. Trace:
                # STT_FINAL "can you tell me about the price" fired
                # ~0.5s before FAREWELL_HANGUP. Fix: re-check the
                # flag AFTER the grace sleep, and give a longer
                # window to STT+brain to catch late speech.
                await asyncio.sleep(0.5)
                if self._caller_spoke_since_farewell:
                    log.info(
                        "FAREWELL_HANGUP_ABORTED_LATE call=%s "
                        "(caller spoke during grace window)",
                        self.call_id,
                    )
                    return
                log.info("FAREWELL_HANGUP call=%s — closing call", self.call_id)
                await self.stop(reason="farewell")
            except asyncio.CancelledError:
                pass

        self._farewell_hangup_task = asyncio.create_task(
            _delayed_stop(), name=f"farewell-hangup-{self.call_id}",
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
            if self._idle_prompted:
                # 2026-08-21 NET: we've already prompted once in this
                # idle window — skip straight to farewell + hangup.
                # Without this guard we double-fired "Anything else?"
                # on CA6cd29489 (07:15:41 + 07:15:57).
                log.info(
                    "IDLE_LADDER call=%s stage=farewell (skip prompt, already prompted)",
                    self.call_id,
                )
            else:
                self._idle_prompted = True
                log.info(
                    "IDLE_LADDER call=%s stage=prompt text=%r",
                    self.call_id, self._IDLE_PROMPT,
                )
                # Set flag so _speak's finally-block _arm_idle_followup
                # doesn't reset self._idle_prompted (would restart ladder).
                self._arming_from_idle_loop = True
                try:
                    await self._speak(self._IDLE_PROMPT)
                finally:
                    self._arming_from_idle_loop = False
                # Second window — say goodbye and hangup.
                await asyncio.sleep(self._IDLE_HANGUP_AFTER_PROMPT_S)
                if self.actor is None or self.actor.state != CallState.LISTENING:
                    return
            log.info(
                "IDLE_LADDER call=%s stage=farewell text=%r",
                self.call_id, self._IDLE_FAREWELL,
            )
            self._arming_from_idle_loop = True
            try:
                await self._speak(self._IDLE_FAREWELL)
            finally:
                self._arming_from_idle_loop = False
            # Give the farewell time to actually stream out before we tear down.
            await asyncio.sleep(2.0)
            log.info(
                "IDLE_LADDER call=%s stage=hangup reason=idle_timeout",
                self.call_id,
            )
            await self.stop(reason="idle_timeout")
        except asyncio.CancelledError:
            pass

    # ── R3 P2: structured-input slot capture ─────────────────────────
    #
    # OWNERSHIP: the DIALOGUE / WORKFLOW CONTROLLER opens capture.  Tools
    # do NOT.  A tool is a pure operation over already-validated inputs:
    #     book_appointment(service, time, name, validated_phone)
    # The workflow says "I need a phone" → enter_slot_capture("phone")
    # → validated E.164 lands → workflow invokes the booking tool.
    #
    # While a slot is active:
    #   - STT finals feed the session, not `_run_brain_from_text`
    #   - Speculative brain dispatch is suppressed (fragments would
    #     churn the brain during multi-turn digit dictation)
    #   - VALID → on_commit fires; workflow resumes with a canonical value
    #   - POSSIBLE → on_confirm_needed fires; workflow asks explicit
    #     "just to confirm, is that ...?" and re-enters capture on "no"
    #     or commits on "yes".  POSSIBLE never auto-commits.
    #   - INVALID → stays inside the structured subsystem via on_stall
    #     with stage="escalate"; the general LLM never gets to guess
    #     digits from a partial buffer
    #   - Stall watchdog fires on_stall("first_prompt" | "escalate")
    #     when the caller goes silent mid-capture
    # Stall watchdog defaults (config-overridable via kwargs to
    # enter_slot_capture); the actor stays entirely inside the
    # structured subsystem for the recovery cycle so the LLM never
    # gets a chance to "count digits" from a partial buffer.
    _SLOT_STALL_FIRST_PROMPT_S = 6.0
    _SLOT_STALL_ESCALATE_S = 8.0

    def enter_slot_capture(
        self,
        kind: str,
        config: dict,
        on_commit,
        on_stall=None,
        on_confirm_needed=None,
        stall_first_prompt_s: Optional[float] = None,
        stall_escalate_s: Optional[float] = None,
        modality: str = "audio",
        require_confirmation: Optional[bool] = None,
        extra_instructions: str = "",
        on_enter_persona_hint: str = "",
    ) -> StructuredInputSession:
        """Open a structured-input session for the caller's next turns.

        kind:       slot type registered in packages/slot_parsers/registry
                    (e.g. "phone").
        config:     validator config (e.g. phone_default_region).
        on_commit:  async callable(SlotResult) -> None, fires ONLY on
                    VALID or on caller-confirmed POSSIBLE.  Booking
                    flow resumes here.
        on_confirm_needed:
                    async callable(SlotResult) -> None.  Fires on
                    POSSIBLE — the workflow controller decides how to
                    ask "just to confirm, is that ...?" and re-enter
                    capture on a "no" or commit on a "yes".
        on_stall:   optional async callable(stage: str, session) -> None.
                    Fires when the caller stops talking mid-capture.
                    stage ∈ {"first_prompt", "escalate"}.

        2026-08-29 (LK steal #7): if a sub-agent prompt is available
        for `kind`, stash it on the actor so the next brain turn injects
        the narrow instructions as a system-note.  Callers who invoke
        this via ASK_SLOT get the LK phone_number.py discipline
        automatically — no invention, no simulation, read back in
        groups.  Silent no-op for slot kinds we don't yet have a
        sub-agent prompt for (only 'phone' today).
        """
        # Close any prior capture defensively — one active slot at a time.
        if self._active_slot is not None:
            self.exit_slot_capture(reason="replaced")

        normalizer, validator = get_slot_handlers(kind)
        session = StructuredInputSession(
            slot_type=kind,
            validator=validator,
            config=config,
        )
        self._active_slot = session
        self._slot_normalizer = normalizer
        self._slot_on_commit = on_commit
        self._slot_on_stall = on_stall
        self._slot_on_confirm_needed = on_confirm_needed
        self._slot_stall_first_prompt_s = (
            stall_first_prompt_s if stall_first_prompt_s is not None
            else self._SLOT_STALL_FIRST_PROMPT_S
        )
        self._slot_stall_escalate_s = (
            stall_escalate_s if stall_escalate_s is not None
            else self._SLOT_STALL_ESCALATE_S
        )
        # LK steal #7: attach the narrow sub-agent prompt for this
        # kind if we have one.  Downstream brain turn can read
        # self._active_slot_prompt and inject as a system-note.
        # 2026-08-30 (task #142): extended to email/name/date/yes_no.
        self._active_slot_prompt = None
        try:
            from packages.slot_parsers.slot_capture_prompts import (
                build_phone_capture_prompt,
                build_email_capture_prompt,
                build_name_capture_prompt,
                build_date_capture_prompt,
                build_yes_no_capture_prompt,
            )
            if kind == "phone":
                # Default require_confirmation for audio: True (STT
                # noise makes read-back valuable).  For text: False
                # (the caller sees what they typed).
                if require_confirmation is None:
                    require_confirmation = (modality == "audio")
                self._active_slot_prompt = build_phone_capture_prompt(
                    modality=modality,  # type: ignore[arg-type]
                    require_confirmation=require_confirmation,
                    extra_instructions=extra_instructions,
                    on_enter_persona_hint=on_enter_persona_hint,
                )
            elif kind == "email":
                self._active_slot_prompt = build_email_capture_prompt(
                    modality=modality,  # type: ignore[arg-type]
                    extra_instructions=extra_instructions,
                    on_enter_persona_hint=on_enter_persona_hint,
                )
            elif kind == "name":
                self._active_slot_prompt = build_name_capture_prompt(
                    modality=modality,  # type: ignore[arg-type]
                    extra_instructions=extra_instructions,
                    on_enter_persona_hint=on_enter_persona_hint,
                )
            elif kind == "date":
                self._active_slot_prompt = build_date_capture_prompt(
                    modality=modality,  # type: ignore[arg-type]
                    extra_instructions=extra_instructions,
                    on_enter_persona_hint=on_enter_persona_hint,
                )
            elif kind == "yes_no":
                self._active_slot_prompt = build_yes_no_capture_prompt(
                    modality=modality,  # type: ignore[arg-type]
                    extra_instructions=extra_instructions,
                    on_enter_persona_hint=on_enter_persona_hint,
                )
        except Exception as e:
            # Prompt attachment is defensive — capture must still
            # work if the prompt module is missing.
            log.warning(
                "SLOT_PROMPT_ATTACH_FAILED call=%s kind=%s: %s",
                self.call_id, kind, e,
            )
            self._active_slot_prompt = None
        # Arm the stall watchdog.  It's reset on every feed() and
        # cancelled on commit/reset/exit.
        self._arm_slot_stall_watchdog()
        log.info(
            "SLOT_CAPTURE_ENTER call=%s kind=%s config=%s prompt=%s",
            self.call_id, kind, config,
            bool(self._active_slot_prompt),
        )
        # Emit a typed humanness event so the /trace view shows the
        # capture start in the timeline.  Uses the generic
        # SpeechGateDroppedEvent shape? — no, we want a purpose-built
        # event.  For now, log via the existing durable event_log
        # kind so incident.py shows it, and let a future commit add
        # a proper SlotCaptureEnteredEvent to humanness_events.py.
        try:
            from packages.observability.call_event_log import (
                get_call_event_log, CallEvent as _CE_slot,
                EventSourceKind as _SK_slot,
            )
            _log = get_call_event_log()
            if _log is not None:
                _log.write(_CE_slot(
                    call_id=self.call_id or "?",
                    tenant_id=getattr(
                        self, "tenant_id", "default",
                    ),
                    source=_SK_slot.LLM,
                    kind="slot_capture_enter",
                    payload={
                        "kind": kind,
                        "modality": modality,
                        "prompt_attached": bool(
                            self._active_slot_prompt
                        ),
                        "config_summary": {
                            k: v for k, v in config.items()
                            if k in (
                                "default_region", "accepted_regions",
                            )
                        },
                    },
                ))
        except Exception:
            pass
        return session

    def exit_slot_capture(self, reason: str = "commit") -> None:
        """Close the active slot session, if any.  Cancels stall watchdog."""
        if self._active_slot is None:
            return
        log.info("SLOT_CAPTURE_EXIT call=%s reason=%s buf=%r",
                 self.call_id, reason,
                 (self._active_slot.buffer or "")[:32])
        self._cancel_slot_stall_watchdog()
        self._active_slot = None
        self._slot_normalizer = None
        self._slot_on_commit = None
        self._slot_on_stall = None
        self._slot_on_confirm_needed = None
        # LK steal #7: release the sub-agent prompt so the next
        # brain turn sees the normal system prompt again.
        self._active_slot_prompt = None

    @property
    def slot_capture_active(self) -> bool:
        return self._active_slot is not None

    @property
    def active_slot_prompt(self):
        """LK steal #7: the SlotCapturePrompt currently attached to
        the active capture, or None.  Read-only from outside.

        Brain / turn manager reads this and injects
        `.instructions` as a system-note for the duration of the
        capture.  Returns None during normal (non-capture) turns.
        """
        return self._active_slot_prompt

    def _stage_state_for_brain_dispatch(self, state) -> None:
        """LK steal #7 wire (2026-08-29): copy actor-owned per-turn
        state into `state` before we hand it to the brain.

        Attaches:
          * `_slot_capture_prompt` — narrow sub-agent prompt when a
            phone capture is active (LK steal #7).
          * `_on_policy_decision` — async callback that fires when
            NextActionPolicy chooses ASK_SLOT.  Actor uses it to
            open `enter_slot_capture(kind="phone")` so the NEXT
            caller turn feeds the structured slot session instead
            of the general brain (task #97 second half).

        Call this immediately before every
        `session_manager.run_user_turn(state, brain, ...)` in this
        file.  Networking picked this shape (option B) over a
        packages/core_agent helper so ownership stays inside the
        actor lifecycle.

        Never raises — if state is a shape we don't recognize, the
        attribute assign silently no-ops and the brain sees the
        wider-prompt path.
        """
        try:
            state._slot_capture_prompt = self._active_slot_prompt
        except Exception:
            # State is a dataclass or a plain object; either accepts
            # attribute assignment or errors here defensively.
            pass
        try:
            state._on_policy_decision = (
                self._on_policy_decision_callback
            )
        except Exception:
            pass

    async def _on_policy_decision_callback(self, decision) -> None:
        """Brain calls this after NextActionPolicy runs.  We open
        slot capture on ASK_SLOT(slot='phone').  All other actions
        are no-ops here — the general brain path continues normally.

        Safe to call while a capture is already active — the
        enter_slot_capture side effect closes the prior capture
        defensively (SLOT_CAPTURE_REPLACED).

        The Christiaan scenario: turn N ends, policy sees phone slot
        missing, fires ASK_SLOT(phone).  We enter capture NOW so
        turn N+1 (the caller giving their number) feeds the
        structured session with the LK sub-agent prompt attached.
        """
        try:
            from packages.dialogue.next_action_policy import (
                ConversationAction,
            )
            if getattr(decision, "action", None) != (
                ConversationAction.ASK_SLOT
            ):
                return
            requested = getattr(decision, "requested_slot", None)
            if requested != "phone":
                # Only phone is wired end-to-end today.  name/email/
                # date/yes-no will follow via task #98.
                return
            if self._active_slot is not None and (
                self._active_slot.slot_type == "phone"
            ):
                # Already capturing phone — no-op.
                return
            # Fire the capture.  Config pulled from tenant business
            # profile if available; otherwise safe defaults.
            _biz = getattr(self, "business", None) or getattr(
                self, "_business", None,
            )
            _default_region = (
                getattr(_biz, "phone_default_region", "US")
                if _biz is not None else "US"
            )
            _accepted_regions = (
                getattr(_biz, "phone_accepted_regions", None)
                if _biz is not None else None
            )
            config = {
                "default_region": _default_region,
                "accepted_regions": _accepted_regions or [],
            }
            self.enter_slot_capture(
                kind="phone",
                config=config,
                on_commit=self._default_slot_on_commit,
                modality="audio",
                require_confirmation=True,
            )
        except Exception as e:
            import logging as _pol_log
            _pol_log.getLogger(__name__).warning(
                "policy-decision callback ASK_SLOT wiring failed "
                "(non-fatal): %s", e,
            )

    async def _default_slot_on_commit(self, result) -> None:
        """Default on_commit hook — validated E.164 lands.  We stash
        it on the actor for the next brain turn to see; downstream
        booking-flow code reads it from state or via a booking-tool
        pre-fill.  Deliberately minimal here — this is the plumbing
        that proves the loop closes end-to-end.  Fully-featured
        booking-continuation is a follow-up.
        """
        self._last_validated_phone = getattr(result, "value", None)
        import logging as _sc_log
        _sc_log.getLogger(__name__).info(
            "SLOT_CAPTURE_COMMIT_DEFAULT call=%s value=%r",
            self.call_id,
            (self._last_validated_phone or "")[:32],
        )

    def _arm_slot_stall_watchdog(self) -> None:
        """Start / restart the inactivity timer for the active slot."""
        self._cancel_slot_stall_watchdog()
        if self._active_slot is None:
            return
        self._slot_stall_task = asyncio.create_task(
            self._slot_stall_loop(),
            name=f"slot-stall-{self.call_id}",
        )

    def _cancel_slot_stall_watchdog(self) -> None:
        task = getattr(self, "_slot_stall_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._slot_stall_task = None

    async def _slot_stall_loop(self) -> None:
        """Two-stage stall recovery: prompt at T1, escalate at T1+T2.

        Stage 1 (first_prompt) — nudge the caller to keep going, keeping
        the current buffer.  Stage 2 (escalate) — hand back to the
        workflow controller so it can offer DTMF or re-open capture.
        The LLM is NOT invited to interpret a partial buffer here.
        """
        try:
            await asyncio.sleep(self._slot_stall_first_prompt_s)
            if self._active_slot is None:
                return
            on_stall = self._slot_on_stall
            if on_stall is not None:
                try:
                    await on_stall("first_prompt", self._active_slot)
                except Exception as e:
                    log.exception(
                        "SLOT_STALL_HANDLER_FAILED call=%s stage=first_prompt: %s",
                        self.call_id, e,
                    )
            await asyncio.sleep(self._slot_stall_escalate_s)
            if self._active_slot is None:
                return
            if on_stall is not None:
                try:
                    await on_stall("escalate", self._active_slot)
                except Exception as e:
                    log.exception(
                        "SLOT_STALL_HANDLER_FAILED call=%s stage=escalate: %s",
                        self.call_id, e,
                    )
        except asyncio.CancelledError:
            pass

    async def _feed_active_slot(
        self, transcript: str, turn_gen: int,
        source: SlotSource = SlotSource.SPEECH,
    ) -> bool:
        """Route an STT final (or DTMF batch) to the active slot session.

        Return contract:
          True  — transcript was CONSUMED by the slot (do NOT run brain).
          False — no active slot (brain should run as usual).

        NOTE: INVALID does NOT escape back to the LLM.  We stay inside
        the structured subsystem via on_stall/on_confirm_needed hooks
        so the general LLM never gets to guess digits from a partial buffer.
        """
        session = self._active_slot
        if session is None:
            return False

        normalizer = self._slot_normalizer
        result = session.feed(transcript, normalize=normalizer, source=source)

        log.info(
            "SLOT_FEED call=%s kind=%s status=%s buf=%r src=%s reason=%r",
            self.call_id, session.slot_type, result.status.value,
            (session.buffer or "")[:32], source.value, result.reason or "",
        )

        # Any fragment resets the stall timer — caller is still engaged.
        self._arm_slot_stall_watchdog()

        # VALID → commit immediately.  This is a validator-confirmed
        # canonical value; no caller confirmation needed.
        if result.status == SlotStatus.VALID:
            if result.value:
                session.commit(result.value)
            on_commit = self._slot_on_commit
            self.exit_slot_capture(reason="valid")
            if on_commit is not None:
                try:
                    await on_commit(result)
                except Exception as e:
                    log.exception(
                        "SLOT_ON_COMMIT_FAILED call=%s: %s",
                        self.call_id, e,
                    )
            return True

        # POSSIBLE → right shape but not verified.  Do NOT commit; the
        # workflow controller must ask for explicit confirmation before
        # promoting to VALID.  This protects against libphonenumber
        # metadata lag for newly allocated ranges (and future non-phone
        # validators with the same shape/allocation distinction).
        if result.status == SlotStatus.POSSIBLE:
            on_confirm = self._slot_on_confirm_needed
            if on_confirm is not None:
                try:
                    await on_confirm(result)
                except Exception as e:
                    log.exception(
                        "SLOT_ON_CONFIRM_FAILED call=%s: %s",
                        self.call_id, e,
                    )
            else:
                # No confirmation hook — safest is to stay in capture so
                # the workflow controller can re-prompt.  Do not commit.
                log.warning(
                    "SLOT_POSSIBLE_NO_CONFIRM_HOOK call=%s buf=%r — staying in capture",
                    self.call_id, (session.buffer or "")[:32],
                )
            return True

        # INVALID → do NOT resume the general LLM.  Stay inside the
        # structured subsystem and let the workflow controller decide
        # how to recover (re-prompt, DTMF fallback, escalate).  The
        # on_stall handler is the escape hatch — INVALID triggers an
        # immediate "escalate" so the workflow can react.
        if result.status == SlotStatus.INVALID:
            log.warning(
                "SLOT_INVALID call=%s reason=%s buf=%r — staying in capture",
                self.call_id, result.reason,
                (session.buffer or "")[:32],
            )
            on_stall = self._slot_on_stall
            if on_stall is not None:
                try:
                    await on_stall("escalate", session)
                except Exception as e:
                    log.exception(
                        "SLOT_ON_STALL_FAILED call=%s: %s",
                        self.call_id, e,
                    )
            # Even on INVALID, we stay in capture — the workflow may
            # reset() the buffer to give the caller a fresh try.
            return True

        # INCOMPLETE / AMBIGUOUS → keep listening.  Stall watchdog will
        # fire if the caller goes silent.
        return True

    # ── R3 P3: DTMF + ANI ────────────────────────────────────────────

    async def on_dtmf(self, digit: str, track: str = "inbound_track") -> None:
        """Twilio delivered one DTMF keypress.  If a slot session is
        active, feed it as a SlotSource.DTMF fragment (same accumulator
        as speech — the caller can mix "0333" spoken + "5244772" keyed).
        Outside of slot capture we just log and drop (future: could
        interrupt the agent for a DTMF menu, but not needed yet).
        """
        digit = digit.strip()
        if not digit:
            return
        # * and # arrive as literal characters; keep them for future
        # menu semantics.  The phone validator strips non-digits so a
        # stray * mid-number doesn't pollute the buffer.
        if self.slot_capture_active:
            log.info(
                "DTMF_FEED_SLOT call=%s digit=%s track=%s slot=%s",
                self.call_id, digit, track,
                self._active_slot.slot_type if self._active_slot else "?",
            )
            # turn_gen is not tracked at this layer — DTMF is out-of-band
            # with the STT turn generator.  Use 0 as a sentinel; the
            # slot session doesn't use it for anything.
            await self._feed_active_slot(
                digit, turn_gen=0, source=SlotSource.DTMF,
            )
        else:
            log.info(
                "DTMF_IGNORED call=%s digit=%s track=%s (no slot active)",
                self.call_id, digit, track,
            )

    def resolve_ani_candidate(
        self,
        default_region: Optional[str] = None,
        accepted_regions: Optional[list[str]] = None,
    ):
        """Return the caller's ANI (from Twilio start.customParameters)
        parsed through libphonenumber, so the workflow can offer:
          "Should I use the number you're calling from?"

        Returns:
          A `SlotResult`-shaped object with status VALID / POSSIBLE /
          INVALID / (INCOMPLETE if no ANI available).  The workflow is
          responsible for asking the caller to confirm before treating
          the value as authoritative — the ANI is a CANDIDATE, never
          an auto-committed answer.

        default_region / accepted_regions:
          When not passed, the workflow can call parse_phone directly
          on `self.caller_number`.  We keep them optional so a tenant
          config wire-up later can pass them without another API change.
        """
        # Local import so tests that only exercise slot semantics
        # don't pay the libphonenumber load cost.
        from packages.slot_parsers import parse_phone, PhoneStatus
        from packages.slot_parsers.session import SlotResult, SlotStatus

        if not self.caller_number:
            log.info(
                "ANI_UNAVAILABLE call=%s (no caller_number from Twilio)",
                self.call_id,
            )
            return SlotResult(
                status=SlotStatus.INCOMPLETE,
                reason="no caller_number available (blocked ID or Media "
                       "Streams TwiML without <Parameter>)",
                raw_digits="",
            )

        r = parse_phone(
            self.caller_number,
            default_region=default_region or "US",
            accepted_regions=accepted_regions,
        )
        # Map PhoneStatus → SlotStatus for a uniform workflow surface.
        if r.status == PhoneStatus.COMPLETE:
            status = SlotStatus.VALID
        elif r.status == PhoneStatus.POSSIBLE:
            status = SlotStatus.POSSIBLE
        elif r.status in (
            PhoneStatus.PARTIAL, PhoneStatus.EMPTY,
        ):
            status = SlotStatus.INCOMPLETE
        else:
            status = SlotStatus.INVALID

        log.info(
            "ANI_RESOLVED call=%s raw=%r status=%s value=%r region=%s",
            self.call_id, self.caller_number,
            status.value, r.value, r.matched_region,
        )
        return SlotResult(
            status=status,
            value=r.value,
            matched_region=r.matched_region,
            raw_digits=self.caller_number,
            reason=r.reason or "from Twilio ANI",
        )

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

        # 2026-08-18: hang up promptly when the LLM says goodbye.
        # Was waiting 15+15+2 = 32s in the idle-followup ladder after
        # a farewell, wasting caller minutes on a completed booking.
        self._maybe_hangup_after_farewell(text)

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
        # 2026-08-13 (double-brain fix): mark the in-flight dispatch as
        # having produced audio, so a Deepgram-fragment-then-superset
        # arriving after this point gets dropped by _should_dedupe_dispatch.
        self._inflight_has_spoken = True
        # R5: transition to SPEAKING is a response signal.
        self._clear_turn_committed("speak_start")
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
            # 2026-08-31 CALL-BUG-10: skip idle arm when the text we
            # just spoke IS a farewell — otherwise "anything else?"
            # fires 10s after the goodbye. See streaming pump for the
            # sibling check.
            _speak_is_farewell = False
            if text:
                import re as _re_fw2
                _low_speak = text.lower()
                for _fw_pat in self._FAREWELL_PATTERNS:
                    if _re_fw2.search(rf"\b{_fw_pat}\b", _low_speak):
                        _speak_is_farewell = True
                        break
            if not _speak_is_farewell:
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
                    # 2026-08-20: log START and DONE with wall-time so
                    # per-call logs show the full fastpath/greeting speak
                    # window.  Was invisible before — the fastpath's audio
                    # duration didn't show up anywhere and made "state →
                    # listening" look like the true audio-done time.
                    import time as _t
                    _fp_t0 = _t.perf_counter()
                    log.info(
                        "TTS_STREAM_START call=%s gen=%d transport=cache text=%r bytes=%d",
                        self.call_id, gen, text[:60], len(audio_bytes),
                    )
                    # 2026-08-21 NET-14-followup: watchdog open.
                    _wd_stream_id = self._tts_stream_open(
                        gen=gen, source="answer", text=text, transport="cache",
                    )
                    if span is not None:
                        span.mark("tts_first_byte")
                        self._close_turn_span()
                    # 2026-08-20: CALLER_LATENCY on the cache-hit fastpath.
                    # First TTS byte here is measured against the last DG
                    # STT final anchor (set in _on_stt_final).
                    # 2026-08-23 AUDIT-S3: renamed est_caller_hears_ms →
                    # twilio_playout_ack_est_ms.  The +200 is a jitter
                    # estimate for the Twilio Media Streams send→playout
                    # window, not a measurement of when the caller
                    # actually hears the audio.  Real caller-heard timing
                    # comes from the FIRST40 mark ACK (TWILIO_FIRST40_ACK
                    # log line).  Same numeric value; honest name.
                    if self._last_stt_final_perf is not None:
                        _perceived_ms = (_t.perf_counter() - self._last_stt_final_perf) * 1000
                        log.info(
                            "CALLER_LATENCY call=%s gen=%d path=cache "
                            "stt_final_to_first_byte_ms=%.0f "
                            "twilio_playout_ack_est_ms=%.0f text=%r",
                            self.call_id, gen, _perceived_ms,
                            _perceived_ms + 200,
                            self._last_stt_final_text[:60],
                        )
                        self._last_stt_final_perf = None
                        self._last_stt_final_text = ""
                    # 2026-08-23 AUDIT-S2: arm FIRST40 gate at reply-
                    # boundary.  Single-shot send below emits one mark.
                    self._first40_pending["answer"] = True
                    self._first_media_pending["answer"] = True
                    try:
                        await self._send_audio_frames(audio_bytes, mime)
                    finally:
                        # 2026-08-21 NET-14-followup: close watchdog on
                        # any exit — normal completion, exception, or
                        # cancellation.  Prevents phantom watchdog fires
                        # on a healthy short-circuit path.
                        self._tts_stream_close(_wd_stream_id)
                    _fp_ms = (_t.perf_counter() - _fp_t0) * 1000
                    log.info(
                        "TTS_STREAM_DONE call=%s gen=%d transport=cache chunks=1 bytes=%d total_ms=%.0f",
                        self.call_id, gen, len(audio_bytes), _fp_ms,
                    )
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

        # 2026-08-23 AUDIT-S2: arm FIRST40 gate for the batch-synth
        # legacy fallback path (fires when streaming is disabled: browser,
        # VPL two-planner, or provider lacks stream_synthesize). Single-
        # shot send emits one mark.
        self._first40_pending["answer"] = True
        self._first_media_pending["answer"] = True
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
        # 2026-08-21 NET-14-followup: open watchdog before entering
        # the stream loop.  Closed in the outer try/finally below so
        # normal completion, cancellation, exception, and WS→HTTP
        # fallback all cleanly close the tracker.  Source="answer"
        # because this path only fires for the caller-visible reply
        # (filler audio goes through `_play_cached_backchannel` which
        # calls _send_audio_frames with source="filler" and does NOT
        # use _stream_tts_incremental).
        _wd_stream_id = self._tts_stream_open(
            gen=gen, source="answer", text=text, transport=transport,
        )
        # 2026-08-23 AUDIT-S2: arm FIRST40 gate at reply-boundary.
        # Without this, every EL chunk's call to `_send_audio_frames`
        # would emit its own FIRST40 mark; with it, only the first
        # chunk to reach 40ms of audio emits one, then the gate
        # self-clears until the next reply.
        #
        # 2026-08-24 ChatGPT audit fix: this method fires per SENTENCE
        # within a streaming reply. Naively re-arming here caused
        # FIRST_MEDIA_SENT + FIRST40 to fire per-sentence, not
        # per-reply. Fix: only arm if we haven't emitted for this
        # turn_gen yet. `_first_media_emitted_turn_gens` tracks it.
        if gen not in self._first_media_emitted_turn_gens:
            self._first40_pending["answer"] = True
            self._first_media_pending["answer"] = True
            self._first_media_emitted_turn_gens.add(gen)

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
                    # 2026-08-20: one-line perceived-latency summary.
                    # DG speech_final → first TTS byte leaves us + assumed
                    # ~200ms Twilio jitter = what caller actually hears.
                    # Only fires when we have a real STT anchor (not for
                    # agent-initiated speech like the greeting).
                    # 2026-08-23 AUDIT-S3: renamed est_caller_hears_ms →
                    # twilio_playout_ack_est_ms.  Same +200 estimate;
                    # honest name — the +200 is a Twilio send→playout
                    # jitter guess, not measurement of caller ear-time.
                    # For actual caller-heard timing, cross-reference the
                    # TWILIO_FIRST40_ACK line with the same call/gen.
                    if self._last_stt_final_perf is not None:
                        _perceived_ms = (_t.perf_counter() - self._last_stt_final_perf) * 1000
                        log.info(
                            "CALLER_LATENCY call=%s gen=%d path=stream "
                            "stt_final_to_first_byte_ms=%.0f "
                            "twilio_playout_ack_est_ms=%.0f text=%r",
                            self.call_id, gen, _perceived_ms,
                            _perceived_ms + 200,  # jitter estimate
                            self._last_stt_final_text[:60],
                        )
                        self._last_stt_final_perf = None
                        self._last_stt_final_text = ""
                chunk_count += 1
                cumulative_bytes += len(chunk)
                await self._send_audio_frames(chunk, mime)
        except asyncio.CancelledError:
            log.info(
                "TTS_STREAM_CANCELLED call=%s gen=%d transport=%s "
                "chunks=%d bytes=%d",
                self.call_id, gen, transport, chunk_count, cumulative_bytes,
            )
            # 2026-08-21 NET-14-followup: close watchdog on cancel so
            # a barge-in / bump_turn cancellation doesn't leave a
            # phantom watchdog that fires HUNG minutes later.
            self._tts_stream_close(_wd_stream_id)
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
                    # 2026-08-21 NET-14-followup: close watchdog on
                    # fallback failure (no ledger emit after this path).
                    self._tts_stream_close(_wd_stream_id)
                    return
            else:
                # 2026-08-21 NET-14-followup: close watchdog on
                # non-fallback failure exit.
                self._tts_stream_close(_wd_stream_id)
                return

        # 2026-08-21 NET-14-followup: normal completion path — close
        # watchdog BEFORE the DONE log so future greppers see a clean
        # sequence (open → done → close, all silent unless HUNG fires).
        self._tts_stream_close(_wd_stream_id)
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
            # 2026-08-12: first partial → open a new turn span so
            # TURN_SUMMARY can measure stt→llm→tts per turn.  media_in
            # is set at the same moment (this is the first evidence
            # caller audio hit the pipe).
            if self._current_turn_span is None and self.actor is not None:
                self._open_turn_span(self.actor.turn_generation)
            if self._current_turn_span is not None:
                self._current_turn_span.mark("media_in")
                self._current_turn_span.mark("stt_first_partial")
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
            # 2026-08-23 CAf535b0dd hangup-forever fix: caller actually
            # said something. If we're in a farewell hangup window, this
            # is the signal to abort. See _maybe_hangup_after_farewell.
            self._caller_spoke_since_farewell = True
            # 2026-08-20: capture wall-time on any real-text final (both
            # is_final and speech_final variants).  This is the anchor
            # for CALLER_LATENCY (stt_final → first TTS byte).
            import time as _t
            _now_perf = _t.perf_counter()
            self._last_stt_final_perf = _now_perf
            # Separate anchor for POST_EOT_HOLD metric (see __init__).
            self._last_stt_final_perf_hold = _now_perf
            self._last_stt_final_text = text
            # 2026-08-12: mark stt_final on the turn span so TURN_SUMMARY
            # can compute stt_partial→stt_final and stt_final→brain_start.
            if self._current_turn_span is not None:
                self._current_turn_span.mark("stt_final")
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
        these kinds so this handler is Flux-only in practice.

        2026-08-24 ChatGPT audit CRITICAL fix: Flux `end_of_turn` bypasses
        `_on_stt_final` entirely, so the two anchors set there were
        NEVER firing on Flux:
          (1) `_caller_spoke_since_farewell = True` — hangup abort
              check silently missed every caller speech during grace,
              causing the "hangup fired mid-caller-speech" bug on
              CAf21b0d5.
          (2) `_last_stt_final_perf_hold` — POST_EOT_HOLD_MS metric
              always showed -1 because the anchor was never set.
        Fix: set both here too for the Flux-specific `end_of_turn`
        kind. `eager_end_of_turn` fires earlier and shouldn't set the
        final anchors (it's speculative). `turn_resumed` means the
        caller kept talking — don't touch flags.
        """
        kind = event.kind
        text = event.payload.get("text", "")
        _tel.record_stream_event(self.tenant_id, kind=kind)
        # 2026-08-24: set the anchors that _on_stt_final would have
        # set for a Nova-3 speech_final. Only on the AUTHORITATIVE
        # end-of-turn (not eager, not resumed).
        if kind == "end_of_turn" and text:
            import time as _t
            _now_perf = _t.perf_counter()
            self._caller_spoke_since_farewell = True
            self._last_stt_final_perf = _now_perf
            self._last_stt_final_perf_hold = _now_perf
            self._last_stt_final_text = text
            self._streaming_utterance_text = text
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
            # R3 P2: during structured-input capture we suppress
            # speculative brain — digit fragments would churn the LLM
            # while the slot session accumulates.
            if self.slot_capture_active:
                log.debug("speculative brain suppressed (slot capture) call=%s",
                          self.call_id)
                return True
            # 2026-08-19 (T3.6 fix): if the caller's text ends on an
            # incomplete trailing word (K1 territory — "Can you", "and",
            # "for the"), do NOT speculate.  Otherwise we speak a
            # premature reply, K1 flushes the continuation as a NEW
            # turn (interpreted as an INTERRUPTION mid-agent-speech),
            # and the caller hears a triple-stacked response.  Observed
            # on CAe88134d2959e8f4c0e8933d731d9a8b0 (2026-08-19 Karachi
            # test): "Yeah. I wanted to get tooth implants. Can you" →
            # spec fires "Sure! We can help with that." → K1 flushes
            # "tell me how to do that?" → turn-manager classifies as
            # INTERRUPTION → gen bumps → full reply plays over the top.
            #
            # Only suppress if the text has NO terminal punctuation
            # (Deepgram's smart-format adds .!? when confident), so
            # a clean "book me for 3pm." still speculates.
            try:
                from packages.runtime.turn_manager import _INCOMPLETE_TRAILING_WORDS
                stripped_text = text.strip()
                has_terminal_punct = (
                    stripped_text and stripped_text[-1] in ".!?"
                )
                last_word = (
                    stripped_text.rstrip(".,!?;:").split()[-1].lower()
                    if stripped_text else ""
                )
                if not has_terminal_punct and last_word in _INCOMPLETE_TRAILING_WORDS:
                    log.info(
                        "speculative suppressed (K1 incomplete-word %r) call=%s text=%r",
                        last_word, self.call_id, text[:80],
                    )
                    return True
            except Exception:
                pass
            # Don't stack — a prior speculative task means we haven't
            # cleaned up yet; skip this one.
            if getattr(self, "_speculative_task", None) and \
                    not self._speculative_task.done():
                return True
            speculative_turn = actor.turn_generation
            # 2026-08-17 (fastpath regression fix): the conv-control
            # fastpath sits AFTER the commit-lock claim inside
            # _run_brain_from_text.  Once speculative claims the lock
            # here (below), the spawned run_brain SKIPs and the
            # fastpath is dead code.  Try it BEFORE the claim — if it
            # matches, spawn a fastpath speak task instead of the LLM.
            if self._matches_conversation_control_intent(text):
                if not self._try_claim_response_commit(
                    speculative_turn, reason="conv_control_fastpath",
                ):
                    return True
                self._speculative_text = text
                self._speculative_task = asyncio.create_task(
                    self._speak_conversation_control_fastpath(
                        text, speculative_turn,
                    ),
                    name=f"conv-control-{self.call_id}-{speculative_turn}",
                )
                return True
            # Task #369: enforce one-gen-one-commit BEFORE spawning.
            # Prevents the Abdullah-gen=20 race where speculative HIT
            # cleared _speculative_task while the task was still
            # running, letting a second EAGER_END_OF_TURN pass the
            # "no in-flight" guard and fire another brain.
            if not self._try_claim_response_commit(
                speculative_turn, reason="speculative",
            ):
                return True
            log.info("speculative brain firing call=%s gen=%d text=%r",
                     self.call_id, speculative_turn, text[:80])
            self._speculative_text = text
            # T4 (2026-08-19): pass owns_lock=True so the spawned brain
            # skips the redundant self._try_claim_response_commit that
            # was silently vetoing every speculative dispatch.
            self._speculative_task = asyncio.create_task(
                self._run_brain_from_text(
                    text, speculative_turn, owns_lock=True,
                ),
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
            # 2026-08-21 CRITICAL FIX: release the commit-lock claimed
            # by the now-dead speculative dispatch. Without this the
            # gen stays "committed" and the REAL final's dispatch fails
            # _try_claim_response_commit with reason=speculative, then
            # the caller waits 1+ seconds for the lock to expire while
            # the real reply sits blocked. Observed on CA83f5a9e:
            # speculative fired at 6.39s on partial "Hello. Can you
            # hear", cancelled at 6.45s, but gen=0 remained locked so
            # the real final at 6.93s hit COMMIT_LOCK_SKIP and the
            # caller heard nothing until 8.03s (filler kick-in).
            if self.actor is not None:
                _stale_gen = self.actor.turn_generation
                self._committed_response_gens.discard(_stale_gen)
                log.info(
                    "COMMIT_LOCK_RELEASE call=%s gen=%d reason=turn_resumed",
                    self.call_id, _stale_gen,
                )
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
        #
        # 2026-08-31 CALL-BUG-11: honest re-ask detection. Real trace
        # CAe854acb1: caller said "next available slot" → bad cache
        # gave nonsense reply → caller said "next available slot"
        # AGAIN → dedupe stripped it to '' → dead air. The signal
        # that distinguishes redelivery from re-ask is: has the agent
        # ALREADY SPOKEN a reply to the previous transcript? If yes,
        # the caller has heard the reply and is intentionally
        # repeating themselves — do NOT drop.
        norm_prev = prev_transcript.strip().lower()
        norm_add = addition.lower()
        already_replied = (
            self._inflight_has_spoken
            and actor is not None
            and actor.state.name == "LISTENING"
        )
        if norm_prev and norm_prev in norm_add and not already_replied:
            addition = addition[norm_add.index(norm_prev) + len(norm_prev):].lstrip(" ,.-")
            log.info("stripped duplicated prefix call=%s: keeping %r",
                     self.call_id, addition)
            if not addition:
                # Entire "new" final was just the old transcript replayed.
                return True
        elif norm_prev and norm_prev in norm_add and already_replied:
            # Caller re-asked. Keep the transcript intact, clear the
            # "last committed" so we treat this as fresh (not a merge).
            log.info(
                "re-ask detected call=%s: prev=%r add=%r — replying fresh",
                self.call_id, prev_transcript, addition,
            )
            self._last_committed_transcript = ""
            prev_transcript = ""

        # 2026-08-31 CALL-BUG-12: only continuation-merge when the
        # PREVIOUS transcript hasn't been fully replied to. Once agent
        # has spoken a reply and is in LISTENING, a fresh utterance is
        # a NEW turn, not a continuation. Real trace CAee03239: caller
        # said "it was a cleaning" (turn 1) → agent replied → 5.9s
        # later caller said "next available slot" → merge fired,
        # concatenated the two into "cleaning. next available slot",
        # LLM asked wrong follow-up question. Fix: gate on
        # `_inflight_has_spoken == True + state LISTENING` to signal
        # "the agent has already replied to prev — this is a new turn".
        already_replied_to_prev = (
            self._inflight_has_spoken
            and actor is not None
            and actor.state.name == "LISTENING"
        )
        if (
            prev_transcript
            and gap <= self._CONTINUATION_MERGE_MAX_S
            and not already_replied_to_prev
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

        2026-08-21 NET-E1 (Flux fragment-merge skip): Flux's own turn
        state machine (StartOfTurn / Update / EagerEndOfTurn /
        TurnResumed / EndOfTurn) already handles multi-part utterances.
        Flux EndOfTurn is authoritative — no other END_OF_TURN will
        follow for the same turn.  So on Flux the 400ms fragment-merge
        wait is pure dead time.  BUT the 2000ms structured-data widening
        still applies (Flux fires EndOfTurn faster than a caller can
        finish dictating a phone number) — that widened window is a
        different guarantee about intra-turn coalescing.  So we only
        skip the base 400ms.
        """
        # Structured-data ask → wide window applies regardless of provider.
        last_agent = (self._recent_agent_utterances[-1] if self._recent_agent_utterances else "").lower()
        is_structured_ask = any(kw in last_agent for kw in (
            "phone number", "10-digit", "ten-digit", "ten digit",
            "your name", "full name", "your number", "callback",
            "spell", "address", "email",
        ))
        # 2026-08-24 ChatGPT breakthrough audit: when Flux is authoritative
        # for turn boundaries, DO NOT add a 2000ms structured-ask hold on
        # top. Flux fires EndOfTurn on semantic completion — a caller who
        # pauses mid-phone-number ("0333 <pause> 5244772") emits TurnResumed
        # and Flux waits for the resumption. Adding an application-level
        # 2000ms sleep AFTER Flux already committed is pure dead time and
        # was the smoking gun explaining the 2.5s plateau — bench-shipped
        # STT/model wins were being masked by this manufactured wait.
        # The 2000ms wait remains for Nova-3 (non-Flux) which lacks the
        # native TurnResumed signal.
        if is_structured_ask and not settings.deepgram_use_flux:
            window_ms = 2000
            log.debug(
                "fragment merge window widened to %dms "
                "(structured-data ask, non-Flux path)", window_ms,
            )
        elif is_structured_ask and settings.deepgram_use_flux:
            # 2026-08-24 (v2, ChatGPT correction): my earlier fix set
            # this to 0ms on the Flux path assuming TurnResumed would
            # cancel any premature brain dispatch. ChatGPT verified
            # against Deepgram docs: TurnResumed ONLY follows
            # EagerEndOfTurn. Once Final EndOfTurn fires, the turn is
            # closed — if the caller resumes ("0333"[pause]"5244772"),
            # the resumption arrives as a NEW turn, not a cancellation.
            # So 0ms would let brain speak "was that all?" while caller
            # is still dictating the second half.
            # Compromise: 500ms cooldown on Flux structured turns. Long
            # enough for a 2nd Final EndOfTurn (continuation) to arrive
            # and get continuation-merged into the same commit. Short
            # enough that natural end-of-utterance doesn't feel slow.
            # The proper fix is wiring enter_slot_capture() into the
            # workflow — deferred to voice-agent's NextActionPolicy
            # rollout. See docs/POST-EOT-COOLDOWN-STRATEGY-2026-08-24.md
            # and packages/slot_parsers/session.py (already exists,
            # only test callers today).
            window_ms = 500
            log.info(
                "STRUCTURED_ASK_FLUX_COOLDOWN call=%s window_ms=%d "
                "(was 2000ms pre-fix; interim until enter_slot_capture "
                "is wired into workflow)",
                self.call_id, window_ms,
            )
        elif settings.deepgram_use_flux:
            # Flux EndOfTurn is authoritative — skip base merge wait.
            window_ms = 0
        else:
            window_ms = self._FRAGMENT_MERGE_WINDOW_MS
        if window_ms > 0:
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
        # 2026-08-24 ChatGPT breakthrough audit: K1 is a LEXICAL heuristic
        # ("does the last word suggest more speech?"). Flux is a SEMANTIC
        # detector that already answered that question. When Flux fires
        # authoritative EndOfTurn, K1 is stale-tech duplicate work — and
        # it's what caused "Or day after" (last word "after" is in
        # _INCOMPLETE_TRAILING_WORDS) to hold 2s despite Flux confirming
        # the caller genuinely stopped. Skip K1 on Flux entirely. K1
        # stays enabled for the Nova-3 (non-Flux) path which lacks a
        # semantic EOT detector and needs the lexical safety net.
        self._pending_k1_hint = ""
        _k1_enabled = not settings.deepgram_use_flux
        if not _k1_enabled:
            log.debug(
                "K1_SKIP_FLUX call=%s (Flux is authoritative on EOT)",
                self.call_id,
            )
            self._incomplete_hold_started_at = None
        try:
            if not _k1_enabled:
                # Flux path — skip K1 block entirely.  Fall through
                # to the commit path below.
                raise _K1SkipSentinel()
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
        except _K1SkipSentinel:
            # 2026-08-24: Flux-path K1 skip — expected control flow, silent.
            pass
        except Exception as e:
            log.debug("K1 completeness check failed: %s", e)

        # K1 hold-timer resets on commit — next turn starts fresh.
        self._incomplete_hold_started_at = None

        # Record the transcript + timestamp so a follow-on fragment
        # arriving after this point (during brain/speech) can be
        # continuation-merged rather than starting a competing turn.
        # 2026-08-17 (fastpath triple-speak fix): if the conv-control
        # fastpath already answered on EAGER, do NOT arm the merge
        # anchor.  A fresh sentence 5s later must dispatch as a new
        # turn, not staple onto the fastpath's original text.
        if self._suppress_next_continuation_anchor:
            self._suppress_next_continuation_anchor = False
            self._last_committed_transcript = ""
            log.info(
                "continuation-merge anchor suppressed (fastpath answered) "
                "call=%s text=%r",
                self.call_id, text[:80],
            )
        else:
            self._last_committed_transcript = text
        self._last_final_monotonic = time.monotonic()

        # 2026-08-10 (task #284): speculative dispatch short-circuit.
        # If we already fired a speculative brain on EAGER_END_OF_TURN
        # AND the confirmed text matches (or is a trivial prefix/suffix
        # extension), the reply is already in flight.  Its brain_completed
        # event will land on this same turn_generation.  Do NOT bump the
        # turn (that would cancel our own in-flight task) and do NOT
        # spawn a second brain.
        #
        # Task #369 note: we intentionally leave the commit-lock claim
        # in place for the speculative's gen — the task is still running
        # and owns the response.  Clearing the marker here (which we
        # used to also do, and which Abdullah's gen=20 double-fire
        # exploited) is fine now because a follow-on EAGER_END_OF_TURN
        # will fail the commit-lock claim (same gen still held) and
        # skip its dispatch.
        spec_task = getattr(self, "_speculative_task", None)
        spec_text = getattr(self, "_speculative_text", None) or ""
        if spec_task is not None and not spec_task.done() and spec_text:
            if _text_matches_for_speculative(spec_text, text):
                log.info("speculative HIT call=%s: text=%r spec=%r",
                         self.call_id, text[:60], spec_text[:60])
                # Clear the speculative markers; the task itself will
                # emit brain_completed as usual.  The commit lock for
                # its gen stays held until bump_turn / stall / hangup.
                self._speculative_task = None
                self._speculative_text = None
                return True
            # Text drifted — cancel speculative and release its lock
            # so bump_turn can proceed cleanly on the new gen below.
            log.info("speculative MISS call=%s: cancelling, text=%r spec=%r",
                     self.call_id, text[:60], spec_text[:60])
            spec_task.cancel()
            self._committed_response_gens.discard(actor.turn_generation)
            self._speculative_task = None
            self._speculative_text = None

        # 2026-08-24 ChatGPT audit — POST_EOT_HOLD_MS metric.
        # Measures the delta from when Deepgram Flux fired Final
        # EndOfTurn (captured as self._last_stt_final_perf in
        # _on_stt_final) to when we're about to dispatch the brain.
        # If this exceeds ~500ms without an explicit reason
        # (structured_ask / k1_incomplete / commit_lock etc) it means
        # we've reintroduced application-side dead time downstream of
        # the semantic EOT detector — the exact class of bug that
        # created the 2.5s plateau (see docs/POST-EOT-COOLDOWN-STRATEGY
        # -2026-08-24.md). Emit ALWAYS so we have baseline for both
        # the fast path and the intentional-hold paths.
        import time as _t_hold
        _post_eot_ms = -1
        # Use the hold-specific timestamp so we don't race the CALLER_LATENCY
        # emit that nulls _last_stt_final_perf.
        _hold_anchor = getattr(self, "_last_stt_final_perf_hold", None)
        if _hold_anchor is not None:
            _post_eot_ms = int(
                (_t_hold.perf_counter() - _hold_anchor) * 1000
            )
        _hold_reason = "none"
        if is_structured_ask:
            _hold_reason = "structured_ask"
        elif getattr(self, "_pending_k1_hint", ""):
            _hold_reason = "k1_incomplete_word"
        log.info(
            "POST_EOT_HOLD call=%s post_eot_ms=%d reason=%s flux=%s",
            self.call_id, _post_eot_ms, _hold_reason,
            settings.deepgram_use_flux,
        )

        await actor.bump_turn(reason="end-of-turn")
        self._open_turn_span(actor.turn_generation)
        if self._current_turn_span is not None:
            self._current_turn_span.mark("media_in")
            self._current_turn_span.mark("stt_final")

        turn_gen = actor.turn_generation
        # Task #369: fresh gen — clean up any stale commit-lock
        # entries.  The new turn starts with an empty slot.  We do NOT
        # claim immediately — the fastpaths + brain dispatch below
        # each claim before firing, and the FIRST caller wins.
        self._clear_response_commits_before(turn_gen)

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
        #
        # 2026-08-23 CAab964e92 fix: caller confirmations that repeat
        # agent-mentioned values verbatim can trip the echo detector.
        # Trace: agent listed "nine forty five am" as a slot; caller
        # said "Yeah. Nine forty five AM." — 4/5 words matched agent
        # utterance with only "yeah" as novel word → 80% coverage +
        # novel≤1 → echo drop. But confirmations are NOT echo. Skip
        # the echo filter when the transcript starts with an
        # acknowledgment token (yeah/yes/sure/no/etc.) — real mic-echo
        # of the agent's audio never starts with those.
        if (
            not _starts_with_ack(transcript)
            and _looks_like_agent_echo(transcript, self._recent_agent_utterances)
        ):
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
            if not self._pending_k1_hint and not settings.response_cache_bypass:
                try:
                    from packages.response_cache import get_shared_response_cache
                    _rcache = get_shared_response_cache()
                    # 2026-08-24 CAff590033 fix: async wrapper.
                    _cache_hit = await _rcache.aget(business_id, self.tenant_id, transcript)
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
            # 2026-08-24 ChatGPT audit CRITICAL fix: previously called
            # `session_manager.run_user_turn(state, brain, transcript)`
            # WITHOUT `on_delta`, forcing brain.py's `_stream_ok` to
            # False → batch LLM mode. The full LLM response was awaited
            # before TTS started, discarding all the SentenceBuffer +
            # streaming-TTS infrastructure we built. Only speculative
            # (Eager EndOfTurn hit) turns actually streamed; every
            # confirmed-Final turn went batch. Verified in CA34075f5b6
            # log — turn 0 shows `stream-brain` + LLM_STREAM_START +
            # TTS_SENTENCE_QUEUED (streaming), turn 1 shows only
            # `brain-job` (batch, no LLM_STREAM_START, no
            # TTS_SENTENCE_QUEUED). ChatGPT's estimate: 150-400ms per
            # turn savings + kills the p95 "why does this turn feel
            # slower than that one" variance.
            #
            # Fix: delegate the actual brain/TTS work to
            # `_run_brain_streaming` which passes on_delta → streams
            # tokens as sentences → pumps to TTS incrementally. That
            # path also handles response-cache write, gate, fake-
            # booking guard, LEAKED_TOOL_JSON detection, etc.
            #
            # When streaming succeeds, `_run_brain_streaming` emits
            # the reply directly via TTS — we skip the batch-mode
            # `brain_completed → _speech_job` handoff. If streaming
            # fails or is unavailable, we fall back to the batch call
            # below.
            #
            # `_run_brain_streaming` needs a span; open one for the turn.
            # _open_turn_span sets self._current_turn_span as side effect.
            self._open_turn_span(turn_gen)
            _span = self._current_turn_span
            _streamed_ok = False
            try:
                await self._run_brain_streaming(
                    state, brain, transcript, turn_gen, _span,
                )
                _streamed_ok = True
            except Exception as _stream_err:
                log.warning(
                    "brain-job stream path failed, falling back to batch: %s",
                    _stream_err,
                )
                _streamed_ok = False
            finally:
                if not _filler_task.done():
                    _filler_task.cancel()

            if _streamed_ok:
                # Streaming path handled reply + TTS + cache write.
                # Nothing left to do — return before batch-mode fallback.
                return

            # Fallback: batch-mode brain call (legacy path).
            # LK steal #7 wire.
            self._stage_state_for_brain_dispatch(state)
            payload = await session_manager.run_user_turn(state, brain, transcript)
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
            # 2026-08-22 NET Ship 2: also suppress on may_be_partial turns
            # (same rationale as the streaming path — see comment there).
            _is_partial_turn = turn_gen in self._may_be_partial_turns
            if _is_partial_turn:
                log.info(
                    "RESPONSE_CACHE_BATCH_SKIP call=%s gen=%d reason=may_be_partial "
                    "input=%r",
                    self.call_id, turn_gen, transcript[:60],
                )
                self._may_be_partial_turns.discard(turn_gen)
            elif reply and not tool_results and not escalated:
                try:
                    from packages.response_cache import get_shared_response_cache
                    # 2026-08-24 CAff590033 fix: async wrapper.
                    await get_shared_response_cache().aput(
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
        silent lane (never synthesise fresh — defeats latency point).

        2026-08-13 (R4 P0): fixed cache-key mismatch.  Writes via
        TTSCacheWrapper.synthesize hash with the INNER provider name
        ("elevenlabs"), but reads here were hashing with the wrapper
        name ("elevenlabs+cache") because the wrapper overrides
        `.name`.  Every reactive lookup missed — confirmed in
        Abdullah's call log 18:53:54 + 18:54:22 (MISS for phrases
        that ARE in DEFAULT_FILLERS + warmed at boot).  Unwrap to
        the inner provider before extracting name/voice/format so
        keys match writes exactly."""
        from packages.tts_cache.cache import get_shared_cache, _hash_key
        from app.routes.twilio import _get_telephony_tts

        tts = _get_telephony_tts()
        # Unwrap the TTSCacheWrapper — synthesize() hashes with the
        # inner provider's attrs, so reads must do the same.
        inner = getattr(tts, "_inner", tts)
        voice = getattr(inner, "default_voice", "default")
        fmt = getattr(inner, "output_format", "unknown")
        provider = getattr(inner, "name", "tts")

        key = _hash_key(voice, phrase, fmt, provider)
        cache = get_shared_cache()
        hit = await cache.get(key)
        if hit is None:
            log.warning("reactive backchannel cache MISS for %r (voice=%s fmt=%s provider=%s) — degrading to silent",
                        phrase, voice, fmt, provider)
            return False
        audio, mime = hit
        log.info("reactive backchannel HIT: %r (%d bytes)", phrase, len(audio))
        # 2026-08-21 NET-13/14: label this audio source as filler so
        # tts_first_frame_wire span is NOT touched (that's answer-only)
        # and the FIRST40 mark is tagged FIRST40_FILLER_<n>.
        # 2026-08-23 AUDIT-S2: arm FIRST40 gate for filler.  Single-shot
        # send emits one mark; no per-chunk noise (filler buffers are
        # already whole in cache).
        self._first40_pending["filler"] = True
        self._first_media_pending["filler"] = True
        await self._send_audio_frames(audio, mime, source="filler")
        return True

    async def _play_filler_on_slow_brain(self, turn_gen: int) -> None:
        """2026-08-08 task #271: play a cached filler ('one sec', 'let me check')
        if the brain hasn't returned within FILLER_DELAY_MS.  Fires as
        a background task; the caller cancels it when the brain returns.

        Uses the pre-warmed filler pool (packages/voice/filler.py) so the
        audio is instant off disk — no synth latency + no additional LLM
        cost.  Only plays ONCE per turn (not looped) to avoid stepping
        on the real reply.

        2026-08-18: (a) bumped delay 1200→1500ms so the filler doesn't
        step on the caller's last word / clip acknowledgments; (b) route
        phrase selection through FillerPool.pick() so the recency-avoid
        logic actually applies — previously we did random.choice on
        DEFAULT_FILLERS and hit the same phrase back-to-back regularly.
        2026-08-21: 1500 → 700ms. Post-Flux + gpt-4.1-nano the LLM
        first-token was 1348ms on the last real call (CAb499d5f), so
        filler NEVER fired at 1500ms. 700ms catches every LLM turn
        that would otherwise feel silent to the caller. Filler audio
        is ~400-600ms (from pre-warmed cache), so total perceived first
        audio = 700ms (filler start) → immediate acknowledgment, then
        the real reply streams behind. If LLM comes back before 700ms
        the filler is cancelled and never plays.

        2026-08-21 NET-03: 700 → 1200ms. Audit found filler + real
        answer both call _send_audio_frames() with no arbiter, so at
        700ms they overlap on the Twilio wire (observed on CA83f5a9e:
        filler starts 179ms before real answer TTS). That interleaving
        is the "voice breaky" complaint. 1200ms buys enough gap for the
        real answer to arrive in most turns (LLM 1300-1500ms first
        token) and prevents the collision.

        2026-08-21 (b): networking chat shipped `_outbound_audio_lock`
        so filler + answer now serialize at the wire even if they
        overlap in time. That means the delay no longer has to hide
        the collision. Drop back to 600ms so filler actually catches
        the common ~1.3-1.5s LLM turn and masks the dead air, without
        the earlier overlap risk. Lock ensures the answer will not
        interleave; if the answer's first-byte arrives while filler
        is still on the wire, the lock naturally queues the answer
        until filler completes.

        2026-08-21 (c) REVERT: 600 → 1200ms. Real-call verification
        on CA5a1ce466 showed filler fired 7 times in a 2.5-minute call
        (every LLM turn), because normal Karachi→OpenAI first-token is
        1200-1500ms which crossed 600ms every time. User called it
        "weird — Gotcha, one second before every reply". Audit §NET-03
        had already warned: "Do not 'always acknowledge immediately.'
        That adds another conversational move and can make the agent
        more robotic." 1200ms restores slow-brain-only behavior:
        filler only fires when LLM genuinely stalls past ~1.2s. Later
        pass: speech-act aware suppression (no filler for ACK/CLARIFY,
        only fire on TOOL_CALL turns).

        2026-08-21 (d) DISABLED: user report on CA792b1dcfb53d — "it
        always starts with ok or some generic phrase first and then
        the real response". Even 1200ms was tripping on every Karachi
        LLM turn because network + prompt-cache-miss consistently
        exceeded the threshold. User explicitly asked to kill it. Set
        threshold to 60000ms so it never fires in normal operation —
        effectively OFF. Speech-act gate (only fire on tool-call
        turns) is the correct long-term fix but requires reading
        speech_act tag at brain-start time; deferring to next batch."""
        FILLER_DELAY_MS = 60000
        try:
            await asyncio.sleep(FILLER_DELAY_MS / 1000.0)
        except asyncio.CancelledError:
            return  # brain returned in time — no filler needed
        if self.actor is None or self.actor.turn_generation != turn_gen:
            return  # turn moved on
        # Pull a phrase from the shared pool — its pick() avoids the
        # last few picks so the same filler doesn't fire back-to-back.
        try:
            from packages.voice.filler import get_pool, DEFAULT_FILLERS
            clip = get_pool().pick()
            phrase = clip.text if clip is not None else DEFAULT_FILLERS[0]
        except Exception:
            phrase = "One second."
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

    async def _run_brain_from_text(
        self,
        transcript: str,
        turn_gen: int,
        *,
        owns_lock: bool = False,
    ) -> None:
        """Streaming-path brain execution.  Same shape as _run_brain
        but skips the WAV→STT step (we already have text).

        `owns_lock=True` (new 2026-08-19, T4): the caller has already
        claimed the commit-lock for `turn_gen` and is passing ownership
        DOWN into this function — do NOT re-claim (that would SKIP
        because the slot is held by our own caller).  Used by the
        speculative EAGER_END_OF_TURN dispatcher, which claims the
        lock as `reason=speculative` BEFORE spawning us; before this
        parameter existed, the re-claim always failed and the entire
        speculative path — including its fastpath / response-cache
        prelude — was dead code, forcing every LLM turn through the
        much slower `_brain_job` fallback (adds ~800ms + filler)."""
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
            log.info("stream-brain %s turn=%d heard: %s owns_lock=%s",
                     self.session_id, turn_gen, transcript, owns_lock)

            # 2026-08-21: fire the same slow-brain filler that _brain_job
            # spawns. Docstring said "streaming path skips ~800ms filler"
            # but on real PK calls (CAd6d8c1) LLM first_tok=2141ms with
            # NO filler → caller heard ~3s of silence. The filler's own
            # 700ms delay + cancellation-on-brain-return still lets fast
            # LLM turns skip it. This is the fastpath's silent-audio fix.
            _filler_task = asyncio.create_task(
                self._play_filler_on_slow_brain(turn_gen),
                name=f"filler-stream-{self.call_id}-{turn_gen}",
            )

            # R3 P2: structured-input capture takes precedence.  When a
            # slot session is open (e.g. booking flow asked for phone),
            # STT finals feed the session, not the LLM.  The session
            # decides when to release control back to normal brain flow
            # via its on_commit callback.
            if self.slot_capture_active:
                consumed = await self._feed_active_slot(
                    transcript, turn_gen, source=SlotSource.SPEECH,
                )
                if consumed:
                    if not _filler_task.done():
                        _filler_task.cancel()
                    return

            handle = session_manager.get_session(
                self.session_id, tenant_id=self.tenant_id,
            )
            if handle is None:
                state, brain = session_manager.start_session_with_id(
                    self.session_id, tenant_id=self.tenant_id,
                )
            else:
                state, brain = handle

            # 2026-08-13 (double-brain fix): dedupe fragment→full re-fires.
            # If Deepgram already emitted a prefix of this transcript and
            # we spoke a reply for it, drop this superset — it would just
            # stack a second reply.
            if self._should_dedupe_dispatch(transcript):
                return
            # Task #369: enforce one-gen-one-commit.  If another
            # dispatch (usually a HITted speculative) already claimed
            # this generation, we are the redundant fire — bail.  This
            # is the definitive guard against Abdullah's gen=20 same-
            # gen double dispatch; the transcript-based dedupe above
            # catches text-repeats but not fresh transcripts on the
            # same gen slot.
            #
            # 2026-08-19 (T4): if the CALLER already owns the lock
            # (owns_lock=True from the speculative dispatcher), skip
            # the re-claim.  Without this branch every speculative
            # dispatch was self-vetoing because the caller had just
            # claimed reason=speculative, and this re-claim reason=
            # run_brain would SKIP → the entire fastpath/cache/streaming
            # prelude below was dead code on the speculative path.
            if not owns_lock:
                if not self._try_claim_response_commit(
                    turn_gen, reason="run_brain",
                ):
                    return
            self._mark_dispatched(transcript)
            # R5 P0: start the stall timer.  Cleared by any downstream
            # response signal (fastpath hit, TTS start, etc.).  Watchdog
            # fires TURN_STALLED at ERROR if it stays stamped >3s.
            self._stamp_turn_committed(transcript, turn_gen)

            # 2026-08-13 (A1 patch): conversation-control fastpath FIRST.
            # Deterministic intents ("can you hear me", "hello", "are you
            # there") skip both the LLM and the response cache — canonical
            # replies are warmed into the TTS disk cache at boot.
            if await self._try_conversation_control_fastpath(
                transcript, turn_gen,
            ):
                return

            # 2026-08-13 (ChatGPT audit fix): response-cache fastpath.
            # Streaming path used to bypass the response cache entirely,
            # forcing "Hello can you hear me" through a 2.6s OpenAI call
            # every time.  Check cache BEFORE any LLM dispatch.
            if await self._try_response_cache_fastpath(
                state, transcript, turn_gen,
            ):
                return

            # Task #283: streaming LLM→TTS branch when eligible.
            if span is not None:
                span.mark("brain_start")
            if self._streaming_llm_eligible(brain):
                if span is not None:
                    span.mark("streaming_path")
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

            # LK steal #7 wire.
            self._stage_state_for_brain_dispatch(state)
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
        finally:
            # Always cancel filler on exit — whether real reply spoke,
            # turn got cancelled, or brain failed. Without this the
            # filler could still fire AFTER the real reply.
            try:
                if not _filler_task.done():
                    _filler_task.cancel()
            except NameError:
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

        # 2026-08-22 NET Ship 1: wait briefly for STT_FINAL to catch up.
        # Regression on CAa7effd6273 gen=3→4: interruption fired on a
        # PARTIAL ("the general appointment") 160ms BEFORE the FINAL
        # arrived. LLM then re-answered the price question in context
        # of the partial, and the RESPONSE_CACHE wrote that answer keyed
        # on the partial text. When the FINAL later promoted to
        # END_OF_TURN, cache HIT and replayed the same answer — user
        # heard the same reply three times ("the ne-... the new patient
        # exam... the new patient exam...").
        # Fix: poll self._streaming_utterance_text (which _on_stt_final
        # populates) for up to 200ms.  If a longer/different final
        # arrives in that window, use it.  If not, we tag the dispatch
        # `may_be_partial=True` so Ship 2 can suppress the cache write.
        _initial_text = (text or "").strip()
        _may_be_partial = True  # assume partial until we see a final
        try:
            import time as _t
            _wait_start = _t.perf_counter()
            _wait_budget_s = 0.200
            _initial_streaming = (self._streaming_utterance_text or "").strip()
            while (_t.perf_counter() - _wait_start) < _wait_budget_s:
                await asyncio.sleep(0.025)
                _current = (self._streaming_utterance_text or "").strip()
                # A newer/longer streaming buffer implies a fresher
                # STT partial/final has landed since the barge.
                if _current and _current != _initial_streaming and len(_current) >= len(_initial_text):
                    text = _current
                    _may_be_partial = False
                    log.info(
                        "INTERRUPT_WAITED_FOR_FINAL call=%s waited_ms=%.0f "
                        "initial=%r final=%r",
                        self.call_id,
                        (_t.perf_counter() - _wait_start) * 1000,
                        _initial_text[:60], text[:60],
                    )
                    break
        except Exception as _e:
            log.debug("interrupt wait-for-final skipped: %s", _e)
        if _may_be_partial:
            log.info(
                "INTERRUPT_USING_PARTIAL call=%s text=%r (no final within 200ms; "
                "cache write will be suppressed)",
                self.call_id, _initial_text[:60],
            )

        # Run the brain on the interruption text as the next real turn
        if text and text.strip():
            self._open_turn_span(actor.turn_generation)
            if self._current_turn_span is not None:
                self._current_turn_span.mark("media_in")
                self._current_turn_span.mark("stt_final")
            turn_gen = actor.turn_generation
            # Ship 2: tag the pending turn as "may-be-partial" so the
            # streaming brain completion path skips cache-write.
            self._may_be_partial_turns.add(turn_gen)
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

    # ── 2026-08-21 NET-14-followup: TTS stream watchdog ──────────────
    #
    # Every TTS_STREAM_START calls _tts_stream_open(...).  Every
    # TTS_STREAM_DONE calls _tts_stream_close(...).  If the paired
    # DONE never arrives within (spoken_duration + 2s grace), the
    # watchdog task fires WARN with elapsed_s + state hint.  Catches:
    #   - farewell-hangup killing TTS mid-stream (CA3e014ab8 pattern)
    #   - upstream provider WS drop during synth
    #   - cancel/reconnect leaking a stream we forgot to close
    #   - any future variant where audio starts but never finishes
    # Pure telemetry: no behavior change, no test flake surface.

    def _tts_stream_open(
        self, gen: int, source: str, text: str, transport: str,
    ) -> str:
        """Open a TTS stream tracker + spawn a watchdog.  Returns
        stream_id; caller MUST pass it to _tts_stream_close on any
        exit path (normal completion, exception, cancellation)."""
        import asyncio as _aio
        import time as _t
        self._tts_stream_counter += 1
        stream_id = f"{gen}_{self._tts_stream_counter}"
        # Deadline heuristic: spoken duration ≈ 75ms per character
        # (13 chars/sec is typical for TTS at conversational pace) +
        # 2s grace for TTS-first-byte + Twilio jitter.  Floor 4s so
        # very short replies don't spuriously alarm.  Ceiling 60s so
        # a bug in the timer itself can't hold a phantom task forever.
        deadline_s = max(4.0, min(60.0, len(text) * 0.075 + 2.0))
        entry = {
            "started_at": _t.perf_counter(),
            "text": text[:80],
            "source": source,
            "transport": transport,
            "gen": gen,
            "deadline_s": deadline_s,
        }
        try:
            entry["watchdog_task"] = _aio.create_task(
                self._tts_stream_watchdog(stream_id, deadline_s),
                name=f"tts-watchdog-{self.call_id}-{stream_id}",
            )
        except RuntimeError:
            # No running loop (defensive; should not happen in actor path)
            entry["watchdog_task"] = None
        self._tts_open_streams[stream_id] = entry
        return stream_id

    def _tts_stream_close(self, stream_id: Optional[str]) -> None:
        """Close a TTS stream tracker + cancel its watchdog.  Idempotent:
        None/unknown stream_id is a no-op (safe for cancellation paths
        where we might close twice)."""
        if not stream_id:
            return
        entry = self._tts_open_streams.pop(stream_id, None)
        if entry is None:
            return
        watchdog = entry.get("watchdog_task")
        if watchdog is not None and not watchdog.done():
            watchdog.cancel()

    async def _tts_stream_watchdog(
        self, stream_id: str, deadline_s: float,
    ) -> None:
        """Sleep for deadline_s.  If the stream is still open, log a
        TTS_STREAM_HUNG WARN with the elapsed time + a state hint.
        Cancellation (from _tts_stream_close on normal completion) is
        the healthy path — swallow silently."""
        import asyncio as _aio
        import time as _t
        try:
            await _aio.sleep(deadline_s)
        except _aio.CancelledError:
            return
        entry = self._tts_open_streams.get(stream_id)
        if entry is None:
            # Closed just before the timer fired — race is fine.
            return
        elapsed_ms = (_t.perf_counter() - entry["started_at"]) * 1000
        state_hint = (
            self.actor.state.value if (self.actor and self.actor.state)
            else "unknown"
        )
        log.warning(
            "TTS_STREAM_HUNG call=%s stream_id=%s gen=%s source=%s transport=%s "
            "elapsed_ms=%.0f deadline_ms=%.0f state=%s text=%r",
            self.call_id, stream_id, entry.get("gen"), entry.get("source"),
            entry.get("transport"), elapsed_ms, deadline_s * 1000,
            state_hint, entry.get("text"),
        )
        # Leave the entry in place so a late DONE still cleans it — but
        # cancel the (already-fired) watchdog reference for tidiness.
        entry["watchdog_task"] = None

    def _tts_close_all_streams(self, reason: str) -> None:
        """Bulk-close all open TTS stream trackers.  Used from bump_turn
        / bump_speech / stop cleanup paths where the actor has
        cancelled TTS wholesale — leaving open trackers would fire
        spurious HUNG warnings."""
        if not self._tts_open_streams:
            return
        stream_ids = list(self._tts_open_streams.keys())
        for sid in stream_ids:
            entry = self._tts_open_streams.pop(sid, None)
            if entry is None:
                continue
            watchdog = entry.get("watchdog_task")
            if watchdog is not None and not watchdog.done():
                watchdog.cancel()
        log.info(
            "TTS_STREAMS_BULK_CLOSED call=%s reason=%s closed=%d",
            self.call_id, reason, len(stream_ids),
        )

    async def _send_audio_frames(
        self, audio_bytes: bytes, mime: str, *, source: str = "answer",
    ) -> None:
        """Stream audio out to the transport.

        For Twilio-format calls (stream_sid starts with 'MZ' or the ws
        is a real Twilio Media Streams socket): downsample to µ-law 8kHz
        and send in 20ms frames.

        For browser-format calls (stream_sid starts with 'browser_'):
        send raw PCM base64 with a rate marker; widget plays at native
        rate.  Zero encoding loss.

        Sprint 9f duck logic + gain logic preserved for the Twilio path.

        2026-08-21 NET-03: holds `self._outbound_audio_lock` for the whole
        send.  Prevents filler + real answer + greeting from interleaving
        JSON media frames on the wire when they fire concurrently.  If a
        second sender is queued behind, it waits for the first to finish
        the entire buffer.  Cost: filler that misses its window won't
        step on the answer; the answer plays clean end-to-end.

        2026-08-21 NET-13/14: `source` labels the audio origin so the
        FIRST40 mark and tts_first_frame_wire span can distinguish
        filler audio from real answer audio.  Values: "answer" (LLM
        reply / cache hit / greeting — the caller-visible content),
        "filler" (backchannel while brain runs), "greeting" (opening
        line, first audio of the call).  Only "answer" writes to
        `tts_first_frame_wire` so TURN_SUMMARY no longer produces
        negative deltas when filler beats answer to the wire."""
        async with self._outbound_audio_lock:
            await self._send_audio_frames_locked(audio_bytes, mime, source=source)

    async def _send_audio_frames_locked(
        self, audio_bytes: bytes, mime: str, *, source: str = "answer",
    ) -> None:
        """Body of _send_audio_frames, run under _outbound_audio_lock."""
        # 2026-08-12: record wire-time on FIRST send of the turn so the
        # TURN_SUMMARY line shows the tts_first_byte → wire gap.  First-
        # write-wins in the span means subsequent frames don't overwrite.
        # 2026-08-21 NET-14: only the ANSWER should populate this mark.
        # Filler firing first would lock in a wire time BEFORE the real
        # answer TTS started, producing negative `wire_first_frame` in
        # TURN_SUMMARY.
        if source == "answer" and self._current_turn_span is not None:
            self._current_turn_span.mark("tts_first_frame_wire")

        # 2026-08-13 (R1): stamp last-wire-send so the zombie-SPEAKING
        # watchdog can tell active speech from a stuck lifecycle.
        self._last_wire_send_at = time.monotonic()

        is_browser = self.stream_sid.startswith("browser_")

        if is_browser:
            await self._send_browser_pcm_frames(audio_bytes, mime)
            return

        # ----- Twilio path: encode PCM → µ-law 8kHz at the wire -----
        from app.routes.twilio import _tts_bytes_to_mulaw
        mulaw = _tts_bytes_to_mulaw(audio_bytes, mime)

        # 2026-08-31 task #104-followup: tee outbound agent audio into
        # the recorder BEFORE the gain-adjust so listeners hear roughly
        # what the caller hears. Gain is a wire-tuning knob per install
        # and it's fine if the recording is un-gained.
        if self.recorder is not None:
            self.recorder.append_agent(mulaw)

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
            # 2026-08-13 (M1 task #343): the FIRST40 mark goes right
            # after the first 2 frames (40ms) of audio.  Twilio fires
            # `mark` when preceding audio has played out, so ack-wall
            # minus send-wall is the true wire-to-ear latency for THIS
            # reply.  Sent once per _send_audio_frames call — this is
            # the natural "start of a reply" boundary the caller cares
            # about.  See docs/rnd-2026-08/54-chatgpt-audit-response.md
            # (audit #3): "make cached reply the first 40 ms + mark".
            # 2026-08-21 NET-13: mark ID now includes `source` so we can
            # tell in the log whether the ACK is for filler audio or the
            # real answer.  Two ACKs per turn (filler+answer) used to
            # appear as `FIRST40_1` and `FIRST40_2` with no way to
            # distinguish; now they're `FIRST40_FILLER_N` /
            # `FIRST40_ANSWER_N`.
            # 2026-08-23 AUDIT-S2: the "one FIRST40 per _send_audio_frames
            # call" model is wrong for streaming — `_stream_tts_incremental`
            # calls this method PER EL CHUNK, so a 20-chunk answer emitted
            # 20 FIRST40_ANSWER_N marks with only the first being meaningful.
            # Now gated on `_first40_pending[source]` which is armed
            # externally at reply-boundary (see armers in
            # `_stream_tts_incremental`, cache fastpath, and
            # `_play_cached_backchannel`) and cleared here after the mark
            # is sent.  Batch (non-streaming) paths still get the same
            # mark because they arm-then-send in one shot.
            first40_frames_needed = 2  # 2 * 20ms = 40ms
            first40_mark_id: Optional[str] = None
            # Consume-and-clear atomically so no two chunks race on the flag.
            first40_should_send = self._first40_pending.pop(source, False)
            # 2026-08-23 ChatGPT audit measurement correction: emit
            # TWILIO_FIRST_MEDIA_SENT the instant we WRITE the first
            # media frame to the Twilio WSS. This is closer to actual
            # ear latency than TWILIO_FIRST40_ACK (which requires a
            # Twilio→Karachi return trip the caller doesn't wait for).
            # Formula for realistic perceived latency:
            #   caller_hears ≈ STT_FINAL → TWILIO_FIRST_MEDIA_SENT
            #                 + (Karachi→Twilio wire ~150ms one-way)
            #                 + Twilio→carrier→ear (~200ms typical)
            # Whereas TWILIO_FIRST40_ACK includes the return trip too.
            #
            # 2026-08-24 CAff590033 fix: was emitting per FRAME (once
            # per 20ms) because streaming TTS calls _send_audio_frames
            # per chunk, and my per-call local flag was re-init'd each
            # call. Same design bug as pre-fix FIRST40. Now gated on
            # session-level _first_media_pending, armed at reply-
            # boundary by same call sites that arm _first40_pending.
            first_media_should_send = self._first_media_pending.pop(source, False)
            first_media_sent = False
            for i in range(0, len(mulaw), frame_bytes):
                chunk = mulaw[i:i + frame_bytes]
                await self.ws.send_text(json.dumps({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(chunk).decode("ascii")},
                }))
                if first_media_should_send and not first_media_sent:
                    first_media_sent = True
                    log.info(
                        "TWILIO_FIRST_MEDIA_SENT call=%s source=%s bytes=%d "
                        "(closer to ear latency than FIRST40_ACK)",
                        self.call_id, source, len(chunk),
                    )
                frames_in_batch += 1
                # Send the FIRST40 mark after the second frame lands.
                # Gate on `first40_should_send` (armed at reply-boundary)
                # so streaming answers only emit ONE mark per answer, not
                # one per EL chunk. See _first40_pending init.
                if (
                    first40_should_send
                    and first40_mark_id is None
                    and (i // frame_bytes) + 1 >= first40_frames_needed
                ):
                    self._first40_counter += 1
                    first40_mark_id = f"FIRST40_{source.upper()}_{self._first40_counter}"
                    self._first40_send_wall[first40_mark_id] = time.monotonic()
                    try:
                        await self.ws.send_text(json.dumps({
                            "event": "mark",
                            "streamSid": self.stream_sid,
                            "mark": {"name": first40_mark_id},
                        }))
                    except Exception:
                        log.debug("FIRST40 mark send failed", exc_info=True)
                    # first40_mark_id is now set → the `is None` gate above
                    # prevents any further FIRST40 sends within this chunk
                    # loop.  Per-answer dedup is handled by _first40_pending.
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
    # R7 (task #355): arrival observability.  Emit WEBSOCKET_CONNECTED
    # the instant we enter this handler.  If the trail cuts off here
    # (no TWILIO_START ever follows), Twilio opened the WS but never
    # sent `start` — usually a TwiML issue.
    from packages.observability import arrival_events as _arr
    _r7_session_key = _arr.new_session_key()
    _r7_remote_ip = ""
    try:
        if getattr(ws, "client", None) is not None:
            _r7_remote_ip = ws.client.host or ""
    except Exception:
        pass
    _arr.websocket_connected(_r7_session_key, remote_ip=_r7_remote_ip)

    session: Optional[TwilioActorSession] = None
    _r7_first_media_seen = False
    try:
        while True:
            raw = await ws.receive_text()
            event = json.loads(raw)
            kind = event.get("event")

            if kind == "connected":
                log.info("actor twilio connected: %s", event.get("protocol"))
                continue

            if kind == "start":
                start_payload = event["start"]
                stream_sid = start_payload["streamSid"]
                call_sid = (start_payload.get("callSid")
                            or f"call_{uuid.uuid4().hex[:8]}")
                # R3 P3 (task #370): Twilio delivers ANI/DNIS via the
                # customParameters bag when TwiML sets <Parameter>.
                # Keys are the "name" attribute; values are the
                # already-expanded {{From}}/{{To}} strings.  Missing =
                # empty string (blocked caller ID or older TwiML).
                custom = start_payload.get("customParameters") or {}
                caller_number = (custom.get("from") or "").strip()
                dialed_number = (custom.get("to") or "").strip()
                caller_name = (custom.get("callerName") or "").strip()
                # R7: emit TWILIO_START.  Binds session_key -> CallSid
                # so log searches by CallSid can walk back through the
                # arrival chain.
                _arr.twilio_start(
                    _r7_session_key,
                    call_sid=call_sid,
                    stream_sid=stream_sid,
                    account_sid=start_payload.get("accountSid") or "",
                    from_number=caller_number,
                    to_number=dialed_number,
                )
                session = TwilioActorSession(
                    ws=ws,
                    stream_sid=stream_sid,
                    call_id=call_sid,
                    tenant_id=tenant_id,
                    caller_number=caller_number,
                    dialed_number=dialed_number,
                    caller_name=caller_name,
                )
                log.info(
                    "actor twilio start: %s (%s) caller=%r dialed=%r name=%r",
                    call_sid, stream_sid,
                    caller_number or "-",
                    dialed_number or "-",
                    caller_name or "-",
                )
                await session.start()
                continue

            if kind == "media" and session is not None:
                mulaw = base64.b64decode(event["media"]["payload"])
                # R7 (task #355): FIRST_MEDIA on the very first frame.
                # If this NEVER fires despite TWILIO_START, mic is
                # muted or Media Streams region can't reach the carrier.
                if not _r7_first_media_seen:
                    _r7_first_media_seen = True
                    _arr.first_media(
                        _r7_session_key,
                        call_sid=session.call_id,
                        stream_sid=session.stream_sid,
                        frame_bytes=len(mulaw),
                    )
                # 2026-08-13 (M1 task #343): honest cadence-skew metric.
                # cadence_skew_ms = local_elapsed - twilio_elapsed
                # where both elapsed values are measured against the same
                # frame's arrival (first frame's wall + first frame's
                # carrier timestamp).  Answers: "is the receive loop
                # keeping up with the sender in real time?"  A steady
                # ~0ms skew means we're on the pace; positive skew that
                # grows means our loop is stalling; negative skew means
                # Twilio is bursting media faster than real-time (their
                # buffer is flushing).
                # Also handler_ms: time inside on_media() — catches sync
                # work still blocking the receiver post-P0-startup.
                if session._twilio_media_debug_count < settings.twilio_media_timestamp_debug_frames:
                    import time as _t
                    media = event.get("media", {})
                    try:
                        media_ts = int(media.get("timestamp", -1))
                    except (TypeError, ValueError):
                        media_ts = -1
                    recv_wall = _t.monotonic()
                    if session._twilio_base_wall is None and media_ts >= 0:
                        session._twilio_base_wall = recv_wall
                        session._twilio_base_media_ts = media_ts
                    if (
                        session._twilio_base_wall is not None
                        and session._twilio_base_media_ts is not None
                        and media_ts >= 0
                    ):
                        local_elapsed_ms = int((recv_wall - session._twilio_base_wall) * 1000)
                        twilio_elapsed_ms = media_ts - session._twilio_base_media_ts
                        skew_ms = local_elapsed_ms - twilio_elapsed_ms
                    else:
                        local_elapsed_ms = -1
                        twilio_elapsed_ms = -1
                        skew_ms = -1
                    session._twilio_media_debug_count += 1
                    handler_start = _t.perf_counter()
                    await session.on_media(mulaw)
                    handler_ms = int((_t.perf_counter() - handler_start) * 1000)
                    log.info(
                        "TWILIO_MEDIA call=%s n=%d track=%s chunk=%s "
                        "twilio_elapsed=%dms local_elapsed=%dms skew=%dms "
                        "handler_ms=%d bytes=%d",
                        session.call_id,
                        session._twilio_media_debug_count,
                        media.get("track"),
                        media.get("chunk"),
                        twilio_elapsed_ms,
                        local_elapsed_ms,
                        skew_ms,
                        handler_ms,
                        len(mulaw),
                    )
                else:
                    await session.on_media(mulaw)
                continue

            if kind == "mark" and session is not None:
                mark_name = event.get("mark", {}).get("name")
                if mark_name:
                    await session.on_mark_ack(mark_name)
                continue

            # R3 P3 (task #370): Twilio DTMF event during Media Streams.
            # Payload shape (per Twilio docs):
            #   {"event":"dtmf","streamSid":"MZ...","sequenceNumber":"N",
            #    "dtmf":{"track":"inbound_track","digit":"5"}}
            # Only 0-9 * # #A #B #C #D are legal digits.  Delivered as
            # ONE event per keypress — batching (rapid keypad entry) is
            # our problem, not Twilio's.
            if kind == "dtmf" and session is not None:
                dtmf = event.get("dtmf") or {}
                digit = str(dtmf.get("digit") or "").strip()
                track = dtmf.get("track") or "inbound_track"
                if digit:
                    await session.on_dtmf(digit, track=track)
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
