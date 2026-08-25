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


class AcknowledgmentKind(str, Enum):
    """Which kind of acknowledgment (if any) the reply should open with.

    2026-08-25 (humanness audit P0.2): the prompt currently tells the LLM
    to vary acks by context (pain → 'Ah, I see'; slot info → 'Yeah,
    Thursday works'; correction → 'Oh sorry'; dictation → silent).  That
    guidance is advisory — under load the LLM ignores it and either says
    the same 'Okay,' every turn or drops acks entirely.

    Moving this out of the LLM's discretion into a deterministic policy
    gives us:
      - Correct choice per turn shape (context/correction/dictation vary
        appropriately)
      - Recency guard (no same ack twice in a row — real receptionists
        never do this, LLMs constantly do)
      - Delivery-intent alignment (RUSHED caller → ACK_NONE + CRISP body;
        UPSET caller → ACK_EMPATHY, not chirpy ACK_UNDERSTOOD)

    The LLM still verbalizes the reply; the policy just decides which
    ack lane to open in.  Prompt tells the LLM 'if action.ack is
    ACK_EMPATHY, open with something like "Ah, I see" or "Got it" but
    never a chirpy chatbot opener like "Sure!"; then move on'.

    Values chosen to be self-documenting for grep-friendliness.
    """

    ACK_NONE = "ack_none"
    """No verbal ack.  Cases:
    - Dictation (caller reading phone/name digits — constant 'okay' is
      patronizing; wait until they finish)
    - Second turn of a series (already acked, don't repeat)
    - RUSHED caller (skip social grease, go straight to answer)
    - Follow-through after we asked a clarifying question (they answered,
      just process)
    """

    ACK_LISTEN = "ack_listen"
    """Backchannel-only 'mmhmm' / 'uh huh' during long caller utterance.
    Fires on Eager EndOfTurn during multi-sentence caller streams.  Never
    commits to content — just signals presence.  Networking's ReactiveBrain
    lane == "backchannel" pairs with this.
    """

    ACK_UNDERSTOOD = "ack_understood"
    """Standard 'got it / yeah / okay' after receiving slot info or a
    factual answer.  Most common ack.  Never say 'gotcha' twice in a
    row (selector enforces via last_ack recency).
    """

    ACK_CORRECTION = "ack_correction"
    """Caller corrected us — 'Oh sorry — 3pm' or 'Right, Thursday not
    Tuesday'.  Different tone from ACK_UNDERSTOOD — briefly apologetic
    without dwelling.  Prevents chirpy 'Perfect!' after a correction.
    """

    ACK_EMPATHY = "ack_empathy"
    """Caller shared pain/context/hardship — 'Ah, I see' or 'That sounds
    rough'.  For dental: 'My tooth's been killing me since Monday' →
    ACK_EMPATHY, not chirpy 'Great!'.  Delivery is WARM.
    """

    ACK_AGREEMENT = "ack_agreement"
    """Caller confirmed our proposal — 'Great, that works' or 'Perfect'.
    Slightly warmer than ACK_UNDERSTOOD; marks the pivot to next step.
    """

    ACK_TRANSITION = "ack_transition"
    """Moving between topics — 'Okay so' or 'Alright, one more thing'.
    Used sparingly; too many transitions in a row read as a bureaucratic
    script.  Selector rate-limits.
    """

    ACK_WAIT = "ack_wait"
    """Caller asked to hold — 'Give me a sec' from THEM.  Response is
    silent.  Do NOT say 'Of course!' or 'Take your time!' — those are
    chatbot tells.  ReactiveBrain silent-lane pairs with this.
    """


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

    # 2026-08-25 (P0.2 ack primitive): tracked for recency-guard so we
    # don't emit the same ack twice in a row.  Reducer populates from
    # the last agent turn's chosen ack (if the reducer has that info)
    # or leaves as None (selector picks fresh).
    last_ack: Optional["AcknowledgmentKind"] = None
    # Signals the caller is dictating a slot value (phone digits, name
    # spelling, address).  Selector uses this to return ACK_NONE — real
    # receptionists don't say "okay" between each digit.  Reducer sets
    # from either an explicit slot-capture flag OR pattern match on
    # last caller text (digit run, spelled-out sequence).
    caller_is_dictating: bool = False
    # Signals the caller explicitly asked to hold — "one sec / hold on /
    # give me a moment".  Selector returns ACK_WAIT (silent).
    caller_asked_to_wait: bool = False
    # Signals the caller corrected us — "No, I said Thursday" / "actually
    # 3pm".  Selector returns ACK_CORRECTION.
    caller_corrected_us: bool = False
    # Signals the caller shared pain / hardship / context ("my tooth's
    # been killing me").  Selector returns ACK_EMPATHY with WARM delivery.
    caller_shared_hardship: bool = False


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
    # 2026-08-25 (P0.2 ack primitive): which ack lane to open in.
    # None = policy hasn't decided (backward compat with pre-2026-08-25
    # callers that skipped this).  ACK_NONE = deliberate no-ack.  Any
    # other value = policy selected an ack shape; prompt should render
    # accordingly.
    acknowledgment: Optional[AcknowledgmentKind] = None


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
        """Return the next action for this turn. Never raises.

        2026-08-25 (P0.2 ack primitive): every returned action carries an
        `acknowledgment` field selected by `_select_ack(state, action,
        delivery_intent)`.  ACK_NONE is a valid answer — some turn shapes
        (dictation, RUSHED caller, follow-through) genuinely want no ack.
        """

        # Emergency short-circuit — always ESCALATE.
        if state.urgency == Urgency.EMERGENCY:
            action = ConversationAction.ESCALATE
            delivery = DeliveryIntent.CRISP
            return ConversationNextAction(
                action=action,
                delivery_intent=delivery,
                max_tokens=96,
                acknowledgment=self._select_ack(state, action, delivery),
            )

        # Pending tool result → we're waiting; use the interval to preamble.
        if state.tool_pending:
            action = ConversationAction.TOOL_PREAMBLE
            delivery = DeliveryIntent.STANDARD
            return ConversationNextAction(
                action=action,
                delivery_intent=delivery,
                max_tokens=32,
                acknowledgment=self._select_ack(state, action, delivery),
            )

        # Booking confirmation owed → readback with facts.
        if state.requires_confirmation:
            action = ConversationAction.CONFIRM_ACTION
            delivery = DeliveryIntent.WARM
            return ConversationNextAction(
                action=action,
                delivery_intent=delivery,
                max_tokens=80,
                must_include_facts=[
                    f"{k}: {v}" for k, v in state.known.items()
                    if k in ("service", "date", "time", "caller_name")
                ],
                acknowledgment=self._select_ack(state, action, delivery),
            )

        # We have missing slots → ask for the next one.
        if state.missing:
            slot = state.missing[0]
            action = ConversationAction.ASK_SLOT
            delivery = DeliveryIntent.STANDARD
            return ConversationNextAction(
                action=action,
                requested_slot=slot,
                delivery_intent=delivery,
                max_tokens=40,
                acknowledgment=self._select_ack(state, action, delivery),
            )

        # Opening turn → ACKNOWLEDGE (short greeting response).
        if state.conversation_phase == ConversationPhase.OPENING:
            action = ConversationAction.ACKNOWLEDGE
            delivery = DeliveryIntent.WARM
            return ConversationNextAction(
                action=action,
                delivery_intent=delivery,
                max_tokens=20,
                acknowledgment=self._select_ack(state, action, delivery),
            )

        # Wrapping phase after commit → END_CALL.
        if state.conversation_phase == ConversationPhase.WRAPPING:
            action = ConversationAction.END_CALL
            delivery = DeliveryIntent.WARM
            return ConversationNextAction(
                action=action,
                delivery_intent=delivery,
                max_tokens=32,
                acknowledgment=self._select_ack(state, action, delivery),
            )

        # Default: let the LLM answer. Adjusts brevity via delivery_intent.
        delivery = DeliveryIntent.CRISP if state.caller_affect == CallerAffect.RUSHED else DeliveryIntent.STANDARD
        action = ConversationAction.ANSWER
        return ConversationNextAction(
            action=action,
            delivery_intent=delivery,
            max_tokens=48,
            acknowledgment=self._select_ack(state, action, delivery),
        )

    # ── ack selector ────────────────────────────────────────────────
    #
    # Decision order (first match wins):
    #   1. Explicit caller-state signals (hardship / correction / wait /
    #      dictation) — these are dominant regardless of action shape.
    #   2. Action type overrides — TOOL_PREAMBLE / CONFIRM_ACTION / ESCALATE
    #      have canonical acks that don't depend on affect.
    #   3. Delivery intent — RUSHED/CRISP means ACK_NONE unless the caller
    #      specifically shared context first.
    #   4. Recency guard — never emit the same ack twice in a row.  If the
    #      first-choice ack matches state.last_ack, fall to a second choice.
    #
    # This is deliberately a rule-based ladder rather than a scoring model
    # — makes it debuggable in prod logs.  Grep for ACK_SELECTED lines.

    def _select_ack(
        self,
        state: ConversationDecisionState,
        action: ConversationAction,
        delivery: DeliveryIntent,
    ) -> AcknowledgmentKind:
        """Pick the ack lane for this turn.  Never raises."""
        first_choice = self._first_choice_ack(state, action, delivery)
        # Recency guard: if we'd repeat the last ack, and it's not one of
        # the "canonical" acks that MUST fire regardless (CORRECTION,
        # EMPATHY, WAIT — those we let repeat because the state that
        # triggers them is dominant), fall to a milder second choice.
        must_repeat = {
            AcknowledgmentKind.ACK_CORRECTION,
            AcknowledgmentKind.ACK_EMPATHY,
            AcknowledgmentKind.ACK_WAIT,
            AcknowledgmentKind.ACK_NONE,   # NONE stacking is fine
        }
        if (
            state.last_ack is not None
            and first_choice == state.last_ack
            and first_choice not in must_repeat
        ):
            return self._second_choice_ack(first_choice)
        return first_choice

    @staticmethod
    def _first_choice_ack(
        state: ConversationDecisionState,
        action: ConversationAction,
        delivery: DeliveryIntent,
    ) -> AcknowledgmentKind:
        # 1) Dominant caller-state signals.
        if state.caller_is_dictating:
            return AcknowledgmentKind.ACK_NONE
        if state.caller_asked_to_wait:
            return AcknowledgmentKind.ACK_WAIT
        if state.caller_corrected_us:
            return AcknowledgmentKind.ACK_CORRECTION
        if state.caller_shared_hardship:
            return AcknowledgmentKind.ACK_EMPATHY

        # 2) Action-type canonical acks.
        if action == ConversationAction.ESCALATE:
            # Emergency — do NOT open with "gotcha" or chatbot ack.
            # Empathy is closer to what a receptionist does — brief
            # "oh no" implicit, then move to action.  ACK_NONE keeps
            # the message CRISP; the ESCALATE body carries the empathy.
            return AcknowledgmentKind.ACK_NONE
        if action == ConversationAction.TOOL_PREAMBLE:
            # "Let me check that for you" — brief transitional ack.
            return AcknowledgmentKind.ACK_TRANSITION
        if action == ConversationAction.CONFIRM_ACTION:
            # Verbatim booking readback — start with agreement then
            # facts.  ACK_AGREEMENT primes the tone.
            return AcknowledgmentKind.ACK_AGREEMENT
        if action == ConversationAction.REPAIR_MISHEAR:
            # We misheard — sorry-shaped ack.
            return AcknowledgmentKind.ACK_CORRECTION

        # 3) Delivery-intent gates.
        if delivery == DeliveryIntent.CRISP:
            # RUSHED caller — skip the social grease.
            return AcknowledgmentKind.ACK_NONE

        # 4) Phase-based defaults.
        if action == ConversationAction.ACKNOWLEDGE:
            # Opening / plain acknowledge turn.  Use a mild UNDERSTOOD.
            return AcknowledgmentKind.ACK_UNDERSTOOD
        if action == ConversationAction.ASK_SLOT:
            # Asking next slot after receiving previous info.
            return AcknowledgmentKind.ACK_UNDERSTOOD
        if action == ConversationAction.PROPOSE_SLOT:
            return AcknowledgmentKind.ACK_UNDERSTOOD
        if action == ConversationAction.CLARIFY:
            # Clarification — brief bewilderment lands, but a full
            # correction-ack is too heavy.  Use TRANSITION as a soft
            # "wait, let me make sure".
            return AcknowledgmentKind.ACK_TRANSITION
        if action == ConversationAction.END_CALL:
            # Farewell — should just be the farewell.  No ack.
            return AcknowledgmentKind.ACK_NONE

        # ANSWER (default action) — standard understood ack.
        return AcknowledgmentKind.ACK_UNDERSTOOD

    @staticmethod
    def _second_choice_ack(first: AcknowledgmentKind) -> AcknowledgmentKind:
        """Recency-guard fallback: when first_choice would repeat last_ack.

        Table intentionally small — most acks that repeat are mundane
        (UNDERSTOOD/TRANSITION), so alternates cycle among safe variants.
        Nothing here escalates severity (never map UNDERSTOOD → EMPATHY).
        """
        return {
            AcknowledgmentKind.ACK_UNDERSTOOD: AcknowledgmentKind.ACK_NONE,
            AcknowledgmentKind.ACK_TRANSITION: AcknowledgmentKind.ACK_UNDERSTOOD,
            AcknowledgmentKind.ACK_AGREEMENT: AcknowledgmentKind.ACK_UNDERSTOOD,
            AcknowledgmentKind.ACK_LISTEN: AcknowledgmentKind.ACK_NONE,
        }.get(first, AcknowledgmentKind.ACK_NONE)


__all__ = [
    "ConversationAction",
    "ConversationPhase",
    "CallerAffect",
    "CallerStyle",
    "Urgency",
    "DeliveryIntent",
    "AcknowledgmentKind",
    "ConversationDecisionState",
    "ConversationNextAction",
    "NextActionPolicy",
]
