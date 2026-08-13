"""SpeechCommitGate — deterministic pre-TTS commit gate for streaming.

Problem this exists to solve
============================
The streaming LLM → TTS pipeline releases sentences to the wire as soon
as they arrive.  This wins ~700ms of first-response latency vs waiting
for the full reply.  But it opens a correctness hole:

  1. LLM streams "Gotcha!" + "Let me confirm that for you." +
     "One moment, please."
  2. Pump releases all three to TTS — caller starts hearing them.
  3. Full reply assembles.  R2 fake-wait guard fires: "wait language,
     no tool call — REWRITE."
  4. Rewrite goes to a fresh TTS start.  Caller hears the three fake-
     wait utterances THEN the rewrite.  Duplicate speech.

The R2 guard is correct about the lie.  The problem is *when* it runs —
after early sentences are on the wire.

The wrong fix is to buffer the whole reply (kills the latency win) or
to invoke an LLM guard per sentence (expensive, wrong tool).

The right fix is a deterministic **selective commit gate**: classify
each sentence before TTS, hold the ones that depend on downstream
signals (a tool starting, a tool receipt landing), and release them
only when the signal actually arrives.

Design guardrails
=================
- Deterministic: regex + tool-state.  No LLM per sentence.
- Selective: SAFE sentences stream immediately.  Only claims that need
  evidence get held.
- Generic: same mechanism will later protect booking confirmations,
  cancellations, payments, prices, RAG assertions, transfers, etc.
  Adding a class of held speech = one regex list + one signal name.
- Latency-preserving: the first sentence of a normal turn is nearly
  always SAFE, so first-audio latency is unaffected.
- Fail-safe: if a stream ends and a held sentence never got its
  signal, it is DROPPED, not spoken.  A workflow-level replacement
  (or the existing STREAM_REPLY_REPLACED path) handles the recovery.

This module is telephony-agnostic; the actor wraps queue.put through
`SpeechCommitGate.on_sentence(...)` and calls `on_tool_call_started`,
`on_tool_receipt`, and `flush` at the right moments.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, List, Optional, Set

log = logging.getLogger(__name__)


class SpeechClass(str, Enum):
    """What kind of downstream evidence a sentence depends on."""

    SAFE = "safe"
    """No downstream dependency.  Stream immediately."""

    WAIT_PROMISE = "wait_promise"
    """'one moment', 'let me check' — needs a tool CALL to actually
    start before we speak it.  Otherwise the caller is being told to
    wait for nothing."""

    ACTION_CONFIRMATION = "action_confirmation"
    """'you're booked', 'confirmed', 'scheduled' — needs a SUCCESSFUL
    tool RECEIPT for the matching action before we speak it.
    Otherwise it's a fake confirmation."""

    UNSUPPORTED_COMMITMENT = "unsupported_commitment"
    """Reserved: sentences that make specific factual claims (prices,
    times, guarantees) without upstream evidence.  Not detected in v1.
    Placeholder so downstream code can distinguish 'held pending
    evidence' from 'never speakable'."""


# ── classifier patterns ──────────────────────────────────────────────
# Kept in sync with brain.py's R2 patterns.  Duplicated here (not
# imported) so the gate stays independently unit-testable and doesn't
# create an import cycle between core_agent.brain and core_agent.speech_*.

_WAIT_PROMISE_PATTERNS = tuple(
    re.compile(pat, re.IGNORECASE)
    for pat in (
        r"\bone (?:moment|sec(?:ond)?|minute)\b",
        r"\blet me (?:check|look|see|find|verify|confirm|pull up|grab)\b",
        r"\bi(?:'ll| will) (?:check|look|see|find|verify|pull up|grab|confirm)\b",
        r"\bhold on\b",
        r"\bhang on\b",
        r"\bgive me (?:a )?(?:sec(?:ond)?|moment|minute|second)\b",
        r"\bchecking (?:now|on that|availability|the calendar|for you)\b",
        r"\bjust a (?:sec(?:ond)?|moment|minute)\b",
        r"\blooking (?:that up|into (?:that|it))\b",
        r"\bbear with me\b",
    )
)

