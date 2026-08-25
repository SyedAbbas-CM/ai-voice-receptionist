"""Turn-level ConversationState + NextActionPolicy — P7 scaffold.

Status: scaffolded 2026-08-23, NOT WIRED TO RUNTIME.

Purpose (master TODO P7): move the "what should I say next" decision out of
free-form LLM improvisation into a small deterministic policy layer, so the
LLM's job becomes "verbalize this specific action" rather than "choose,
decide, and verbalize all at once."

This module exposes two dataclasses + a policy class. It is imported by:
- `packages/core_agent/brain.py` (once wiring lands): to call the policy
  BEFORE each LLM turn and inject the chosen action into the system prompt.
- `packages/dialogue/reducer.py` (once wiring lands): to update the decision
  state from turn events.

The prompt scaffold in `packages/core_agent/prompt.py` § CURRENT CONVERSATION
STATE (shipped 2026-08-22 Ship 8) already provides an anchor. When P7 wires
in, we substitute real values into that section.

Test coverage: `apps/api/tests/test_next_action_policy_scaffold.py` — pins
dataclass shape and default policy decisions. Real behavioral coverage lands
with the wiring PR.

Design notes:
- `ConversationDecisionState` COMPOSES the existing DialogueState + business
  profile + extracted fields. Does not duplicate.
- `ConversationNextAction` is the OUTPUT — a single decided move + the
  parameters the LLM needs to verbalize it. `must_include_facts` is the
  verbatim-substitution list (booking readback, quoted times, etc.).
- Policy default fallbacks are safe: if the state doesn't fit any explicit
  rule, return ANSWER with no facts — matches current LLM-improvise behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ── decision enums ─────────────────────────────────────────────────


class ConversationAction(str, Enum):
    """The single spoken move the LLM should verbalize this turn.

    Ordered roughly by increasing scope. Naming matches master TODO P7."""

    ACKNOWLEDGE = "acknowledge"       # "Yeah, absolutely." — no info
    CLARIFY = "clarify"               # "Sorry — Tuesday or Thursday?"
    ASK_SLOT = "ask_slot"             # "Do mornings or afternoons work?"
    ANSWER = "answer"                 # direct info answer, may have follow-up
    TOOL_PREAMBLE = "tool_preamble"   # "I'll check Tuesday afternoon."
    PROPOSE_SLOT = "propose_slot"     # "I've got 2:30 or 4 — which works?"
    CONFIRM_ACTION = "confirm_action" # verbatim booking readback
    REPAIR_MISHEAR = "repair_mishear" # "Sorry — I got 'Oliver' but missed the rest."
    ESCALATE = "escalate"             # transfer / emergency guidance
    END_CALL = "end_call"             # final farewell


class ConversationPhase(str, Enum):
    """Where in the arc of a call we are. Loosely correlates with which
    actions are plausible."""

    OPENING = "opening"           # greeting through initial "how can I help"
    DISCOVERY = "discovery"       # figuring out what caller wants
    INFO_GATHER = "info_gather"   # collecting slots for a task
    TOOL_WORK = "tool_work"       # tool call in flight / awaiting result
    PROPOSING = "proposing"       # offering slots / options
    CONFIRMING = "confirming"     # verbatim readback before commit
    WRAPPING = "wrapping"         # post-commit close
    ENDED = "ended"               # farewell said, awaiting hangup


class CallerAffect(str, Enum):
    """Emotional tone. Default `neutral` when uninferred."""

    NEUTRAL = "neutral"
    RUSHED = "rushed"
    CASUAL = "casual"
    CONFUSED = "confused"
    FORMAL = "formal"
    UPSET = "upset"
    ANXIOUS = "anxious"


class CallerStyle(str, Enum):
    """Communication style. Independent from affect."""

    BRIEF = "brief"       # short answers, direct
    CHATTY = "chatty"     # elaborates, adds context
    FORMAL = "formal"
    HALTING = "halting"   # pauses, thinks aloud


class Urgency(str, Enum):
    """Time pressure the caller is under. Drives brevity + tool priority."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EMERGENCY = "emergency"


class DeliveryIntent(str, Enum):
    """How the chosen action should be spoken. Modifies token budget +
    prosodic guidance without changing the action itself."""

    STANDARD = "standard"
    WARM = "warm"           # reassuring, softer
    CRISP = "crisp"         # short, direct, no filler
    APOLOGETIC = "apologetic"


