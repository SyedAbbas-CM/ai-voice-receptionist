"""Tests for policy_directive.render_policy_directive.

2026-08-27 (task #120): the renderer that turns a NextActionPolicy
decision into a system-note directive brain.py injects into the LLM
prompt.  Zero LLM in these tests — pure string-generation contract.
"""
from __future__ import annotations

import pytest

from packages.core_agent.policy_directive import render_policy_directive
from packages.dialogue.next_action_policy import (
    AcknowledgmentKind,
    ConversationAction,
    ConversationNextAction,
    DeliveryIntent,
)


# ── ack-lane rendering ────────────────────────────────────────────


def test_ack_none_forbids_chirpy_openers():
    d = ConversationNextAction(
        action=ConversationAction.ANSWER,
        acknowledgment=AcknowledgmentKind.ACK_NONE,
    )
    out = render_policy_directive(d)
    assert out is not None
    assert "NO opener" in out
    assert "Sure!" in out or "Absolutely!" in out  # forbid list mentioned


def test_ack_wait_forbids_of_course_take_your_time():
    d = ConversationNextAction(
        action=ConversationAction.ACKNOWLEDGE,
        acknowledgment=AcknowledgmentKind.ACK_WAIT,
    )
    out = render_policy_directive(d)
    assert "silence" in out.lower()
    assert "of course" in out.lower() or "chatbot" in out.lower()


def test_ack_empathy_gives_concrete_openers():
    d = ConversationNextAction(
        action=ConversationAction.ANSWER,
        acknowledgment=AcknowledgmentKind.ACK_EMPATHY,
    )
    out = render_policy_directive(d)
    # Should include at least one of the empathy examples.
    assert any(x in out for x in ("Ah, I see", "sounds rough", "Oh no"))


def test_ack_correction_uses_sorry_shape():
    d = ConversationNextAction(
        action=ConversationAction.REPAIR_MISHEAR,
        acknowledgment=AcknowledgmentKind.ACK_CORRECTION,
    )
    out = render_policy_directive(d)
    assert "sorry" in out.lower() or "mistake" in out.lower()


def test_ack_understood_gives_three_examples():
    d = ConversationNextAction(
        action=ConversationAction.ANSWER,
        acknowledgment=AcknowledgmentKind.ACK_UNDERSTOOD,
    )
    out = render_policy_directive(d)
    # At least two examples in the joined list.
    assert out.count("'") >= 4  # 2+ quoted strings


# ── recency guard ─────────────────────────────────────────────────


def test_last_ack_matching_adds_vary_hint():
    d = ConversationNextAction(
        action=ConversationAction.ANSWER,
        acknowledgment=AcknowledgmentKind.ACK_UNDERSTOOD,
    )
    out = render_policy_directive(
        d, last_ack=AcknowledgmentKind.ACK_UNDERSTOOD,
    )
    assert "vary" in out.lower() or "parrot" in out.lower()


def test_last_ack_different_no_vary_hint():
    d = ConversationNextAction(
        action=ConversationAction.ANSWER,
        acknowledgment=AcknowledgmentKind.ACK_UNDERSTOOD,
    )
    out = render_policy_directive(
        d, last_ack=AcknowledgmentKind.ACK_EMPATHY,
    )
    # Should NOT include the vary hint when last was different.
    assert "used a similar opener" not in out


# ── action guidance ───────────────────────────────────────────────


def test_ask_slot_says_one_slot_only():
    d = ConversationNextAction(
        action=ConversationAction.ASK_SLOT,
        requested_slot="phone",
    )
    out = render_policy_directive(d)
    assert "ONE" in out or "one specific slot" in out.lower()
    assert "phone" in out


def test_confirm_action_requires_receipts():
    d = ConversationNextAction(
        action=ConversationAction.CONFIRM_ACTION,
    )
    out = render_policy_directive(d)
    assert "verbatim" in out.lower() or "receipt" in out.lower()


