"""Semantic Plan Protocol — Sprint 10 Track A2.

The current semantic planner is post-hoc classification (reply text
already generated, then tagged with a speech act).  A real semantic
plan is emitted BEFORE wording:

  * What task is active?
  * What operation is this turn — ask, answer, verify, propose, commit?
  * Which facts should be communicated?
  * What question, if any?
  * What CANNOT be claimed?
  * What input do we expect next?

The realizer then generates one concise utterance from the plan.  For
critical operations (booking confirmation), the utterance is a
deterministic template — the model doesn't get to reinterpret names,
dates, phones, or amounts.

Kept as data types + validation here.  The realizer + LLM prompt for
producing plans live in `packages/core_agent/planners/semantic_v2.py`
(added in a follow-up commit).
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class PlanOperation(str, Enum):
    """What kind of turn is this?

    Each operation constrains what the realizer can say.  A
    `commit_action` plan MUST use a deterministic template built from
    the committed_action_id's data, not generated prose."""
    GREET = "greet"
    ASK_SLOT = "ask_slot"                    # "What's your phone number?"
    ANSWER_FAQ = "answer_faq"                # from RAG evidence
    ANSWER_FROM_PROFILE = "answer_from_profile"
    OFFER_SLOTS = "offer_slots"              # "I have 10am, 10:30, or 2pm"
    PROPOSE_ACTION = "propose_action"        # "Book Sarah for 10:30?"
    CONFIRM_ACTION = "confirm_action"        # "Booked — you'll get a text"
    ACKNOWLEDGE = "acknowledge"              # "Got it" / "One moment"
    APOLOGIZE = "apologize"                  # "Sorry, we don't have Tuesday"
    ESCALATE = "escalate"                    # "Let me get you to a human"
    HANDOFF_INFO = "handoff_info"            # "Our office manager will call you back"
    ASK_CORRECTION = "ask_correction"        # "Did you say Tuesday or Thursday?"
    NEUTRAL = "neutral"                      # anything else


class DeliveryIntent(str, Enum):
    """Hint to the performance planner about desired tone.

    Overlaps with VPL SpeechAct but is a superset — driven by
    operation + caller state.  Kept separate so the plan can request
    a specific tone independent of the operation type."""
    WARM = "warm"
    REASSURING = "reassuring"
    PROFESSIONAL = "professional"
    URGENT = "urgent"
    APOLOGETIC = "apologetic"
    NEUTRAL = "neutral"


class PlannedFact(BaseModel):
    """One piece of information the realizer is allowed to say.

    Every fact carries a source_id — a reference to where this claim
    came from (tool result ID, business profile section, RAG chunk
    source).  If a fact has no source, the realizer MUST NOT include
    it.  This is the guardrail against hallucination: no unsourced
    claims in the mouth of the agent."""
    claim: str
    source: str
    """Reference identifier — e.g. "tool:availability_27",
    "business_profile:hours.thursday", "rag:insurance-policy-v7"."""
    critical: bool = False
    """When True, the realizer MUST include this fact and MUST NOT
    paraphrase it — use verbatim.  Used for dates, times, prices,
    phone numbers, appointment details."""


class PlannedQuestion(BaseModel):
    """The one question this turn will ask (if any).

    Kept as a single-question schema — multi-question turns are a
    smell (caller can only answer one thing).  If the plan needs to
    ask two things, split into two turns or use ASK_SLOT with a
    compound `slot_name`."""
    purpose: str
    """Machine-readable — e.g. "slot_confirmation", "clarify_date",
    "ask_phone".  Drives the reducer's expected_next_input."""
    text_goal: str
    """What the realizer should try to say.  Not the exact wording —
    the realizer picks phrasing."""


class SemanticPlan(BaseModel):
    """One turn's semantic plan.

    Emitted by the semantic planner before the realizer generates
    speech.  Validated deterministically before the realizer runs.

    Invariants (enforced by model_validator):
      * If operation is CONFIRM_ACTION, active_task_id must be set.
      * If operation is ASK_SLOT, question must be set.
      * If operation is ANSWER_FAQ or ANSWER_FROM_PROFILE, facts must
        be non-empty.
      * forbidden_claims and facts must not overlap.
      * critical facts must remain UNPARAPHRASED — realizer contract."""
    active_task_id: Optional[str] = None
    operation: PlanOperation
    facts: list[PlannedFact] = Field(default_factory=list)
    question: Optional[PlannedQuestion] = None
    pending_tasks: list[str] = Field(default_factory=list)
    """task_ids the plan intentionally does NOT address this turn
    (deferred multi-intent).  Used to compose acknowledgments like
    'I can help with the booking; the insurance question we can come
    back to.'"""
    forbidden_claims: list[str] = Field(default_factory=list)
    """Explicit list of things the realizer MUST NOT say.  Used to
    prevent premature confirmations ("The booking is complete" before
    the tool returns) and unsupported claims ("Your insurance will
    cover this")."""
    expected_next_input: list[str] = Field(default_factory=list)
    """Machine-readable labels for the caller's expected next answer.
    E.g. ["accept_slot", "reject_slot", "request_alternative"].
    Drives disambiguation of a subsequent "yes" (which action was
    accepted?)."""
    delivery_intent: DeliveryIntent = DeliveryIntent.NEUTRAL

    model_config = {"frozen": False}   # semantic planner may amend before validate

    @model_validator(mode="after")
    def _check_operation_invariants(self) -> "SemanticPlan":
        op = self.operation
        if op == PlanOperation.CONFIRM_ACTION and not self.active_task_id:
            raise ValueError("CONFIRM_ACTION requires active_task_id")
        if op == PlanOperation.ASK_SLOT and self.question is None:
            raise ValueError("ASK_SLOT requires question")
        if op in (PlanOperation.ANSWER_FAQ, PlanOperation.ANSWER_FROM_PROFILE):
            if not self.facts:
                raise ValueError(f"{op.value} requires at least one PlannedFact")
        # Sanity: forbidden claims shouldn't literal-match a stated fact
        fact_claims = {f.claim.strip().lower() for f in self.facts}
        for fc in self.forbidden_claims:
            if fc.strip().lower() in fact_claims:
                raise ValueError(
                    f"forbidden_claim {fc!r} contradicts a stated fact"
                )
        return self

    # ── realizer contract helpers ───────────────────────────────────

    def critical_facts(self) -> list[PlannedFact]:
        """Facts the realizer must include verbatim."""
        return [f for f in self.facts if f.critical]

    def optional_facts(self) -> list[PlannedFact]:
        return [f for f in self.facts if not f.critical]

    def requires_deterministic_template(self) -> bool:
        """True when the utterance MUST come from a template, not from
        LLM generation.  Currently: any CONFIRM_ACTION.  Extension
        point for PROPOSE_ACTION on high-stakes tasks (payment)."""
        return self.operation == PlanOperation.CONFIRM_ACTION
