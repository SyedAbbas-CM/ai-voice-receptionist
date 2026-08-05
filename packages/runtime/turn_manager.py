"""Turn Manager (Sprint 10 C2).

The audit's finding:
    Cloned-voice quality breaks when the agent interrupts wrong,
    speaks over the caller, treats 'mhm' as a new request, or
    restarts the whole answer after interruption.  Turn-taking is the
    real differentiator.

Fix: one authoritative subsystem that consumes STT events + emits
SEMANTIC turn events.  Everything else (barge-in, brain triggering,
duck/unduck) subscribes to those.

Event taxonomy (verbatim from moat doc line 335 + audit-2 §Turn Manager):

    EAGER_END_OF_TURN
        First stable final hypothesis.  Actor may start speculative
        LLM/RAG work.  If the caller keeps talking, TURN_RESUMED fires
        and the speculative work gets cancelled.

    TURN_RESUMED
        Caller kept talking after eager end.  Cancel any speculative
        speech generation; wait for the real END_OF_TURN.

    END_OF_TURN
        True end.  Commit caller's utterance; run the brain for real.

    BACKCHANNEL
        Short utterance during agent speech: 'yeah', 'mm-hm', 'right'.
        Agent keeps talking.

    INTERRUPTION
        Real barge-in with content.  Cancel current speech; open new
        caller turn.

    USER_REQUESTED_PAUSE
        Caller said 'hold on', 'give me a second', 'wait'.  Agent
        stays silent; do NOT respond with 'sure!'.

    FALSE_INTERRUPTION
        VAD triggered but no stable partial materialized (noise/TV).
        Un-duck; resume playback.

Kept transport-agnostic — consumes STT events + agent SPEAKING state,
emits CONTROL events into the same actor mailbox.  Downstream
handlers (TwilioActorSession's _on_barge_candidate) subscribe.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .call_actor import CallActor, CallState
from .call_event import CallEvent, EventSource

log = logging.getLogger(__name__)


class TurnEventKind(str, Enum):
    """Semantic turn events emitted by the manager."""
    EAGER_END_OF_TURN = "eager_end_of_turn"
    TURN_RESUMED = "turn_resumed"
    END_OF_TURN = "end_of_turn"
    BACKCHANNEL = "backchannel"
    INTERRUPTION = "interruption"
    USER_REQUESTED_PAUSE = "user_requested_pause"
    FALSE_INTERRUPTION = "false_interruption"


# ── classification patterns ─────────────────────────────────────────

# Very short + purely acknowledging utterances = backchannel.
_BACKCHANNEL_TOKENS = frozenset({
    "yeah", "yes", "yep", "mm-hm", "mmhm", "uh-huh", "uhuh", "mhm",
    "right", "ok", "okay", "cool", "sure", "gotcha", "got it", "hmm",
    "sounds good", "i see", "makes sense",
})

# Caller wants agent to be quiet + wait.
_PAUSE_PATTERNS = [
    re.compile(r"\b(hold on|hang on|give me (?:a )?(?:sec(?:ond)?|minute|moment))\b",
               re.IGNORECASE),
    re.compile(r"\b(wait|one moment|one sec(?:ond)?|just a moment|just a sec)\b",
               re.IGNORECASE),
    re.compile(r"\b(let me (?:check|see|think|grab|find)|bear with me)\b",
               re.IGNORECASE),
]


def classify_short_utterance(text: str) -> Optional[TurnEventKind]:
    """Return BACKCHANNEL / USER_REQUESTED_PAUSE / None.  Called on
    every stable partial during agent speech."""
    if not text:
        return None
    stripped = text.strip().lower()
    stripped = re.sub(r"[.,!?;:]", "", stripped)
    if stripped in _BACKCHANNEL_TOKENS:
        return TurnEventKind.BACKCHANNEL
    for pat in _PAUSE_PATTERNS:
        if pat.search(text):
            return TurnEventKind.USER_REQUESTED_PAUSE
    return None


# ── config ──────────────────────────────────────────────────────────

@dataclass
class TurnManagerConfig:
    """Tunables — override per-tenant later if needed."""

    # After the first stable final, wait this long for TURN_RESUMED
    # before declaring true END_OF_TURN.  Shorter = snappier response,
    # more risk of interrupting caller.  Longer = safer, more dead air.
    eager_confirm_ms: int = 400

    # Partial deemed 'stable' after this many identical or growing
    # revisions in a row.  Deepgram sends partials ~200ms; 2 stable
    # revisions ≈ 400ms of confidence.
    stable_partial_count: int = 2

    # Minimum partial text length before we consider promoting to
    # INTERRUPTION (during agent speech).  Prevents a single "u"
    # from cutting the agent off.
    interruption_min_chars: int = 3

    # Interruption confirmed after this many stable non-backchannel
    # partials during agent speech.
    interruption_confirm_partials: int = 2

    # False-interruption detection: if we duck on speech_start but no
    # stable partial arrives within this many ms, un-duck.
    false_interruption_deadline_ms: int = 400

    # Fragment buffer flush deadline — how long we hold a fragment
    # (incomplete-looking final) waiting for a completing final to
    # arrive.  If nothing joins within this window, flush as-is so
    # a single-word answer isn't stuck forever.
    # 2500ms tuned for natural mid-sentence pauses in browser demos.
    fragment_flush_ms: int = 2500


# ── state ──────────────────────────────────────────────────────────

@dataclass
class TurnManagerState:
    """Per-call rolling state.  Not thread-safe — Turn Manager runs
    on the actor's single event loop task."""
    last_partial_text: str = ""
    stable_repeats: int = 0
    eager_fired_at_ns: Optional[int] = None
    eager_confirm_task: Optional[asyncio.Task] = None
    saw_speech_start: bool = False
    speech_start_at_ns: Optional[int] = None
    non_backchannel_partial_count: int = 0
    false_interruption_deadline_task: Optional[asyncio.Task] = None
    # Buffered incomplete-sentence fragments (Deepgram fires final on
    # mid-sentence pauses too often; we splice them together until we
    # get a final that actually looks complete).
    pending_fragment: str = ""
    fragment_flush_task: Optional[asyncio.Task] = None