_ACTION_CONFIRMATION_PATTERNS = tuple(
    re.compile(pat, re.IGNORECASE)
    for pat in (
        r"\byou'?re all set\b",
        r"\byou'?re booked\b",
        r"\byou'?re confirmed\b",
        r"\bi'?ve booked\b",
        r"\bi'?ve got you (?:down|booked|scheduled) for\b",
        r"\b(?:appointment|booking) (?:is )?(?:booked|confirmed|locked in|set)\b",
        r"\blocked in\b",
        r"\bsee you\s+(?:then|on|at|next|this|tomorrow|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        r"\ball set for your\b",
        r"\bi'?ve (?:scheduled|reserved) that\b",
    )
)


# Which tool names count as "the action" for ACTION_CONFIRMATION.
# Kept in sync with brain.py's _BOOKING_TOOLS; grows as we add
# cancellation / payment / transfer flows.
_ACTION_TOOLS = frozenset({
    "book_appointment", "book_reservation", "book_viewing",
})


def classify(sentence: str) -> SpeechClass:
    """Return the SpeechClass for one sentence.

    Deterministic and cheap.  Order matters — action confirmation is
    stricter than wait promise, so we check it first.
    """
    if not sentence or not sentence.strip():
        return SpeechClass.SAFE
    for pat in _ACTION_CONFIRMATION_PATTERNS:
        if pat.search(sentence):
            return SpeechClass.ACTION_CONFIRMATION
    for pat in _WAIT_PROMISE_PATTERNS:
        if pat.search(sentence):
            return SpeechClass.WAIT_PROMISE
    return SpeechClass.SAFE


# ── held sentence bookkeeping ────────────────────────────────────────

@dataclass
class _Held:
    """One sentence waiting for a downstream signal."""
    text: str
    kind: SpeechClass
    # For ACTION_CONFIRMATION: which tool receipt satisfies this
    # sentence.  Empty = any successful ACTION_TOOL receipt matches.
    required_tool: Optional[str] = None
    # Monotonic timestamp for debugging.
    at: float = 0.0


ReleaseCallback = Callable[[str], Awaitable[None]]
"""Async callable the gate calls to actually stream a sentence to TTS.

The actor supplies this — typically `lambda s: queue.put(s)`.
"""


@dataclass
class GateStats:
    """Counters for observability (call debug endpoint, telemetry)."""
    safe_released: int = 0
    wait_held: int = 0
    wait_released: int = 0
    wait_dropped: int = 0
    action_held: int = 0
    action_released: int = 0
    action_dropped: int = 0

    def as_dict(self) -> dict:
        return {
            "safe_released": self.safe_released,
            "wait_held": self.wait_held,
            "wait_released": self.wait_released,
            "wait_dropped": self.wait_dropped,
            "action_held": self.action_held,
            "action_released": self.action_released,
            "action_dropped": self.action_dropped,
        }


# ── the gate ─────────────────────────────────────────────────────────

