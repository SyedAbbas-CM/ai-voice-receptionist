"""Semantic planner — the WHAT of a turn.

Wraps the existing ReceptionistBrain so we don't fork the tool-loop or
the emergency-classifier / input-guard paths that already work.  The
wrapper's only job is to extract a speech_act tag from the brain's
output so the performance planner can plan delivery.

Speech-act inference (until the brain prompt is extended in a followup):
   * escalated=True                    → EMERGENCY
   * tool_results contain 'book_*'     → CONFIRM
   * text starts with 'Sorry|I'm sorry' → APOLOGY
   * text contains 'don't have|not available|no openings' → DELIVER_BAD_NEWS
   * turn count == 0 (greeting)         → GREETING
   * otherwise                          → NEUTRAL

This deterministic classifier is cheap (< 0.5ms) and covers ~80% of
turns in the current business flow.  When the brain prompt is
extended to emit `speech_act` in its JSON, this classifier becomes
the fallback for missing/invalid values.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from packages.schemas import CallState
from packages.voice.vpl import SpeechAct

from ..brain import BrainTurnResult, ReceptionistBrain

log = logging.getLogger(__name__)


_APOLOGY_RE = re.compile(r"^\s*(sorry|i'?m sorry|my apolog)", re.IGNORECASE)
_BAD_NEWS_RE = re.compile(
    r"\b(don'?t have|not available|no (?:openings|slots|appointments|reservations)|fully booked|can'?t (?:accommodate|find))",
    re.IGNORECASE,
)
_CLARIFY_RE = re.compile(
    r"(could you|can you (?:tell me|repeat|clarify|confirm)|(?:did|do) you (?:mean|say)|not (?:sure|clear))",
    re.IGNORECASE,
)


BOOKING_TOOL_PREFIXES = ("book_", "reserve_", "schedule_", "confirm_")


@dataclass(frozen=True)
class SemanticOutput:
    """What the semantic planner returned for one turn.

    Wraps BrainTurnResult with a resolved speech_act.  Downstream code
    (twilio_actor._stream_tts) reads this to build a VPL utterance."""
    reply: str
    speech_act: SpeechAct
    state: CallState
    tool_results: list[dict] = field(default_factory=list)
    escalated: bool = False

    @classmethod
    def from_brain(cls, result: BrainTurnResult) -> "SemanticOutput":
        act = _infer_speech_act(result)
        return cls(
            reply=result.reply,
            speech_act=act,
            state=result.state,
            tool_results=result.tool_results,
            escalated=result.escalated,
        )


def _infer_speech_act(result: BrainTurnResult) -> SpeechAct:
    """Best-effort deterministic classifier.  Called once per turn.

    Priority order matters: escalation before booking before content
    patterns, because a booking-during-emergency is still an emergency.
    """
    # 1. Explicit tag from the brain wins (Sprint 9e followup — brain
    #    prompt extension).  Enum-safe: unknown values fall through.
    if result.speech_act and result.speech_act != "neutral":
        try:
            return SpeechAct(result.speech_act)
        except ValueError:
            log.warning(
                "brain returned unknown speech_act=%r; falling back to inference",
                result.speech_act,
            )

    # 2. Escalation is highest priority
    if result.escalated:
        return SpeechAct.EMERGENCY

    # 3. Booking-related tool call → CONFIRM
    for tr in result.tool_results:
        name = tr.get("name", "")
        if any(name.startswith(p) for p in BOOKING_TOOL_PREFIXES):
            if tr.get("result", {}).get("blocked") is not True:
                return SpeechAct.CONFIRM

    text = (result.reply or "").strip()
    if not text:
        return SpeechAct.NEUTRAL

    # 4. Text pattern matching (in priority order)
    if _APOLOGY_RE.match(text):
        return SpeechAct.APOLOGY
    if _BAD_NEWS_RE.search(text):
        return SpeechAct.DELIVER_BAD_NEWS
    if _CLARIFY_RE.search(text):
        return SpeechAct.CLARIFY

    return SpeechAct.NEUTRAL


class SemanticPlanner:
    """Thin wrapper around ReceptionistBrain.

    Kept a class (not a function) so we can inject test fakes and add
    per-tenant state (voice profile, prior utterance) later without
    changing the public signature."""

    def __init__(self, brain: ReceptionistBrain) -> None:
        self._brain = brain

    async def greet(self, state: CallState) -> SemanticOutput:
        result = await self._brain.greet(state)
        # Greetings always classify as GREETING, regardless of text
        return SemanticOutput(
            reply=result.reply,
            speech_act=SpeechAct.GREETING,
            state=result.state,
            tool_results=result.tool_results,
            escalated=result.escalated,
        )

    async def plan(self, state: CallState, user_text: str) -> SemanticOutput:
        result = await self._brain.handle_user_turn(state, user_text)
        return SemanticOutput.from_brain(result)