# ── semantic completeness heuristic ────────────────────────────────

_INCOMPLETE_TRAILING_WORDS = frozenset({
    # Conjunctions
    "and", "but", "or", "so", "because", "since", "although", "though",
    "while", "if", "unless", "until", "when", "where", "before", "after",
    # Prepositions
    "of", "to", "for", "with", "on", "in", "at", "by", "from", "about",
    # Auxiliaries + modals mid-clause
    "is", "are", "was", "were", "am", "be", "been", "being",
    "do", "does", "did", "have", "has", "had",
    "will", "would", "shall", "should", "can", "could", "may", "might", "must",
    # Articles + determiners (definitely mid-noun-phrase)
    "the", "a", "an", "my", "your", "his", "her", "their", "our",
    "this", "that", "these", "those",
    # Personal + object pronouns — "can you", "with me", "for us" etc.
    # almost always continue.
    "you", "me", "us", "him", "them", "it", "i", "we", "they", "he", "she",
    # Common trailing fillers
    "um", "uh", "er", "hmm", "like",
    # Relative pronouns
    "which", "who", "whom", "whose", "what",
})


def _ends_on_incomplete_word(text: str) -> bool:
    """Return True if the transcript is unlikely to be a complete
    sentence — Deepgram probably endpointed on a mid-sentence pause
    rather than a real turn boundary.

    We use TWO signals:
      1. Smart-format didn't append terminating punctuation
         (`.` / `?` / `!` / `…`).  Deepgram's smart-format normally
         punctuates a real sentence end; a bare fragment ending on a
         letter is a strong tell that the caller trailed off.
      2. The last word is a conjunction / preposition / article /
         auxiliary / filler.  Belt-and-suspenders even when Deepgram
         did add a period after a fragment.
    """
    stripped = text.strip()
    if not stripped:
        return False
    # Signal 1: no terminating punctuation → almost always incomplete
    if stripped[-1] not in ".?!…":
        return True
    # Signal 2: even with punctuation, catch trailing conjunctions
    import re as _re
    tail = _re.sub(r"[^\w]+$", "", stripped).lower()
    if not tail:
        return False
    last = tail.rsplit(None, 1)[-1] if " " in tail else tail
    return last in _INCOMPLETE_TRAILING_WORDS


