"""Render a NextActionPolicy decision into an LLM system-note directive.

2026-08-27 (task #120): the ACK primitive + TurnSignalReducer are shipped
but they only affect the deterministic post-tool synthesis branch — the
majority of turns (any turn not ending in a booking receipt) still get
"whatever gpt-4o-mini improvises."

This module closes that gap.  brain.py calls `render_policy_directive`
on every turn (feature-flagged) with the current caller-signal state.
The returned string is injected as a system-role message BEFORE the
transcript, telling the LLM WHICH action + WHICH ack shape + HOW brief
to be.  LLM still verbalizes; policy decides the shape.

Design notes:
- The directive is short (~200-400 chars).  gpt-4o-mini follows short
  system-role directives reliably; long ones get ignored.
- The directive is idempotent — running the policy twice yields the
  same directive.  Deterministic, debuggable.
- Never raises.  Any error → returns None → brain skips the injection
  and the LLM behaves as before.
- Every ack lane maps to a specific "open with something like X"
  instruction so the LLM has concrete phrasings, not a category name.

Not covered here (deferred):
- Rendering `must_include_facts` as a fact-list ("you must include:
  X, Y, Z verbatim").  Booking confirmations that reach the LLM path
  need this to prevent time-drift.  Synth already handles the
  deterministic case; LLM-path booking-confirm falls back here.
"""
from __future__ import annotations

from typing import Optional

from packages.dialogue.next_action_policy import (
    AcknowledgmentKind,
    ConversationAction,
    ConversationNextAction,
    DeliveryIntent,
)


# ── ack-lane wording samples ──────────────────────────────────────
#
# For each ack, three example openers.  The LLM sees the list + is told
# "open with something LIKE these — never verbatim two turns in a row."
# Multiple examples per lane prevents the model from parroting one form
# every turn.

_ACK_EXAMPLES: dict[AcknowledgmentKind, tuple[str, ...]] = {
    AcknowledgmentKind.ACK_NONE: (
        # Jump straight to the answer — no ack opener at all.
    ),
    AcknowledgmentKind.ACK_LISTEN: (
        "mmhmm", "uh huh", "right",
    ),
    AcknowledgmentKind.ACK_UNDERSTOOD: (
        "Got it —", "Yeah,", "Okay,",
    ),
    AcknowledgmentKind.ACK_CORRECTION: (
        "Oh — sorry,", "Ah, my mistake —", "Right, sorry —",
    ),
    AcknowledgmentKind.ACK_EMPATHY: (
        "Ah, I see —", "That sounds rough —", "Oh no —",
    ),
    AcknowledgmentKind.ACK_AGREEMENT: (
        "Yeah,", "Great —", "Sounds good —",
    ),
    AcknowledgmentKind.ACK_TRANSITION: (
        "Okay, so —", "Alright,", "Right, one more thing —",
    ),
    AcknowledgmentKind.ACK_WAIT: (
        # Deliberate silence — do NOT verbally ack a caller's "hold on".
        # Reactive-brain-style short "mmhmm" is the max.
    ),
}


# ── action-shape guidance ─────────────────────────────────────────

_ACTION_GUIDANCE: dict[ConversationAction, str] = {
    ConversationAction.ACKNOWLEDGE:
        "Brief acknowledgment only.  No new information, no question.",
    ConversationAction.CLARIFY:
        "Ask ONE specific clarifying question.  Do not list options — pick "
        "the most important thing to clarify.",
    ConversationAction.ASK_SLOT:
        "Ask for ONE specific slot value.  Do NOT list what you already "
        "have — just ask the next thing.",
    ConversationAction.ANSWER:
        "Answer the caller's question directly.  Keep it factual, in "
        "range of the length ladder in your persona.",
    ConversationAction.TOOL_PREAMBLE:
        "Say ONE short 'checking that for you' style sentence.  Under 12 "
        "words.  Do NOT promise anything the tool hasn't returned yet.",
    ConversationAction.PROPOSE_SLOT:
        "Propose the slot options from the tool result.  If the deterministic "
        "renderer handled this, use its output verbatim.  Never invent a "
        "time not in the tool result.",
    ConversationAction.CONFIRM_ACTION:
        "Verbatim readback of the booked action.  Include service, date, time.  "
        "Never confirm without a matching tool receipt this turn.",
    ConversationAction.REPAIR_MISHEAR:
        "Own the mishear briefly ('sorry, I got X').  Then ask for the ONE "
        "piece you missed.",
    ConversationAction.ESCALATE:
        "Transfer to human OR emergency guidance.  Do not attempt to solve.  "
        "Keep it under 15 words.",
    ConversationAction.END_CALL:
        "One warm farewell.  Do NOT ask 'anything else?' — that reopens the "
        "conversation.",
}