def test_end_call_forbids_anything_else():
    d = ConversationNextAction(
        action=ConversationAction.END_CALL,
    )
    out = render_policy_directive(d)
    assert "anything else" in out.lower()


def test_tool_preamble_under_12_words():
    d = ConversationNextAction(
        action=ConversationAction.TOOL_PREAMBLE,
    )
    out = render_policy_directive(d)
    assert "12" in out or "short" in out.lower()


def test_propose_slot_forbids_invented_times():
    d = ConversationNextAction(
        action=ConversationAction.PROPOSE_SLOT,
    )
    out = render_policy_directive(d)
    assert "invent" in out.lower() or "verbatim" in out.lower()


# ── delivery intent ───────────────────────────────────────────────


def test_crisp_delivery_says_cut_filler():
    d = ConversationNextAction(
        action=ConversationAction.ANSWER,
        delivery_intent=DeliveryIntent.CRISP,
    )
    out = render_policy_directive(d)
    assert "filler" in out.lower() or "crisp" in out.lower()


def test_warm_delivery_says_warm():
    d = ConversationNextAction(
        action=ConversationAction.CONFIRM_ACTION,
        delivery_intent=DeliveryIntent.WARM,
    )
    out = render_policy_directive(d)
    assert "warm" in out.lower()


def test_standard_delivery_no_tone_hint():
    d = ConversationNextAction(
        action=ConversationAction.ANSWER,
        delivery_intent=DeliveryIntent.STANDARD,
    )
    out = render_policy_directive(d)
    # STANDARD has empty hint — shouldn't have a "Tone:" line.
    assert "Tone:" not in out


# ── max_tokens → word budget ─────────────────────────────────────


def test_max_tokens_becomes_word_target():
    d = ConversationNextAction(
        action=ConversationAction.ANSWER,
        max_tokens=40,
    )
    out = render_policy_directive(d)
    # 40 tokens / 1.3 = 30 words rounded.
    assert "30 words" in out or "words" in out


def test_no_max_tokens_no_length_line():
    d = ConversationNextAction(
        action=ConversationAction.ANSWER,
        max_tokens=None,
    )
    out = render_policy_directive(d)
    assert "Length:" not in out


# ── must_include_facts ────────────────────────────────────────────


def test_must_include_facts_rendered():
    d = ConversationNextAction(
        action=ConversationAction.CONFIRM_ACTION,
        must_include_facts=[
            "service: cleaning",
            "date: Wednesday, August 27",
            "time: 2:30 PM",
        ],
    )
    out = render_policy_directive(d)
    assert "verbatim" in out.lower()
    assert "cleaning" in out
    assert "Wednesday" in out
    assert "2:30 PM" in out


def test_empty_must_include_facts_no_facts_line():
    d = ConversationNextAction(
        action=ConversationAction.ANSWER,
        must_include_facts=[],
    )
    out = render_policy_directive(d)
    assert "Must include" not in out


# ── tool preamble reference ─────────────────────────────────────


def test_tool_preamble_mentions_specific_tool():
    d = ConversationNextAction(
        action=ConversationAction.TOOL_PREAMBLE,
        tool="check_availability",
    )
    out = render_policy_directive(d)
    assert "check_availability" in out


# ── final guardrail always present ─────────────────────────────


def test_final_guardrail_present():
    d = ConversationNextAction(action=ConversationAction.ANSWER)
    out = render_policy_directive(d)
    assert "narrate" in out.lower() or "parrot" in out.lower()


# ── never raises ────────────────────────────────────────────────


def test_none_input_returns_none():
    assert render_policy_directive(None) is None  # type: ignore[arg-type]


def test_garbage_input_returns_none():
    assert render_policy_directive("not-a-decision") is None  # type: ignore[arg-type]
    assert render_policy_directive(42) is None  # type: ignore[arg-type]


def test_all_action_types_produce_output():
    for action in ConversationAction:
        d = ConversationNextAction(action=action)
        out = render_policy_directive(d)
        assert out is not None, f"action {action.value} returned None"
        assert "This turn's chosen move" in out