# ── main class ─────────────────────────────────────────────────────

class TurnManager:
    """One instance per active call.  Reads STT events; emits semantic
    turn events into the actor.

    Wiring:
      * TwilioActorSession or browser widget creates TurnManager(actor).
      * Calls tm.on_stt_event(kind, text, is_final) from the STT
        handler (or, when using StreamingSTTBridge, the actor's STT
        event handler dispatches into tm).
      * Downstream barge-in / brain handlers subscribe to
        CONTROL/turn events emitted by the manager.
    """

    def __init__(
        self,
        actor: CallActor,
        config: Optional[TurnManagerConfig] = None,
        clock_ns=None,
    ) -> None:
        self._actor = actor
        self._config = config or TurnManagerConfig()
        self._clock_ns = clock_ns or time.monotonic_ns
        self._state = TurnManagerState()

    # ── inbound STT events ─────────────────────────────────────────

    async def on_stt_event(
        self, kind: str, text: str = "", is_final: bool = False,
        speech_final: bool = False,
    ) -> None:
        """Route one STT event through the state machine.  kind ∈
        {speech_start, speech_end, partial, final}.

        `speech_final` (Deepgram) distinguishes VAD-confirmed
        end-of-utterance from mere endpoint-hit.  Only speech_final
        promotes to END_OF_TURN; is_final-only fragments get buffered."""
        if kind == "speech_start":
            await self._on_speech_start()
            return
        if kind == "speech_end":
            await self._on_speech_end()
            return
        if kind == "partial":
            await self._on_partial(text)
            return
        if kind == "final":
            await self._on_final(text, speech_final=speech_final)
            return
        # stream_failed, unknown — ignore
        return

    # ── individual handlers ────────────────────────────────────────

    async def _on_speech_start(self) -> None:
        """Caller started talking.  If agent is speaking → this is a
        candidate interruption; start the false-interruption deadline
        so we un-duck if no real content arrives."""
        self._state.saw_speech_start = True
        self._state.speech_start_at_ns = self._clock_ns()
        self._state.non_backchannel_partial_count = 0
        # If already ducked from a previous speech_start, don't
        # reschedule the deadline.
        if self._state.false_interruption_deadline_task is not None:
            return
        # Schedule the deadline.  If a partial arrives + gets classified
        # before the deadline, we cancel this task.
        if self._agent_is_speaking():
            self._state.false_interruption_deadline_task = asyncio.create_task(
                self._false_interruption_deadline(),
                name=f"tm-false-int-{self._actor.call_id}",
            )

    async def _on_speech_end(self) -> None:
        """VAD reports caller stopped.  If we had an active partial
        that hadn't been finalized, that's a signal the eager end is
        likely — but we wait for a real 'final' from the STT to fire
        EAGER_END_OF_TURN so we don't act on noise-terminated silence."""
        self._state.saw_speech_start = False
        self._state.speech_start_at_ns = None

    async def _on_partial(self, text: str) -> None:
        """Interim hypothesis.  Two possible reactions depending on
        agent state:

        - Agent LISTENING: track partial stability; used later when
          FINAL fires to decide EAGER vs full END_OF_TURN.
        - Agent SPEAKING: run backchannel/pause classifier.  If
          neither matches AND partial is content-y and stable, promote
          to INTERRUPTION.
        """
        if not text:
            return
        # Track stability
        if text == self._state.last_partial_text:
            self._state.stable_repeats += 1
        else:
            self._state.last_partial_text = text
            self._state.stable_repeats = 0

        if self._agent_is_speaking():
            await self._handle_partial_during_speech(text)

    async def _handle_partial_during_speech(self, text: str) -> None:
        """Backchannel / pause / interruption logic during agent speech."""
        cls = classify_short_utterance(text)
        if cls == TurnEventKind.BACKCHANNEL:
            # Reset false-interruption deadline; don't count as
            # non-backchannel; emit BACKCHANNEL so barge-in unducks.
            self._cancel_false_interruption_deadline()
            self._state.non_backchannel_partial_count = 0
            await self._emit(TurnEventKind.BACKCHANNEL, text=text)
            return
        if cls == TurnEventKind.USER_REQUESTED_PAUSE:
            self._cancel_false_interruption_deadline()
            await self._emit(TurnEventKind.USER_REQUESTED_PAUSE, text=text)
            return
        # Neither backchannel nor pause — count toward interruption
        if len(text) >= self._config.interruption_min_chars:
            self._state.non_backchannel_partial_count += 1
            if self._state.non_backchannel_partial_count >= self._config.interruption_confirm_partials:
                self._cancel_false_interruption_deadline()
                await self._emit(TurnEventKind.INTERRUPTION, text=text)
                self._state.non_backchannel_partial_count = 0

    async def _on_final(self, text: str, speech_final: bool = False) -> None:
        """Final hypothesis.  Promotion to EAGER_END_OF_TURN requires
        EITHER Deepgram's `speech_final=True` (VAD confirmed real
        end-of-utterance) OR a text-completeness check.  Bare
        `is_final=True` (endpoint hit on a mid-sentence pause) buffers
        as a fragment waiting for the completing final.

        If agent is SPEAKING, this final should have already tripped
        the interruption path — treat as a redundant INTERRUPTION."""
        if not text:
            return
        if self._agent_is_speaking():
            await self._emit(TurnEventKind.INTERRUPTION, text=text, is_final=True)
            return

        # If Deepgram says speech_final=False, it's almost certainly a
        # mid-sentence endpoint (comma-pause, filler pause, thinking).
        # Buffer and wait for the real utterance-end final.
        looks_incomplete = _ends_on_incomplete_word(text)
        if not speech_final and looks_incomplete:
            self._state.pending_fragment = (
                (self._state.pending_fragment + " " if self._state.pending_fragment else "")
                + text
            )
            if self._state.fragment_flush_task and not self._state.fragment_flush_task.done():
                self._state.fragment_flush_task.cancel()
            self._state.fragment_flush_task = asyncio.create_task(
                self._flush_pending_fragment(),
                name=f"tm-fragflush-{self._actor.call_id}",
            )
            return
        # Cancel any pending fragment flush — we got a complete final.
        if self._state.fragment_flush_task and not self._state.fragment_flush_task.done():
            self._state.fragment_flush_task.cancel()
            self._state.fragment_flush_task = None
        # Splice buffered fragment (if any) with this complete final.
        if self._state.pending_fragment:
            text = (self._state.pending_fragment + " " + text).strip()
            self._state.pending_fragment = ""

        # Cancel any existing confirmation task (new final supersedes)
        if self._state.eager_confirm_task and not self._state.eager_confirm_task.done():
            self._state.eager_confirm_task.cancel()
            self._state.eager_confirm_task = None

        await self._emit(TurnEventKind.EAGER_END_OF_TURN, text=text, is_final=True)
        self._state.eager_fired_at_ns = self._clock_ns()
        # Reset the speech-start flag so the confirm window only sees
        # NEW speech starts during the confirmation, not the one that
        # already produced this final.  Without this reset, streaming
        # STT paths with active VAD (Deepgram continuous) would perpetually
        # see saw_speech_start=True and never promote to END_OF_TURN.
        self._state.saw_speech_start = False
        # Schedule confirmation
        self._state.eager_confirm_task = asyncio.create_task(
            self._confirm_end_of_turn(text),
            name=f"tm-confirm-{self._actor.call_id}",
        )

    # ── deadlines ──────────────────────────────────────────────────

    async def _flush_pending_fragment(self) -> None:
        """Safety flush.  If a fragment sits without a completing final
        arriving within `fragment_flush_ms`, treat it as a real (short)
        turn so the caller isn't stuck.  Applies to short answers like
        'yes' / 'okay' where Deepgram's smart-format doesn't add
        terminating punctuation."""
        try:
            await asyncio.sleep(self._config.fragment_flush_ms / 1000.0)
            frag = self._state.pending_fragment.strip()
            if not frag:
                return
            self._state.pending_fragment = ""
            self._state.fragment_flush_task = None
            # Now treat the buffered fragment as a complete final.
            if self._state.eager_confirm_task and not self._state.eager_confirm_task.done():
                self._state.eager_confirm_task.cancel()
                self._state.eager_confirm_task = None
            await self._emit(TurnEventKind.EAGER_END_OF_TURN, text=frag, is_final=True)
            self._state.eager_fired_at_ns = self._clock_ns()
            self._state.saw_speech_start = False
            self._state.eager_confirm_task = asyncio.create_task(
                self._confirm_end_of_turn(frag),
                name=f"tm-confirm-{self._actor.call_id}",
            )
        except asyncio.CancelledError:
            pass

    async def _confirm_end_of_turn(self, text: str) -> None:
        """After eager end, wait N ms.  If no TURN_RESUMED happened,
        promote to END_OF_TURN.  If speech_start fires in the window,
        we treat that as caller resuming and emit TURN_RESUMED
        instead."""
        try:
            await asyncio.sleep(self._config.eager_confirm_ms / 1000.0)
            if self._state.saw_speech_start:
                await self._emit(TurnEventKind.TURN_RESUMED, text=text)
            else:
                await self._emit(TurnEventKind.END_OF_TURN, text=text, is_final=True)
        except asyncio.CancelledError:
            pass

    async def _false_interruption_deadline(self) -> None:
        """If we saw speech_start during agent speech but never
        confirmed content within the window, emit FALSE_INTERRUPTION
        so the ducking layer can unduck."""
        try:
            await asyncio.sleep(self._config.false_interruption_deadline_ms / 1000.0)
            if self._state.non_backchannel_partial_count == 0:
                await self._emit(TurnEventKind.FALSE_INTERRUPTION)
        except asyncio.CancelledError:
            pass
        finally:
            self._state.false_interruption_deadline_task = None

    def _cancel_false_interruption_deadline(self) -> None:
        t = self._state.false_interruption_deadline_task
        if t is not None and not t.done():
            t.cancel()
        self._state.false_interruption_deadline_task = None

    # ── helpers ────────────────────────────────────────────────────

    def _agent_is_speaking(self) -> bool:
        return self._actor.state in (CallState.SPEAKING, CallState.YIELDING)

    async def _emit(
        self, kind: TurnEventKind, text: str = "", is_final: bool = False,
    ) -> None:
        """Emit a turn event as a CONTROL kind into the actor.
        Downstream barge-in / brain handlers subscribe to these."""
        try:
            await self._actor.emit(CallEvent.new(
                call_id=self._actor.call_id,
                tenant_id=self._actor.tenant_id,
                source=EventSource.CONTROL,
                turn_generation=self._actor.turn_generation,
                speech_generation=self._actor.speech_generation,
                kind=kind.value,
                payload={"text": text, "is_final": is_final},
            ))
        except Exception as e:
            log.warning("TurnManager emit failed (%s): %s", kind.value, e)
