"""Tests for AcknowledgmentKind + NextActionPolicy ack selection.

2026-08-25 (humanness audit P0.2): the LLM was ignoring prompt guidance
about acks under load — same "Okay," every turn or none at all.  This
moves ack selection into a deterministic policy so the LLM only has to
verbalize the chosen ack shape, not decide which shape to use.

Pins:
- Caller-state signals (hardship / correction / wait / dictation) win
  over action-type defaults.
- Action-type canonical acks fire when caller-state is neutral.
- RUSHED / CRISP delivery → ACK_NONE (skip the social grease).
- Recency guard prevents "gotcha, gotcha" repeats (except for canonical
  acks like CORRECTION / EMPATHY / WAIT that are dominant).
- ACK_NONE stacking is fine (dictation runs stay silent).
"""
from __future__ import annotations

import pytest

from packages.dialogue.next_action_policy import (
    AcknowledgmentKind,
    CallerAffect,
    ConversationAction,
    ConversationDecisionState,
    ConversationPhase,
    DeliveryIntent,
    NextActionPolicy,
    Urgency,
)


@pytest.fixture
def policy():
    return NextActionPolicy()


# ── caller-state signals win (P1: dominant) ────────────────────────


def test_dictation_returns_none_regardless_of_action(policy):
    """Caller reading digits → no ack even if action is ASK_SLOT."""
    state = ConversationDecisionState(
        caller_is_dictating=True,
        missing=["phone"],
    )
    decision = policy.decide(state)
    assert decision.acknowledgment == AcknowledgmentKind.ACK_NONE


def test_wait_signal_returns_wait_regardless_of_action(policy):
    """Caller said 'hold on' → silent ack even if action was PROPOSE."""
    state = ConversationDecisionState(
        caller_asked_to_wait=True,
    )
    decision = policy.decide(state)
    assert decision.acknowledgment == AcknowledgmentKind.ACK_WAIT


def test_correction_signal_returns_correction(policy):
    """Caller said 'no, I said Thursday' → ACK_CORRECTION."""
    state = ConversationDecisionState(
        caller_corrected_us=True,
    )
    decision = policy.decide(state)
    assert decision.acknowledgment == AcknowledgmentKind.ACK_CORRECTION


def test_hardship_signal_returns_empathy(policy):
    """Caller shared pain → EMPATHY even if action is ANSWER."""
    state = ConversationDecisionState(
        caller_shared_hardship=True,
    )
    decision = policy.decide(state)
    assert decision.acknowledgment == AcknowledgmentKind.ACK_EMPATHY


def test_dictation_wins_over_correction(policy):
    """If both signals set (edge case), dictation is first in the ladder."""
    state = ConversationDecisionState(
        caller_is_dictating=True,
        caller_corrected_us=True,
    )
    decision = policy.decide(state)
    assert decision.acknowledgment == AcknowledgmentKind.ACK_NONE


# ── action-type canonical acks ─────────────────────────────────────


def test_tool_preamble_gets_transition(policy):
    """Tool preamble ('let me check') is a soft transition."""
    state = ConversationDecisionState(
        tool_pending=True,
    )
    decision = policy.decide(state)
    assert decision.action == ConversationAction.TOOL_PREAMBLE
    assert decision.acknowledgment == AcknowledgmentKind.ACK_TRANSITION


def test_confirm_action_gets_agreement(policy):
    """Booking confirmation → agreement primes the tone."""
    state = ConversationDecisionState(
        requires_confirmation=True,
        known={"service": "cleaning", "date": "2026-08-26",
                "time": "14:30", "caller_name": "Abbas"},
    )
    decision = policy.decide(state)
    assert decision.action == ConversationAction.CONFIRM_ACTION
    assert decision.acknowledgment == AcknowledgmentKind.ACK_AGREEMENT


def test_escalate_gets_none(policy):
    """Emergency ESCALATE — no chirpy ack, empathy is implicit in body."""
    state = ConversationDecisionState(
        urgency=Urgency.EMERGENCY,
    )
    decision = policy.decide(state)
    assert decision.action == ConversationAction.ESCALATE
    assert decision.acknowledgment == AcknowledgmentKind.ACK_NONE


def test_end_call_gets_none(policy):
    """Farewell should just be the farewell."""
    state = ConversationDecisionState(
        conversation_phase=ConversationPhase.WRAPPING,
    )
    decision = policy.decide(state)
    assert decision.action == ConversationAction.END_CALL
    assert decision.acknowledgment == AcknowledgmentKind.ACK_NONE


# ── delivery-intent gates ─────────────────────────────────────────


def test_rushed_caller_gets_no_ack(policy):
    """RUSHED caller → CRISP delivery → skip social grease."""
    state = ConversationDecisionState(
        caller_affect=CallerAffect.RUSHED,
    )
    decision = policy.decide(state)
    assert decision.delivery_intent == DeliveryIntent.CRISP
    assert decision.acknowledgment == AcknowledgmentKind.ACK_NONE


def test_rushed_but_shared_hardship_still_empathy(policy):
    """Hardship signal wins over CRISP delivery.  Caller shared context
    even while rushing — receptionist should still acknowledge, briefly."""
    state = ConversationDecisionState(
        caller_affect=CallerAffect.RUSHED,
        caller_shared_hardship=True,
    )
    decision = policy.decide(state)
    assert decision.acknowledgment == AcknowledgmentKind.ACK_EMPATHY


# ── default / plain ANSWER path ───────────────────────────────────


def test_default_answer_gets_understood(policy):
    """Plain ANSWER with no signals → mild UNDERSTOOD."""
    state = ConversationDecisionState()
    decision = policy.decide(state)
    assert decision.action == ConversationAction.ANSWER
    assert decision.acknowledgment == AcknowledgmentKind.ACK_UNDERSTOOD