# ── delivery intent modifier ─────────────────────────────────────

_DELIVERY_HINT: dict[DeliveryIntent, str] = {
    DeliveryIntent.STANDARD: "",
    DeliveryIntent.WARM: (
        "  Tone: warm, unhurried.  Fine to use one contraction more than "
        "usual."
    ),
    DeliveryIntent.CRISP: (
        "  Tone: crisp — caller is rushed.  Cut all filler.  Shortest "
        "useful reply."
    ),
    DeliveryIntent.APOLOGETIC: (
        "  Tone: brief acknowledgment of the problem, no dwelling."
    ),
}


def render_policy_directive(
    decision: ConversationNextAction,
    *,
    last_ack: Optional[AcknowledgmentKind] = None,
) -> Optional[str]:
    """Render the policy decision into a short system-note directive.

    Returns None on any error (defensive) so brain.py can skip injection.
    Never raises.

    `last_ack` is optional — when supplied and matches the policy's
    chosen ack, we tell the LLM "you used this ack last turn; vary it"
    so the LLM doesn't parrot the same opener twice.
    """
    try:
        if not isinstance(decision, ConversationNextAction):
            return None
        parts: list[str] = ["This turn's chosen move:"]

        # Action guidance.
        action_line = _ACTION_GUIDANCE.get(decision.action)
        if action_line:
            parts.append(f"  Action: {action_line}")
        else:
            parts.append(f"  Action: {decision.action.value}")

        # ACK lane.
        ack = decision.acknowledgment
        if ack == AcknowledgmentKind.ACK_NONE:
            parts.append(
                "  Ack: NO opener — jump straight to the answer.  Chirpy "
                "'Sure!' / 'Absolutely!' openers forbidden."
            )
        elif ack == AcknowledgmentKind.ACK_WAIT:
            parts.append(
                "  Ack: caller asked to hold — respond with silence or "
                "at most a soft 'mmhmm'.  Do NOT say 'Of course!' / "
                "'Take your time!' — those are chatbot tells."
            )
        elif ack is not None:
            examples = _ACK_EXAMPLES.get(ack, ())
            if examples:
                joined = " / ".join(f"'{e}'" for e in examples)
                repeat_hint = ""
                if last_ack == ack:
                    repeat_hint = (
                        "  You used a similar opener last turn — vary the "
                        "wording so it doesn't parrot."
                    )
                parts.append(
                    f"  Ack: open with something like {joined} — pick "
                    f"one, don't recite them all.{repeat_hint}"
                )

        # Delivery intent.
        delivery_line = _DELIVERY_HINT.get(decision.delivery_intent, "")
        if delivery_line:
            parts.append(f"  Delivery:{delivery_line}")

        # Token budget hint (turn max_tokens into a word-count guideline
        # the LLM understands better).
        if decision.max_tokens:
            # Rough tokens→words: 1.3 tokens/word for English.
            words = max(4, int(decision.max_tokens / 1.3))
            parts.append(
                f"  Length: aim for under {words} words.  Cut everything "
                f"the caller doesn't need to hear."
            )

        # Slot / tool specificity.
        if decision.requested_slot:
            parts.append(
                f"  Ask for: {decision.requested_slot}.  Not other slots.  "
                f"Not confirmation of what they already told you."
            )
        if decision.tool:
            parts.append(
                f"  Tool preamble: reference {decision.tool} truthfully "
                f"('let me check that / pulling up your record')."
            )

        # Must-include facts (for LLM-path booking-confirm).
        if decision.must_include_facts:
            facts_str = "; ".join(decision.must_include_facts)
            parts.append(
                f"  Must include verbatim: {facts_str}.  Do NOT rephrase "
                f"or approximate these — the caller will notice drift."
            )

        # Final guardrail — always land the same message.
        parts.append(
            "  Do NOT narrate what you're doing.  Do NOT parrot back the "
            "caller's own words.  Follow the persona's length ladder."
        )
        return "\n".join(parts)
    except Exception:
        return None


__all__ = ["render_policy_directive"]
