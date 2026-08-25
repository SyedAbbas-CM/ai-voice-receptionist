"""P7 scaffold shape + default-policy tests.

Purpose: pin the dataclass shape and the baseline rule-based policy
decisions so any future refactor breaks tests loudly. This module is
NOT WIRED TO RUNTIME (no import from brain.py or twilio_actor.py) —
these tests only prove the module compiles + the default policy makes
sensible decisions.

Real behavioral coverage lands with the wiring PR (see P7 in master TODO).
"""
from __future__ import annotations

from packages.dialogue.next_action_policy import (
    CallerAffect,
    CallerStyle,
    ConversationAction,
    ConversationDecisionState,
    ConversationNextAction,
    ConversationPhase,
    DeliveryIntent,
    NextActionPolicy,
    Urgency,
)


# ── dataclass shape pins ───────────────────────────────────────────


def test_decision_state_default_construction():
    """The state must be constructible with zero args — reducer needs
    a safe empty snapshot on turn 0 before any inference has happened."""
    s = ConversationDecisionState()
    assert s.conversation_phase == ConversationPhase.DISCOVERY
    assert s.caller_affect == CallerAffect.NEUTRAL
    assert s.caller_style == CallerStyle.BRIEF
    assert s.urgency == Urgency.LOW
    assert s.known == {}
    assert s.missing == []
    assert s.tool_pending is False
    assert s.requires_confirmation is False
    assert s.pending_tasks == []


def test_decision_state_is_frozen():
    """Frozen so the policy can't accidentally mutate its input."""
    import dataclasses
    s = ConversationDecisionState()
    try:
        s.tool_pending = True  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("ConversationDecisionState must be frozen")


def test_next_action_default_construction():
    """Output must be constructible from just the action — the rest defaults."""
    a = ConversationNextAction(action=ConversationAction.ANSWER)
    assert a.action == ConversationAction.ANSWER
    assert a.requested_slot is None
    assert a.tool is None
    assert a.delivery_intent == DeliveryIntent.STANDARD
    assert a.max_tokens is None
    assert a.must_include_facts == []


# ── policy rule pins ───────────────────────────────────────────────


def test_emergency_short_circuits_to_escalate():
    """Emergency urgency ALWAYS wins over any other state."""
    p = NextActionPolicy()
    a = p.decide(ConversationDecisionState(
        urgency=Urgency.EMERGENCY,
        tool_pending=True,           # would normally win
        requires_confirmation=True,  # would normally win
        missing=["phone"],           # would normally win
    ))
    assert a.action == ConversationAction.ESCALATE
    assert a.delivery_intent == DeliveryIntent.CRISP


def test_tool_pending_returns_preamble():
    """A tool call in flight → agent should say what it's doing."""
    a = NextActionPolicy().decide(ConversationDecisionState(tool_pending=True))
    assert a.action == ConversationAction.TOOL_PREAMBLE
    assert a.max_tokens == 32


def test_requires_confirmation_returns_confirm_action_with_facts():
    """Booking readback: policy must include known facts verbatim."""
    a = NextActionPolicy().decide(ConversationDecisionState(
        requires_confirmation=True,
        known={
            "caller_name": "Abbas",
            "service": "cleaning",
            "date": "2026-08-25",
            "time": "14:30",
            "notes": "ignored",  # not in the include list
        },
    ))
    assert a.action == ConversationAction.CONFIRM_ACTION
    assert a.max_tokens == 80
    # facts include known slots relevant to confirmation only
    fact_keys = {f.split(":")[0] for f in a.must_include_facts}
    assert fact_keys == {"caller_name", "service", "date", "time"}


def test_missing_slots_returns_ask_slot():
    """Empty booking with a missing field → ask for it."""
    a = NextActionPolicy().decide(ConversationDecisionState(
        missing=["phone", "date"],
    ))
    assert a.action == ConversationAction.ASK_SLOT
    assert a.requested_slot == "phone"  # first missing
    assert a.max_tokens == 40


def test_opening_phase_returns_acknowledge():
    """Turn 0 / opening → short warm ack, not a full answer."""
    a = NextActionPolicy().decide(ConversationDecisionState(
        conversation_phase=ConversationPhase.OPENING,
    ))
    assert a.action == ConversationAction.ACKNOWLEDGE
    assert a.max_tokens == 20
    assert a.delivery_intent == DeliveryIntent.WARM


def test_wrapping_phase_returns_end_call():
    """Post-commit wrap → farewell + hangup."""
    a = NextActionPolicy().decide(ConversationDecisionState(
        conversation_phase=ConversationPhase.WRAPPING,
    ))
    assert a.action == ConversationAction.END_CALL


def test_default_fallback_returns_answer_standard():
    """No rule matches → let the LLM answer (matches current behavior).
    Preserves the ability to ship this behind a flag without breaking calls."""
    a = NextActionPolicy().decide(ConversationDecisionState())
    assert a.action == ConversationAction.ANSWER
    assert a.delivery_intent == DeliveryIntent.STANDARD
    assert a.max_tokens == 48


def test_rushed_caller_gets_crisp_delivery_on_default():
    """Affect drives delivery_intent without changing action."""
    a = NextActionPolicy().decide(ConversationDecisionState(
        caller_affect=CallerAffect.RUSHED,
    ))
    assert a.action == ConversationAction.ANSWER
    assert a.delivery_intent == DeliveryIntent.CRISP


def test_confirmation_precedes_missing_slots():
    """If we owe a readback AND have missing slots, readback wins.
    Prevents 'ask for phone then confirm' backwards flow."""
    a = NextActionPolicy().decide(ConversationDecisionState(
        requires_confirmation=True,
        missing=["email"],
    ))
    assert a.action == ConversationAction.CONFIRM_ACTION


def test_tool_pending_precedes_missing_slots():
    """Tool in flight always preempts asking a new slot — the tool result
    might reveal the slot doesn't need asking."""
    a = NextActionPolicy().decide(ConversationDecisionState(
        tool_pending=True,
        missing=["phone"],
    ))
    assert a.action == ConversationAction.TOOL_PREAMBLE
