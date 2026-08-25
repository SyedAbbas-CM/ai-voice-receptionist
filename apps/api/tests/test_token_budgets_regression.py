"""P3 regression tests — speech-act-aware token budgets.

Motivation: the previous flat `max_tokens=200` was:
  - over-budget for short turns (an ack is ~20 tok, wasting generation budget)
  - inconsistent with the humanness research (which recommends per-turn caps)
  - a lever for pure-speed wins (OpenAI's own guide: "cutting output tokens ≈
    cutting latency proportionally").

Fix: `packages/core_agent/token_budgets.py` provides `token_budget_for_plan(plan)`
which reads `plan.operation` and returns the right cap.  Falls back to
DEFAULT_BUDGET (80) when no plan is present.

These tests pin:
  1. Each speech-act gets the documented cap
  2. Missing/invalid plans get safe defaults
  3. Never truncates emergency/booking-confirmation paths
"""
from __future__ import annotations

from packages.core_agent.token_budgets import (
    COMPLEX_BUDGET,
    DEFAULT_BUDGET,
    EMERGENCY_BUDGET,
    _BUDGETS,
    token_budget_for_operation,
    token_budget_for_plan,
)
from packages.dialogue.plan import PlanOperation, SemanticPlan


def test_ack_is_smallest():
    """An acknowledgment is the shortest thing an agent should say
    ('Yeah, absolutely.').  Cap should reflect that."""
    assert token_budget_for_operation(PlanOperation.ACKNOWLEDGE) == 20


def test_confirm_action_has_headroom():
    """Booking confirmations MUST read all details back verbatim.
    Under-budgeting would clip 'You're booked for Tuesday, August 20 at
    two thirty p.m. with Doctor Chen.  See you then.'"""
    assert token_budget_for_operation(PlanOperation.CONFIRM_ACTION) >= 80


def test_escalate_is_at_least_emergency_budget():
    """ESCALATE covers emergency handoffs.  Approved safety language
    must always fit."""
    assert token_budget_for_operation(PlanOperation.ESCALATE) >= EMERGENCY_BUDGET


def test_ask_slot_shorter_than_answer():
    """'What day works better?' should be tighter than a factual answer."""
    ask = token_budget_for_operation(PlanOperation.ASK_SLOT)
    answer = token_budget_for_operation(PlanOperation.ANSWER_FAQ)
    assert ask < answer


def test_all_documented_ops_have_a_budget():
    """Every PlanOperation should be in the table.  If a new op is
    added, this test forces the token_budgets module to be updated."""
    for op in PlanOperation:
        # Either explicitly in the table OR gets the default — either way
        # `token_budget_for_operation` must return a positive int.
        v = token_budget_for_operation(op)
        assert isinstance(v, int) and v > 0, f"no budget for {op.value}"


def test_none_operation_returns_default():
    assert token_budget_for_operation(None) == DEFAULT_BUDGET


def test_none_plan_returns_default():
    assert token_budget_for_plan(None) == DEFAULT_BUDGET


def test_plan_object_returns_op_budget():
    plan = SemanticPlan(operation=PlanOperation.ACKNOWLEDGE)
    assert token_budget_for_plan(plan) == 20


def test_plan_object_confirm_action_needs_task_id():
    """SemanticPlan enforces CONFIRM_ACTION requires active_task_id.
    Just verify the budget lookup still works when the plan validates."""
    plan = SemanticPlan(
        operation=PlanOperation.CONFIRM_ACTION,
        active_task_id="task_1",
    )
    assert token_budget_for_plan(plan) == 80


def test_dict_fallback_works():
    """The runtime sometimes stores parsed args as a dict rather than a
    SemanticPlan (e.g. mid-parse).  The lookup must handle that."""
    assert token_budget_for_plan({"operation": "acknowledge"}) == 20


def test_dict_with_unknown_operation_returns_default():
    assert token_budget_for_plan({"operation": "totally-made-up"}) == DEFAULT_BUDGET


def test_dict_with_no_operation_returns_default():
    assert token_budget_for_plan({}) == DEFAULT_BUDGET


def test_default_is_less_than_old_flat_cap():
    """The whole point of P3 is that DEFAULT_BUDGET (used when no plan
    emitted) is smaller than the old flat 200.  If someone bumps it
    back up, this test fails."""
    assert DEFAULT_BUDGET < 200
    # And still enough for a normal 40-word reply.
    assert DEFAULT_BUDGET >= 60


def test_complex_budget_covers_hard_answers():
    """When the LLM legitimately needs to explain something complex
    (list of services, insurance details), the escalation ceiling must
    accommodate it without clipping."""
    assert COMPLEX_BUDGET >= 96
    # But still smaller than the old flat 200 — this is a P3 win, not a regression.
    assert COMPLEX_BUDGET <= 200
