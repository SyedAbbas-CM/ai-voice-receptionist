"""T-SP1 regression tests — SemanticPlan tool + realizer.

Motivating cases (2026-08-19):
- Caller: "1:30 tomorrow" → check_availability returns [..., 13:30, ...]
  → LLM says "2:30" in reply → booking off by an hour.
  Fix: LLM emits `emit_semantic_plan` with
  PlannedFact(claim="1:30", critical=True); realizer substitutes.
- Caller: "I want tooth implants and a general appointment first"
  → LLM books general, forgets implants.
  Fix: LLM emits pending_tasks=["implant_consult_follow_up"];
  reducer surfaces into _reactive_notes for next turn.

These tests pin the plan_realizer module in isolation.  End-to-end
brain integration is exercised via the actor test suite.
"""
from __future__ import annotations

import pytest

from packages.core_agent.plan_realizer import (
    SEMANTIC_PLAN_TOOL_NAME,
    parse_semantic_plan,
    semantic_plan_tool_definition,
    substitute_critical_facts,
)
from packages.dialogue.plan import (
    DeliveryIntent,
    PlanOperation,
    PlannedFact,
    SemanticPlan,
)


def test_tool_definition_shape():
    """The tool definition must expose the fields the LLM needs and
    map cleanly to OpenAI's function schema."""
    td = semantic_plan_tool_definition()
    assert td.name == SEMANTIC_PLAN_TOOL_NAME
    schema = td.parameters
    assert schema["type"] == "object"
    # Every declared enum value must be a real PlanOperation.
    ops = schema["properties"]["operation"]["enum"]
    for op in ops:
        PlanOperation(op)  # raises if bogus
    # Delivery intent likewise.
    dis = schema["properties"]["delivery_intent"]["enum"]
    for di in dis:
        DeliveryIntent(di)
    # Facts must be structured, not free text.
    facts = schema["properties"]["facts"]
    assert facts["type"] == "array"
    assert facts["items"]["type"] == "object"
    # Required set includes operation only (facts + pending_tasks are optional).
    assert schema["required"] == ["operation"]


def test_parse_semantic_plan_happy_path():
    args = {
        "operation": "propose_action",
        "facts": [
            {"claim": "1:30", "source": "caller", "critical": True},
            {"claim": "tomorrow", "source": "caller", "critical": True},
        ],
        "pending_tasks": ["implant_consult_follow_up"],
        "delivery_intent": "warm",
    }
    plan = parse_semantic_plan(args)
    assert plan is not None
    assert plan.operation == PlanOperation.PROPOSE_ACTION
    assert len(plan.facts) == 2
    assert plan.critical_facts()[0].claim == "1:30"
    assert plan.pending_tasks == ["implant_consult_follow_up"]
    assert plan.delivery_intent == DeliveryIntent.WARM


def test_parse_semantic_plan_missing_operation_returns_none():
    assert parse_semantic_plan({}) is None
    assert parse_semantic_plan({"operation": None}) is None


def test_parse_semantic_plan_invalid_operation_defaults_neutral():
    plan = parse_semantic_plan({"operation": "totally-made-up"})
    assert plan is not None
    assert plan.operation == PlanOperation.NEUTRAL


def test_parse_semantic_plan_strict_invariant_downgrades_to_neutral():
    """ASK_SLOT requires a question — if the LLM sends ASK_SLOT with
    no question, we downgrade to NEUTRAL rather than losing the plan."""
    args = {
        "operation": "ask_slot",
        # missing question
        "facts": [{"claim": "hello", "source": "caller"}],
    }
    plan = parse_semantic_plan(args)
    assert plan is not None
    assert plan.operation == PlanOperation.NEUTRAL


def test_parse_semantic_plan_drops_incomplete_facts():
    args = {
        "operation": "answer_faq",
        "facts": [
            {"claim": "$185", "source": "profile", "critical": True},
            {"claim": "", "source": "profile"},          # dropped
            {"claim": "orphan claim", "source": ""},     # dropped
            {"other": "junk"},                            # dropped
        ],
    }
    plan = parse_semantic_plan(args)
    assert plan is not None
    assert len(plan.facts) == 1
    assert plan.facts[0].claim == "$185"


def test_parse_semantic_plan_never_raises():
    """The whole point is 'broken plan → skip realizer, no crash'."""
    for garbage in ({"operation": 42}, {"operation": "greet", "facts": "not-a-list"},
                    {"operation": "greet", "delivery_intent": 999}):
        parse_semantic_plan(garbage)  # must not raise


# ── substitute_critical_facts ──────────────────────────────────────────

def _plan_with_time(claim: str) -> SemanticPlan:
    return SemanticPlan(
        operation=PlanOperation.PROPOSE_ACTION,
        facts=[PlannedFact(claim=claim, source="caller", critical=True)],
    )


def test_substitute_fixes_digit_time_drift():
    """The Karachi tooth-implants regression: caller said 1:30, LLM
    wrote 2:30, plan carries the correct 1:30.  Realizer swaps."""
    plan = _plan_with_time("1:30")
    reply = "Just to clarify, we have an opening at 2:30 tomorrow, does that work?"
    revised, subs = substitute_critical_facts(reply, plan)
    assert "1:30" in revised
    assert "2:30" not in revised
    assert subs  # substitution logged


def test_substitute_fixes_spelled_out_time_drift():
    """LLM spells the time as 'two thirty' instead of using the plan's '1:30'."""
    plan = _plan_with_time("1:30")
    reply = "We have an opening at two thirty tomorrow."
    revised, subs = substitute_critical_facts(reply, plan)
    assert "1:30" in revised
    assert "two thirty" not in revised.lower()
    assert subs


def test_substitute_no_op_when_reply_already_correct():
    plan = _plan_with_time("1:30")
    reply = "Booked for 1:30 tomorrow, see you then!"
    revised, subs = substitute_critical_facts(reply, plan)
    assert revised == reply
    assert subs == []


def test_substitute_leaves_non_time_facts_alone_for_now():
    """Phase 1: only time-shaped facts are substituted.  Prices/names
    fall through unchanged (a later ship will extend this)."""
    plan = SemanticPlan(
        operation=PlanOperation.CONFIRM_ACTION,
        active_task_id="task_1",
        facts=[PlannedFact(claim="$185", source="profile", critical=True)],
    )
    reply = "That'll be one hundred eighty five dollars."
    revised, subs = substitute_critical_facts(reply, plan)
    # We don't attempt number-word → $ substitution in phase 1.
    assert revised == reply
    assert subs == []


def test_substitute_handles_missing_plan_gracefully():
    revised, subs = substitute_critical_facts("hi", None)  # type: ignore[arg-type]
    assert revised == "hi"
    assert subs == []


def test_substitute_handles_empty_reply():
    plan = _plan_with_time("1:30")
    revised, subs = substitute_critical_facts("", plan)
    assert revised == ""
    assert subs == []


def test_substitute_leaves_reply_when_no_critical_facts():
    """A plan with only non-critical facts must not touch the reply."""
    plan = SemanticPlan(
        operation=PlanOperation.NEUTRAL,
        facts=[PlannedFact(claim="just fyi", source="caller", critical=False)],
    )
    reply = "I hear you, no problem."
    revised, subs = substitute_critical_facts(reply, plan)
    assert revised == reply
    assert subs == []