# ── input state ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConversationDecisionState:
    """Snapshot of everything the policy needs to decide the next action.

    Populated once per turn by the reducer, immediately before the policy
    runs. Frozen so the policy can't accidentally mutate it.

    Field descriptions (all default to sensible neutrals when uninferred):
      conversation_phase: where we are in the call arc.
      caller_affect: emotional tone. NEUTRAL if not yet inferred.
      caller_style: brief/chatty/etc. BRIEF as safe default.
      urgency: time pressure. LOW default.
      known: slot name → value the caller has already told us.
      missing: slot names still needed to complete current task.
      tool_pending: True if a tool call is in flight this turn.
      requires_confirmation: True if we owe the caller a verbatim readback.
      pending_tasks: labels of secondary intents the caller mentioned but
                     we haven't addressed yet.
      last_caller_text: raw last caller utterance, for repair/clarify decisions.
      last_agent_text: what we last said, for anti-repeat guard.
    """

    conversation_phase: ConversationPhase = ConversationPhase.DISCOVERY
    caller_affect: CallerAffect = CallerAffect.NEUTRAL
    caller_style: CallerStyle = CallerStyle.BRIEF
    urgency: Urgency = Urgency.LOW
    known: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    tool_pending: bool = False
    requires_confirmation: bool = False
    pending_tasks: list[str] = field(default_factory=list)
    last_caller_text: str = ""
    last_agent_text: str = ""


# ── output decision ────────────────────────────────────────────────


@dataclass(frozen=True)
class ConversationNextAction:
    """The single decided move + parameters the LLM should verbalize.

    Semantics:
      action: which speech act to perform. Never None.
      requested_slot: for ASK_SLOT / CLARIFY — which slot we're asking for.
      tool: for TOOL_PREAMBLE — which tool we're about to call, so the
            preamble text can be truthful ("checking availability" vs "pulling
            up your record").
      delivery_intent: how the LLM should shape the reply. Modifies max_tokens
                       and prosody, doesn't override the action.
      max_tokens: hard cap for this turn's LLM output. Mirrors the speech-act
                  budgets in packages/core_agent/token_budgets.py — the policy
                  chooses the right budget for the chosen action.
      must_include_facts: verbatim strings the LLM MUST include (booking time,
                          confirmation number, quoted price). Runtime rewrite
                          layer enforces these.
    """

    action: ConversationAction
    requested_slot: Optional[str] = None
    tool: Optional[str] = None
    delivery_intent: DeliveryIntent = DeliveryIntent.STANDARD
    max_tokens: Optional[int] = None
    must_include_facts: list[str] = field(default_factory=list)


# ── policy ─────────────────────────────────────────────────────────


class NextActionPolicy:
    """Decides the next spoken move from ConversationDecisionState.

    Current implementation is a rule-based baseline: it covers the obvious
    cases (opening → GREETING via ACKNOWLEDGE, tool_pending → TOOL_PREAMBLE,
    requires_confirmation → CONFIRM_ACTION) and falls back to ANSWER for
    everything else. That keeps behavior identical to current LLM-improvise
    on ambiguous turns.

    Future iteration: replace the ANSWER fallback with more discriminating
    rules once affect/style/urgency inference is populated by the reducer.
    """

    def decide(self, state: ConversationDecisionState) -> ConversationNextAction:
        """Return the next action for this turn. Never raises."""

        # Emergency short-circuit — always ESCALATE.
        if state.urgency == Urgency.EMERGENCY:
            return ConversationNextAction(
                action=ConversationAction.ESCALATE,
                delivery_intent=DeliveryIntent.CRISP,
                max_tokens=96,
            )

        # Pending tool result → we're waiting; use the interval to preamble.
        if state.tool_pending:
            return ConversationNextAction(
                action=ConversationAction.TOOL_PREAMBLE,
                delivery_intent=DeliveryIntent.STANDARD,
                max_tokens=32,
            )

        # Booking confirmation owed → readback with facts.
        if state.requires_confirmation:
            return ConversationNextAction(
                action=ConversationAction.CONFIRM_ACTION,
                delivery_intent=DeliveryIntent.WARM,
                max_tokens=80,
                must_include_facts=[
                    f"{k}: {v}" for k, v in state.known.items()
                    if k in ("service", "date", "time", "caller_name")
                ],
            )

        # We have missing slots → ask for the next one.
        if state.missing:
            slot = state.missing[0]
            return ConversationNextAction(
                action=ConversationAction.ASK_SLOT,
                requested_slot=slot,
                delivery_intent=DeliveryIntent.STANDARD,
                max_tokens=40,
            )

        # Opening turn → ACKNOWLEDGE (short greeting response).
        if state.conversation_phase == ConversationPhase.OPENING:
            return ConversationNextAction(
                action=ConversationAction.ACKNOWLEDGE,
                delivery_intent=DeliveryIntent.WARM,
                max_tokens=20,
            )

        # Wrapping phase after commit → END_CALL.
        if state.conversation_phase == ConversationPhase.WRAPPING:
            return ConversationNextAction(
                action=ConversationAction.END_CALL,
                delivery_intent=DeliveryIntent.WARM,
                max_tokens=32,
            )

        # Default: let the LLM answer. Adjusts brevity via delivery_intent.
        delivery = DeliveryIntent.CRISP if state.caller_affect == CallerAffect.RUSHED else DeliveryIntent.STANDARD
        return ConversationNextAction(
            action=ConversationAction.ANSWER,
            delivery_intent=delivery,
            max_tokens=48,
        )


__all__ = [
    "ConversationAction",
    "ConversationPhase",
    "CallerAffect",
    "CallerStyle",
    "Urgency",
    "DeliveryIntent",
    "ConversationDecisionState",
    "ConversationNextAction",
    "NextActionPolicy",
]
