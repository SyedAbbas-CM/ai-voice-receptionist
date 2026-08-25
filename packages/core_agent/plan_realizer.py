"""T-SP1 (2026-08-19): plan-then-realize glue.

Wires the pre-existing `packages/dialogue/plan.py` SemanticPlan schema
into the runtime.  The LLM emits a plan via the `emit_semantic_plan`
tool call; this module:

  1. Parses the tool call arguments into a `SemanticPlan`.
  2. Substitutes any critical PlannedFact values into the LLM's natural
     reply text if the LLM drifted (e.g. caller said "1:30", plan has
     PlannedFact(claim="1:30", critical=True), LLM said "2:30" in the
     reply — post-processor swaps 2:30 → 1:30).
  3. Surfaces `pending_tasks` into `state._reactive_notes` so the next
     turn's prompt sees them.

Does NOT change how the brain runs.  If the LLM doesn't emit
`emit_semantic_plan`, everything falls back to current behaviour.

The `emit_semantic_plan` tool is registered by
`_semantic_plan_tool_definition()` and consumed inside the brain's
tool loop.  It's a METADATA tool — no side effects, no downstream
action, just captures the LLM's structured intent.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from packages.dialogue.plan import (
    DeliveryIntent,
    PlanOperation,
    PlannedFact,
    PlannedQuestion,
    SemanticPlan,
)
from packages.schemas import ToolDefinition

log = logging.getLogger(__name__)

SEMANTIC_PLAN_TOOL_NAME = "emit_semantic_plan"


def semantic_plan_tool_definition() -> ToolDefinition:
    """Return the ToolDefinition the LLM sees.

    Keep the schema tight — every extra field is another way for the
    LLM to hallucinate.  Only the fields the realizer USES are exposed."""
    return ToolDefinition(
        name=SEMANTIC_PLAN_TOOL_NAME,
        description=(
            "Emit a structured plan for THIS turn BEFORE writing your "
            "natural-language reply.  Use this whenever the reply "
            "contains specific facts the caller must hear verbatim: "
            "chosen times, chosen dates, prices, phone numbers, "
            "appointment details, or any tool-returned data.  Also use "
            "when the caller mentioned secondary intents you're "
            "deferring (e.g. 'implants after the general appointment' "
            "— put 'implant_consult_follow_up' in pending_tasks).  If "
            "the turn is purely conversational (hello, yes, no, "
            "acknowledgment), you can skip this tool.  Emitting the "
            "plan is FREE — the realizer uses it to guarantee you "
            "don't accidentally substitute or drop facts."
        ),
        parameters={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [op.value for op in PlanOperation],
                    "description": (
                        "What kind of turn this is.  greet / ask_slot / "
                        "answer_faq / answer_from_profile / offer_slots / "
                        "propose_action / confirm_action / acknowledge / "
                        "apologize / escalate / handoff_info / "
                        "ask_correction / neutral."
                    ),
                },
                "facts": {
                    "type": "array",
                    "description": (
                        "Every specific fact the reply is allowed to "
                        "state.  Each fact needs a source label."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim": {
                                "type": "string",
                                "description": (
                                    "The literal wording the caller "
                                    "should hear — e.g. '1:30', "
                                    "'August 20th', '$185'."
                                ),
                            },
                            "source": {
                                "type": "string",
                                "description": (
                                    "Where this fact came from: "
                                    "'caller' | 'tool:<tool_name>' | "
                                    "'profile' | 'rag:<chunk_id>'."
                                ),
                            },
                            "critical": {
                                "type": "boolean",
                                "description": (
                                    "True if this fact MUST be spoken "
                                    "verbatim (times, prices, phone "
                                    "numbers, dates).  The realizer "
                                    "will substitute this exact value "
                                    "into your reply if you drift."
                                ),
                            },
                        },
                        "required": ["claim", "source"],
                    },
                },
                "question": {
                    "type": "object",
                    "description": (
                        "The single question this turn asks, if any."
                    ),
                    "properties": {
                        "purpose": {"type": "string"},
                        "text_goal": {"type": "string"},
                    },
                },
                "pending_tasks": {
                    "type": "array",
                    "description": (
                        "Secondary intents the caller mentioned that "
                        "you are NOT addressing this turn — e.g. they "
                        "want implants after the general appointment.  "
                        "One short label per intent."
                    ),
                    "items": {"type": "string"},
                },
                "delivery_intent": {
                    "type": "string",
                    "enum": [d.value for d in DeliveryIntent],
                    "description": (
                        "Desired tone: warm / reassuring / "
                        "professional / urgent / apologetic / neutral."
                    ),
                },
            },
            "required": ["operation"],
        },
    )


def parse_semantic_plan(args: dict[str, Any]) -> Optional[SemanticPlan]:
    """Turn tool-call arguments into a validated SemanticPlan, or None
    on any parse/validation failure.  Never raises — a broken plan
    just means we skip realizer post-processing this turn."""
    try:
        op = args.get("operation")
        if not op:
            return None
        try:
            operation = PlanOperation(op)
        except ValueError:
            log.info("plan_realizer: unknown operation=%r, defaulting NEUTRAL", op)
            operation = PlanOperation.NEUTRAL
        facts = []
        for f in args.get("facts") or []:
            if not isinstance(f, dict):
                continue
            claim = str(f.get("claim") or "").strip()
            source = str(f.get("source") or "").strip()
            if not claim or not source:
                continue
            facts.append(PlannedFact(
                claim=claim,
                source=source,
                critical=bool(f.get("critical", False)),
            ))
        question: Optional[PlannedQuestion] = None
        q = args.get("question")
        if isinstance(q, dict) and q.get("purpose") and q.get("text_goal"):
            question = PlannedQuestion(
                purpose=str(q["purpose"]).strip(),
                text_goal=str(q["text_goal"]).strip(),
            )
        pending = [
            str(t).strip() for t in (args.get("pending_tasks") or [])
            if str(t).strip()
        ]
        di = args.get("delivery_intent") or DeliveryIntent.NEUTRAL.value
        try:
            delivery = DeliveryIntent(di)
        except ValueError:
            delivery = DeliveryIntent.NEUTRAL
        # Some invariants in SemanticPlan raise on inconsistent operations
        # (e.g. ASK_SLOT requires question, ANSWER_FAQ requires facts).
        # LLMs may violate these — downgrade to NEUTRAL if the strict
        # form doesn't validate, rather than losing the whole plan.
        try:
            return SemanticPlan(
                operation=operation,
                facts=facts,
                question=question,
                pending_tasks=pending,
                delivery_intent=delivery,
            )
        except Exception as ve:
            log.info(
                "plan_realizer: strict validation failed (%s) — "
                "retrying as NEUTRAL",
                ve,
            )
            return SemanticPlan(
                operation=PlanOperation.NEUTRAL,
                facts=facts,
                pending_tasks=pending,
                delivery_intent=delivery,
            )
    except Exception as e:
        log.warning("plan_realizer: parse_semantic_plan failed: %s", e)
        return None


# ── critical-fact substitution ─────────────────────────────────────────

# Common ways a time can be spoken.  If the plan says "1:30" the LLM
# might have written "2:30", "two thirty", "2:30 PM", "14:30" — we
# aim to detect any of those and swap the plan value in.
_TIME_LIKE = re.compile(
    r"\b(?:\d{1,2}:\d{2}(?:\s*(?:am|pm|a\.m\.|p\.m\.))?"
    r"|\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.))"
    r"\b",
    re.IGNORECASE,
)

# Number-word replacements ("two thirty" → digit form for matching).
# Non-exhaustive on purpose; the substitution only fires if the plan
# fact appears not-quite-verbatim in the reply.
_NUMBER_WORDS = {
    "twelve thirty": "12:30", "twelve fifteen": "12:15",
    "one thirty": "1:30",     "one fifteen": "1:15",
    "two thirty": "2:30",     "two fifteen": "2:15",
    "three thirty": "3:30",   "three fifteen": "3:15",
    "four thirty": "4:30",    "five thirty": "5:30",
    "six thirty": "6:30",     "seven thirty": "7:30",
    "eight thirty": "8:30",   "nine thirty": "9:30",
    "ten thirty": "10:30",    "eleven thirty": "11:30",
}


def _looks_like_time(claim: str) -> bool:
    return bool(_TIME_LIKE.search(claim))


def substitute_critical_facts(reply: str, plan: SemanticPlan) -> tuple[str, list[str]]:
    """Post-process the LLM's reply text so critical PlannedFact values
    appear verbatim.  Returns (revised_reply, substitution_notes).

    Currently handles time-shaped facts (the most-observed drift).
    Other fact types (prices, names, phone numbers) fall through
    without change — they either already appear correctly or the
    realizer's guarantee is left to the prompt.  Future: extend to
    price/phone/date shapes as we see them drift on real calls."""
    if not reply or not plan:
        return reply, []
    subs: list[str] = []
    critical = plan.critical_facts()
    if not critical:
        return reply, []
    revised = reply
    for fact in critical:
        want = fact.claim.strip()
        if not want:
            continue
        # If the exact string is already present, nothing to do.
        if want in revised:
            continue
        # Special-case time drift: if the plan wants a time value,
        # find whatever time-shape thing is in the reply and swap it.
        if _looks_like_time(want):
            # First try replacing spelled-out number-words.
            low = revised.lower()
            for word, digit in _NUMBER_WORDS.items():
                if word in low and digit != want:
                    pattern = re.compile(re.escape(word), re.IGNORECASE)
                    new = pattern.sub(want, revised, count=1)
                    if new != revised:
                        subs.append(f"time '{word}' → {want!r}")
                        revised = new
                        break
            # Then try replacing any digit time that's not what we want.
            def _sub_one(match: re.Match) -> str:
                found = match.group(0)
                # Don't replace correct occurrences.
                if want.lower() in found.lower():
                    return found
                subs.append(f"time {found!r} → {want!r}")
                return want
            new = _TIME_LIKE.sub(_sub_one, revised, count=1)
            revised = new
    return revised, subs
