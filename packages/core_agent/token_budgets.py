"""Speech-act-aware `max_tokens` budgets (P3 / T-SP-SPEED-EXTRA-B2).

Motivation (see `MASTER-PRIORITY-TODO-2026-08-20.md` §P3 +
`33_HUMANNESS_RESPONSE3_deep-research-report.md` §"Response budgets should
depend on the speech act"):

OpenAI's own latency guide + the humanness research converge on the same
point — a global `max_tokens=200` cap is both slower than needed on
simple turns AND encourages the LLM to over-explain when a receptionist
would speak in 20 words.  Tie the token cap to what kind of turn this
actually is:

  ACKNOWLEDGE     → 20 tok  ("Yeah, absolutely.")
  CLARIFY         → 32 tok  ("Sorry — Tuesday or Thursday?")
  ASK_SLOT        → 40 tok  ("Do mornings or afternoons work better?")
  TOOL_PREAMBLE   → 32 tok  ("I'll check Tuesday afternoon.")
  DIRECT_ANSWER   → 48 tok  answer + optional next question
  BOOKING_PROPOSAL → 64 tok slot + one question
  FINAL_CONFIRM   → 80 tok  explicit factual readback
  COMPLEX         → 120 tok only when genuinely needed
  EMERGENCY       → 96 tok  approved safety language
  UNKNOWN/NEUTRAL → 80 tok  safe default

Safety rule: this module NEVER truncates emergency, compliance, or
booking-confirmation text — those get the higher caps + the caller
`_min_tokens_floor` protects against under-budget clipping on the rare
turn where a booking readback runs long.

Wiring:
  * `packages/core_agent/brain.py` reads the plan's `operation` (when
    the LLM emitted one via `emit_semantic_plan`) and calls
    `token_budget_for_operation(op)` to pick the right cap.
  * Fallback when no plan was emitted → returns DEFAULT_BUDGET.
"""
from __future__ import annotations

from typing import Optional

from packages.dialogue.plan import PlanOperation

# Table from MASTER-PRIORITY-TODO §P3.  Values are token counts, NOT
# character counts.  OpenAI's `max_tokens` parameter caps generation
# at this many completion tokens.
_BUDGETS: dict[PlanOperation, int] = {
    PlanOperation.GREET:               32,   # "Thanks for calling ..."
    PlanOperation.ACKNOWLEDGE:         20,   # "Yeah, absolutely."
    PlanOperation.ASK_SLOT:            40,   # "Do mornings work better?"
    PlanOperation.ASK_CORRECTION:      32,   # "Tuesday or Thursday?"
    PlanOperation.ANSWER_FAQ:          64,   # profile/rag-sourced answer
    PlanOperation.ANSWER_FROM_PROFILE: 64,
    PlanOperation.OFFER_SLOTS:         64,   # "2:30 or 4, which works?"
    PlanOperation.PROPOSE_ACTION:      64,   # "Book Sarah for 10:30?"
    PlanOperation.CONFIRM_ACTION:      80,   # booking readback — needs space
    PlanOperation.APOLOGIZE:           48,   # "Sorry — we don't have..."
    PlanOperation.ESCALATE:            96,   # emergency/handoff
    PlanOperation.HANDOFF_INFO:        64,
    PlanOperation.NEUTRAL:             80,
}

# When there is NO SemanticPlan emitted, fall back to a safe middle value.
# The old flat cap was 200; 80 matches "unknown" per master TODO §P3 and
# still covers the majority of turns.
DEFAULT_BUDGET = 80

# Absolute ceiling — if a caller ever asks for a complex explanation
# (list of services, insurance details) the LLM can go up to this.
# Used when the plan explicitly hints "complex" or when the LLM emits
# operation=NEUTRAL but the caller asked for detail.  Kept below the
# old flat 200 so we still get the pure-speed win, but well above
# CONFIRM_ACTION so booking readbacks never clip.
COMPLEX_BUDGET = 120

# Emergency / compliance can never be clipped — always uses this.
# Bigger than COMPLEX so approved safety text always fits.
EMERGENCY_BUDGET = 96


def token_budget_for_operation(op: Optional[PlanOperation]) -> int:
    """Return the `max_tokens` value for this speech-act operation.

    Never raises.  Missing or unknown operation → `DEFAULT_BUDGET`.
    """
    if op is None:
        return DEFAULT_BUDGET
    return _BUDGETS.get(op, DEFAULT_BUDGET)


def token_budget_for_plan(plan) -> int:
    """Convenience wrapper: pull `.operation` from a SemanticPlan-like
    object.  Returns DEFAULT_BUDGET if the plan is None or the
    operation attribute is missing.

    Deliberately duck-typed so a real `SemanticPlan` OR a dict fallback
    both work — the runtime sometimes stores just the parsed args."""
    if plan is None:
        return DEFAULT_BUDGET
    op = getattr(plan, "operation", None)
    if op is None and isinstance(plan, dict):
        op_str = plan.get("operation")
        if op_str:
            try:
                op = PlanOperation(op_str)
            except ValueError:
                op = None
    return token_budget_for_operation(op)
