"""Sprint 9e: SemanticPlanner tests.

The planner is a thin wrapper around ReceptionistBrain — we test the
speech_act inference layer, not the brain itself (which has its own
tests).  Coverage:

  * greet() always classifies as GREETING
  * plan() reads brain.speech_act when set to a valid enum
  * plan() falls back to inference when speech_act is neutral/invalid
  * inference: escalation → EMERGENCY
  * inference: booking tool → CONFIRM
  * inference: apology text → APOLOGY
  * inference: bad-news text → DELIVER_BAD_NEWS
  * inference: clarify text → CLARIFY
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from packages.core_agent.brain import BrainTurnResult
from packages.core_agent.planners import SemanticPlanner, SemanticOutput
from packages.core_agent.planners.semantic import _infer_speech_act
from packages.voice.vpl import SpeechAct


class _StubBrain:
    """Minimal brain stub — records call args, returns configured result."""

    def __init__(self, result: BrainTurnResult) -> None:
        self._result = result
        self.greet_called = 0
        self.turn_called = 0
        self.last_user_text = None

    async def greet(self, state):
        self.greet_called += 1
        return self._result

    async def handle_user_turn(self, state, user_text):
        self.turn_called += 1
        self.last_user_text = user_text
        return self._result


def _make_state():
    """Minimal CallState stub that satisfies BrainTurnResult typing."""
    from packages.schemas import CallState
    return CallState(
        session_id="test", business_id="test-biz",
    )


def _make_result(**kwargs) -> BrainTurnResult:
    defaults = dict(
        reply="hello", state=_make_state(),
        tool_results=[], escalated=False, speech_act="neutral",
    )
    defaults.update(kwargs)
    return BrainTurnResult(**defaults)


# ── greet always classifies as GREETING ─────────────────────────────

@pytest.mark.asyncio
async def test_greet_always_returns_greeting_speech_act():
    brain = _StubBrain(_make_result(reply="Hi there, how can I help?"))
    planner = SemanticPlanner(brain)
    out = await planner.greet(_make_state())
    assert isinstance(out, SemanticOutput)
    assert out.speech_act == SpeechAct.GREETING
    assert out.reply == "Hi there, how can I help?"
    assert brain.greet_called == 1


# ── plan() honors explicit brain.speech_act ─────────────────────────

@pytest.mark.asyncio
async def test_plan_uses_brain_speech_act_when_valid():
    brain = _StubBrain(_make_result(
        reply="Booked for 3pm.", speech_act="confirm",
    ))
    planner = SemanticPlanner(brain)
    out = await planner.plan(_make_state(), "book me for 3pm")
    assert out.speech_act == SpeechAct.CONFIRM


@pytest.mark.asyncio
async def test_plan_falls_back_when_brain_speech_act_invalid():
    brain = _StubBrain(_make_result(
        reply="Sorry, we're fully booked.", speech_act="nonsense_value",
    ))
    planner = SemanticPlanner(brain)
    out = await planner.plan(_make_state(), "any slots?")
    # Falls back to inference; "sorry" + "fully booked" is APOLOGY first
    # by priority order.
    assert out.speech_act in {SpeechAct.APOLOGY, SpeechAct.DELIVER_BAD_NEWS}


# ── inference cases ─────────────────────────────────────────────────

def test_infer_escalation_beats_everything():
    r = _make_result(
        reply="Booking confirmed for 3pm.", escalated=True,
        tool_results=[{"name": "book_appointment", "result": {"ok": True}}],
    )
    assert _infer_speech_act(r) == SpeechAct.EMERGENCY


def test_infer_booking_tool_when_not_escalated():
    r = _make_result(
        reply="You're all set.",
        tool_results=[{"name": "book_appointment", "arguments": {}, "result": {"ok": True}}],
    )
    assert _infer_speech_act(r) == SpeechAct.CONFIRM


def test_infer_blocked_booking_not_confirm():
    """A blocked booking tool call should NOT classify as CONFIRM."""
    r = _make_result(
        reply="I need to double-check that time.",
        tool_results=[{
            "name": "book_appointment",
            "arguments": {"time": "3pm"},
            "result": {"blocked": True, "reason": "unverified"},
        }],
    )
    assert _infer_speech_act(r) != SpeechAct.CONFIRM


def test_infer_apology_pattern():
    for text in [
        "Sorry about that.",
        "I'm sorry, I don't know.",
        "My apologies, let me try again.",
    ]:
        r = _make_result(reply=text)
        assert _infer_speech_act(r) == SpeechAct.APOLOGY, f"failed on {text!r}"


def test_infer_bad_news_pattern():
    for text in [
        "We don't have that available Tuesday.",
        "No openings tomorrow, unfortunately.",
        "That slot is not available.",
        "We're fully booked this week.",
        "I can't accommodate that request.",
    ]:
        r = _make_result(reply=text)
        assert _infer_speech_act(r) == SpeechAct.DELIVER_BAD_NEWS, \
            f"failed on {text!r}"


def test_infer_clarify_pattern():
    for text in [
        "Could you tell me which day?",
        "Can you repeat the last part?",
        "Did you say Tuesday or Thursday?",
    ]:
        r = _make_result(reply=text)
        assert _infer_speech_act(r) == SpeechAct.CLARIFY, f"failed on {text!r}"


def test_infer_defaults_to_neutral():
    r = _make_result(reply="Sure, one moment.")
    assert _infer_speech_act(r) == SpeechAct.NEUTRAL


def test_infer_empty_reply_neutral():
    r = _make_result(reply="")
    assert _infer_speech_act(r) == SpeechAct.NEUTRAL


# ── priority: apology beats bad-news ────────────────────────────────

def test_apology_prefix_beats_bad_news_body():
    """A turn that starts with 'Sorry' AND mentions unavailability
    should classify as APOLOGY (the apology framing is the dominant
    delivery signal)."""
    r = _make_result(reply="Sorry, we don't have Tuesday open.")
    assert _infer_speech_act(r) == SpeechAct.APOLOGY