def test_ask_slot_default_understood(policy):
    """After receiving info + still missing another slot → UNDERSTOOD."""
    state = ConversationDecisionState(
        missing=["phone"],
    )
    decision = policy.decide(state)
    assert decision.action == ConversationAction.ASK_SLOT
    assert decision.acknowledgment == AcknowledgmentKind.ACK_UNDERSTOOD


def test_opening_phase_gets_understood(policy):
    """Opening ack after greeting."""
    state = ConversationDecisionState(
        conversation_phase=ConversationPhase.OPENING,
    )
    decision = policy.decide(state)
    assert decision.action == ConversationAction.ACKNOWLEDGE
    assert decision.acknowledgment == AcknowledgmentKind.ACK_UNDERSTOOD


# ── recency guard ────────────────────────────────────────────────


def test_recency_guard_avoids_double_understood(policy):
    """If last ack was UNDERSTOOD, next ANSWER shouldn't repeat."""
    state = ConversationDecisionState(
        last_ack=AcknowledgmentKind.ACK_UNDERSTOOD,
    )
    decision = policy.decide(state)
    # Second-choice fallback for UNDERSTOOD is NONE.
    assert decision.acknowledgment == AcknowledgmentKind.ACK_NONE


def test_recency_guard_allows_correction_repeat(policy):
    """CORRECTION is dominant — if caller corrects AGAIN, we should
    still ack the correction (not fall through to something else)."""
    state = ConversationDecisionState(
        caller_corrected_us=True,
        last_ack=AcknowledgmentKind.ACK_CORRECTION,
    )
    decision = policy.decide(state)
    assert decision.acknowledgment == AcknowledgmentKind.ACK_CORRECTION


def test_recency_guard_allows_empathy_repeat(policy):
    """Caller sharing hardship across multiple turns → each ack is EMPATHY."""
    state = ConversationDecisionState(
        caller_shared_hardship=True,
        last_ack=AcknowledgmentKind.ACK_EMPATHY,
    )
    decision = policy.decide(state)
    assert decision.acknowledgment == AcknowledgmentKind.ACK_EMPATHY


def test_recency_guard_allows_wait_repeat(policy):
    """Caller still on hold → WAIT ack fires again silently."""
    state = ConversationDecisionState(
        caller_asked_to_wait=True,
        last_ack=AcknowledgmentKind.ACK_WAIT,
    )
    decision = policy.decide(state)
    assert decision.acknowledgment == AcknowledgmentKind.ACK_WAIT


def test_recency_guard_none_stacking_ok(policy):
    """Dictation runs → ACK_NONE every turn is correct."""
    state = ConversationDecisionState(
        caller_is_dictating=True,
        last_ack=AcknowledgmentKind.ACK_NONE,
    )
    decision = policy.decide(state)
    assert decision.acknowledgment == AcknowledgmentKind.ACK_NONE


def test_recency_guard_transition_falls_to_understood(policy):
    """If we just said TRANSITION and would repeat, fall to UNDERSTOOD."""
    state = ConversationDecisionState(
        tool_pending=True,  # would normally give TOOL_PREAMBLE → TRANSITION
        last_ack=AcknowledgmentKind.ACK_TRANSITION,
    )
    decision = policy.decide(state)
    # Fall-through: TRANSITION second-choice is UNDERSTOOD.
    assert decision.acknowledgment == AcknowledgmentKind.ACK_UNDERSTOOD


def test_recency_guard_agreement_falls_to_understood(policy):
    """CONFIRM twice in a row shouldn't both open with 'Perfect!'."""
    state = ConversationDecisionState(
        requires_confirmation=True,
        known={"service": "cleaning", "date": "2026-08-26",
                "time": "14:30", "caller_name": "Abbas"},
        last_ack=AcknowledgmentKind.ACK_AGREEMENT,
    )
    decision = policy.decide(state)
    assert decision.acknowledgment == AcknowledgmentKind.ACK_UNDERSTOOD


# ── decision NEVER RAISES on garbage ───────────────────────────────


def test_decide_returns_valid_ack_on_default_state(policy):
    """Empty state → policy still returns a valid ack."""
    decision = policy.decide(ConversationDecisionState())
    assert isinstance(decision.acknowledgment, AcknowledgmentKind)


def test_all_actions_produce_valid_ack(policy):
    """Every ConversationAction path yields a valid AcknowledgmentKind."""
    scenarios = [
        ConversationDecisionState(),
        ConversationDecisionState(urgency=Urgency.EMERGENCY),
        ConversationDecisionState(tool_pending=True),
        ConversationDecisionState(
            requires_confirmation=True,
            known={"service": "x", "date": "y", "time": "z", "caller_name": "n"},
        ),
        ConversationDecisionState(missing=["phone"]),
        ConversationDecisionState(conversation_phase=ConversationPhase.OPENING),
        ConversationDecisionState(conversation_phase=ConversationPhase.WRAPPING),
    ]
    for s in scenarios:
        decision = policy.decide(s)
        assert isinstance(decision.acknowledgment, AcknowledgmentKind), (
            f"missing/invalid ack for state {s}"
        )


# ── backward compat: acknowledgment=None on hand-built NextAction ──


def test_next_action_default_ack_is_none():
    """Callers that pre-date this change (hand-built ConversationNextAction
    without setting acknowledgment) must still work — default is None."""
    from packages.dialogue.next_action_policy import ConversationNextAction
    na = ConversationNextAction(action=ConversationAction.ANSWER)
    assert na.acknowledgment is None