class SpeechCommitGate:
    """Per-turn gate between the sentence buffer and the TTS pump.

    Lifecycle: one gate per streaming turn.  Actor creates a fresh
    instance for each `_run_brain_streaming` call.

    Interface:
      - `on_sentence(text)` — called for every sentence the SentenceBuffer
        produces.  Gate decides: release immediately, or hold.
      - `on_tool_call_started(name)` — called when brain dispatches a tool.
        Releases any WAIT_PROMISE sentences.
      - `on_tool_receipt(name, ok)` — called when a tool RETURNS.
        Releases any ACTION_CONFIRMATION sentence whose required_tool
        matches, provided ok=True.
      - `flush()` — end-of-stream.  Any still-held sentence is dropped
        (with a WARN log).  Return the list of dropped-and-would-have-
        been-spoken texts so the caller can decide on a safe fallback.

    Thread safety: single asyncio task per gate.  Not thread-safe.
    """

    def __init__(
        self,
        release: ReleaseCallback,
        call_id: str = "?",
        turn_gen: int = -1,
    ) -> None:
        self._release = release
        self._call_id = call_id
        self._turn_gen = turn_gen

        # Ordered list — we always release in the order the LLM produced.
        # A held sentence blocks later held sentences even if the later
        # one becomes releasable first, to preserve reply coherence.
        self._held: List[_Held] = []

        # Signals we've received so far in the turn.
        self._tool_calls_started: Set[str] = set()
        self._tool_receipts_ok: Set[str] = set()

        # What actually crossed the gate to the TTS pump.  Used by the
        # actor's reply-divergence check to compare AGAINST what was
        # spoken (not against the raw LLM stream which may contain
        # dropped-in-gate sentences).
        self._released_texts: List[str] = []

        self.stats = GateStats()
        self._closed = False

    @property
    def released_text(self) -> str:
        """Concatenation of every sentence the gate actually released.
        Empty until the first release; grows as releases happen.  The
        actor reads this after flush() instead of `buf.full_text` to
        decide whether the assembled reply diverged from what got out."""
        return " ".join(self._released_texts).strip()

    # ── inbound: sentence classification ─────────────────────────────

    async def on_sentence(self, text: str) -> None:
        """Classify + release-or-hold ONE sentence.

        Called by the actor from `on_delta` in `_run_brain_streaming`,
        replacing the previous `await queue.put(sentence)`.
        """
        if self._closed:
            log.warning(
                "GATE_LATE_SENTENCE call=%s gen=%d text=%r (dropped)",
                self._call_id, self._turn_gen, text[:60],
            )
            return

        kind = classify(text)

        if kind == SpeechClass.SAFE:
            # Never held.  BUT: preserve order — if anything is queued
            # ahead of us waiting for a signal, we can NOT jump the line.
            # Otherwise a stream like:
            #   "Let me check availability."         [WAIT_PROMISE, held]
            #   "The next available is 8:30 AM."     [SAFE]
            # would speak "The next available is 8:30 AM." first, which
            # is incoherent.  Append after any pending holds so the pump
            # sees them in original order.
            if self._held:
                self._append_held(text, SpeechClass.SAFE)
                log.info(
                    "GATE_ORDER_HOLD call=%s gen=%d kind=safe text=%r "
                    "(behind %d held)",
                    self._call_id, self._turn_gen,
                    text[:60], len(self._held) - 1,
                )
                # Immediately try to release — if the head of the queue
                # became releasable earlier, we'll drain up to and
                # including this safe sentence.
                await self._try_release_from_head()
                return
            self.stats.safe_released += 1
            await self._do_release(text)
            return

        if kind == SpeechClass.WAIT_PROMISE:
            # If ANY tool call has started this turn, the wait is honest —
            # release now.  Otherwise hold until one does.
            if self._tool_calls_started:
                self.stats.wait_released += 1
                log.info(
                    "GATE_WAIT_RELEASE_IMMEDIATE call=%s gen=%d "
                    "text=%r (tool_started=%s)",
                    self._call_id, self._turn_gen, text[:60],
                    sorted(self._tool_calls_started),
                )
                await self._do_release(text)
                return
            self._append_held(text, SpeechClass.WAIT_PROMISE)
            self.stats.wait_held += 1
            log.info(
                "GATE_WAIT_HELD call=%s gen=%d text=%r",
                self._call_id, self._turn_gen, text[:60],
            )
            return

        if kind == SpeechClass.ACTION_CONFIRMATION:
            # Must have a matching successful receipt.  If one already
            # landed (rare — brain usually finishes tool loop before
            # emitting the confirmation), release immediately.
            if self._tool_receipts_ok & _ACTION_TOOLS:
                self.stats.action_released += 1
                log.info(
                    "GATE_ACTION_RELEASE_IMMEDIATE call=%s gen=%d "
                    "text=%r receipts=%s",
                    self._call_id, self._turn_gen, text[:60],
                    sorted(self._tool_receipts_ok & _ACTION_TOOLS),
                )
                await self._do_release(text)
                return
            self._append_held(text, SpeechClass.ACTION_CONFIRMATION)
            self.stats.action_held += 1
            log.warning(
                "GATE_ACTION_HELD call=%s gen=%d text=%r "
                "(no successful action receipt yet)",
                self._call_id, self._turn_gen, text[:60],
            )
            return

        # UNSUPPORTED_COMMITMENT — held until further notice; v1 has no
        # signal that releases it, so flush() will drop.
        self._append_held(text, kind)
        log.warning(
            "GATE_UNSUPPORTED_HELD call=%s gen=%d text=%r",
            self._call_id, self._turn_gen, text[:60],
        )

    # ── inbound: downstream signals ──────────────────────────────────

    async def on_tool_call_started(self, tool_name: str) -> None:
        """Brain dispatched a tool.  Releases held WAIT_PROMISE
        sentences (the wait becomes honest)."""
        if self._closed:
            return
        self._tool_calls_started.add(tool_name)
        log.info(
            "GATE_TOOL_STARTED call=%s gen=%d tool=%s held=%d",
            self._call_id, self._turn_gen, tool_name, len(self._held),
        )
        await self._try_release_from_head()

    async def on_tool_receipt(self, tool_name: str, ok: bool) -> None:
        """A tool returned.  Success → releases matching
        ACTION_CONFIRMATION.  Failure → does nothing (held sentence
        stays held, drops at flush)."""
        if self._closed:
            return
        if ok:
            self._tool_receipts_ok.add(tool_name)
        log.info(
            "GATE_TOOL_RECEIPT call=%s gen=%d tool=%s ok=%s held=%d",
            self._call_id, self._turn_gen, tool_name, ok, len(self._held),
        )
        await self._try_release_from_head()

    # ── end-of-stream ────────────────────────────────────────────────

    async def flush(self) -> List[str]:
        """Called when the brain stream ends.  Any still-held sentence
        is DROPPED.  Returns the list of dropped texts so the caller
        can log or take corrective action.

        After flush, the gate is closed — further on_sentence calls
        are refused with a WARN log.
        """
        # Give any releasable sentences one last chance.
        await self._try_release_from_head()
        dropped: List[str] = []
        for h in self._held:
            dropped.append(h.text)
            if h.kind == SpeechClass.WAIT_PROMISE:
                self.stats.wait_dropped += 1
            elif h.kind == SpeechClass.ACTION_CONFIRMATION:
                self.stats.action_dropped += 1
            log.warning(
                "GATE_DROP call=%s gen=%d kind=%s text=%r "
                "(no satisfying signal by end-of-stream)",
                self._call_id, self._turn_gen, h.kind.value, h.text[:80],
            )
        self._held.clear()
        self._closed = True
        return dropped

    # ── internal ─────────────────────────────────────────────────────

    def _append_held(self, text: str, kind: SpeechClass) -> None:
        self._held.append(_Held(text=text, kind=kind))

    def _is_releasable(self, h: _Held) -> bool:
        if h.kind == SpeechClass.SAFE:
            return True
        if h.kind == SpeechClass.WAIT_PROMISE:
            return bool(self._tool_calls_started)
        if h.kind == SpeechClass.ACTION_CONFIRMATION:
            return bool(self._tool_receipts_ok & _ACTION_TOOLS)
        # UNSUPPORTED_COMMITMENT: no release path in v1.
        return False

    async def _do_release(self, text: str) -> None:
        """Single release chokepoint — records text for released_text
        and calls the actor's release callback."""
        self._released_texts.append(text)
        await self._release(text)

    async def _try_release_from_head(self) -> None:
        """Drain the head of the held queue while the head is releasable.

        We DO NOT release the tail past a stuck head — preserving
        original reply order is more important than emptying the queue.
        """
        while self._held and self._is_releasable(self._held[0]):
            h = self._held.pop(0)
            if h.kind == SpeechClass.WAIT_PROMISE:
                self.stats.wait_released += 1
            elif h.kind == SpeechClass.ACTION_CONFIRMATION:
                self.stats.action_released += 1
            elif h.kind == SpeechClass.SAFE:
                self.stats.safe_released += 1
            log.info(
                "GATE_RELEASE call=%s gen=%d kind=%s text=%r",
                self._call_id, self._turn_gen,
                h.kind.value, h.text[:80],
            )
            await self._do_release(h.text)
