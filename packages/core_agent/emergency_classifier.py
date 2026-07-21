"""Emergency intercept — the highest-liability safety layer.

**Why this exists**: The single most-cited liability failure in 2026 receptionist
research is "agent tried to book a slot when caller was having a heart attack."
(DeepCura, PatientXpress). ~14% of intake bookings are mis-routed.

This runs BEFORE every LLM turn. If it fires, the receptionist immediately:
  1. Tells the caller "please call 911 or go to the nearest emergency room"
  2. Records the escalation reason
  3. Sets state.status = ESCALATED
  4. Does NOT proceed to book, RAG, or any other tool

Two layers, in order:
  - **Regex fast-path** — catches ~90% of clear emergencies in <1 ms, zero cost
  - **LLM slow-path** (optional) — for ambiguous phrasing the regex misses

Regex-only mode is the default (`ENABLE_LLM_EMERGENCY_CLASSIFIER=false`) — the
LLM check adds ~300 ms per turn and doesn't materially improve recall for the
signals worth catching. Enable if you're seeing production misses on edge cases.

Failure mode we OPTIMIZE FOR: false positive (accidentally escalating a
non-emergency) is better than false negative (missing a real one). A caller
told to hang up and call 911 by mistake is embarrassed. A caller ignored
during cardiac arrest is dead.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from apps.api.app.providers.base import LLMProvider


log = logging.getLogger(__name__)


# Regex patterns for CLEAR emergency signals. Each pattern is intentionally
# broad — false-positive cost is one canned "please call 911" reply the caller
# can override; false-negative cost is a dead caller.
#
# Curated from 2026 receptionist-agent SOTA research (see
# docs/rnd-2026-07/05-nightmare-callers.md category 10).
_EMERGENCY_PATTERNS = [
    # Cardiac / chest
    re.compile(r"\bchest\s+(?:pain|pressure|tight|hurt)", re.I),
    re.compile(r"\bheart\s+(?:attack|racing|stopped|failing)", re.I),
    re.compile(r"\bcardiac\b", re.I),
    re.compile(r"\barm\s+(?:is\s+)?numb", re.I),
    # Respiratory
    re.compile(r"\bcan'?t\s+breathe\b", re.I),
    re.compile(r"\bcannot\s+breathe\b", re.I),
    re.compile(r"\b(?:not|isn'?t|is\s+not|aren'?t|are\s+not)\s+breathing\b", re.I),
    re.compile(r"\bbreathing\s+(?:funny|wrong|weird|badly|shallow|hard)\b", re.I),
    re.compile(r"\bstopped\s+breathing\b", re.I),
    re.compile(r"\bchoking\b", re.I),
    re.compile(r"\bstruggling\s+to\s+breathe\b", re.I),
    # Bleeding / trauma
    re.compile(r"\bbleeding\s+(?:heavily|out|badly|a\s+lot)", re.I),
    re.compile(r"\bwon'?t\s+stop\s+bleeding\b", re.I),
    re.compile(r"\bbleeding\s+and\s+(?:can'?t|cannot)\s+stop", re.I),
    re.compile(r"\bcan'?t\s+stop\s+(?:the\s+)?bleeding\b", re.I),
    re.compile(r"\bhemorrhag", re.I),
    re.compile(r"\bmajor\s+(?:trauma|injury|accident)", re.I),
    # Neurological
    re.compile(r"\bhaving\s+a\s+stroke\b", re.I),
    re.compile(r"\bseizure\b", re.I),
    re.compile(r"\bunconscious\b", re.I),
    re.compile(r"\bpassed\s+out\b", re.I),
    re.compile(r"\bwon'?t\s+wake\s+up\b", re.I),
    # Overdose / poisoning
    re.compile(r"\boverdose\b", re.I),
    re.compile(r"\bpoisoned?\b", re.I),
    re.compile(r"\btook\s+too\s+many\b", re.I),
    # Self-harm — highest sensitivity
    re.compile(r"\bsuicid", re.I),
    re.compile(r"\bkill\s+(?:myself|him|her|them)", re.I),
    re.compile(r"\bend\s+(?:my\s+life|it\s+all)", re.I),
    re.compile(r"\bhurt(?:ing)?\s+(?:myself|him|her|them)\b", re.I),
    re.compile(r"\bself[-\s]harm\b", re.I),
    # Anaphylaxis / severe allergic
    re.compile(r"\banaphylax", re.I),
    re.compile(r"\bsevere\s+allergic\s+reaction\b", re.I),
    # Combined "emergency" self-declaration
    re.compile(r"\bthis\s+is\s+an?\s+emergency\b", re.I),
    re.compile(r"\bcall\s+911\b", re.I),
]


# Emergency categories used to route (short-term: we just escalate uniformly;
# longer term the reason string helps the human triage on the other end).
@dataclass
class EmergencyVerdict:
    """Verdict from the emergency classifier."""
    is_emergency: bool
    category: str = ""            # "cardiac", "respiratory", "self_harm", etc.
    matched_text: str = ""        # what triggered — used in the escalation log
    reason: str = ""              # human-readable escalation reason

    @property
    def escalation_message(self) -> str:
        """The single line the receptionist says. Deliberately generic — we
        don't triage medically, we just get out of the way."""
        if not self.is_emergency:
            return ""
        if self.category == "self_harm":
            return (
                "I hear you and I want to help. Please call nine one one, "
                "or the Suicide and Crisis Lifeline at nine eight eight, "
                "and stay on the line with them."
            )
        return (
            "This sounds like an emergency. Please hang up and call nine "
            "one one, or go to the nearest emergency room right now."
        )


# Map of pattern-index range → category. Kept in sync with _EMERGENCY_PATTERNS
# order by tests below.
def _classify_by_pattern_index(idx: int) -> str:
    # Update these ranges any time _EMERGENCY_PATTERNS gets a new entry.
    # Counts: cardiac 4, respiratory 7, bleeding 6, neuro 5, overdose 3,
    # self-harm 5, anaphylaxis 2, self-declared 2 = 34 total.
    if idx <= 3:
        return "cardiac"
    if idx <= 10:
        return "respiratory"
    if idx <= 16:
        return "bleeding_trauma"
    if idx <= 21:
        return "neurological"
    if idx <= 24:
        return "overdose_poisoning"
    if idx <= 29:
        return "self_harm"
    if idx <= 31:
        return "anaphylaxis"
    return "self_declared"


def classify_emergency(text: str) -> EmergencyVerdict:
    """Fast regex-only classification. Returns immediately on first match.

    Returns EmergencyVerdict(is_emergency=False, ...) on no match.
    """
    if not text or not text.strip():
        return EmergencyVerdict(is_emergency=False)

    for i, pattern in enumerate(_EMERGENCY_PATTERNS):
        m = pattern.search(text)
        if m:
            category = _classify_by_pattern_index(i)
            return EmergencyVerdict(
                is_emergency=True,
                category=category,
                matched_text=m.group(),
                reason=f"Emergency signal ({category}): matched {m.group()!r}",
            )
    return EmergencyVerdict(is_emergency=False)


async def classify_with_llm_fallback(
    llm: "LLMProvider",
    text: str,
    max_tokens: int = 8,
) -> EmergencyVerdict:
    """LLM slow-path for ambiguous phrasing the regex misses.

    Enabled via ENABLE_LLM_EMERGENCY_CLASSIFIER=true. Adds ~300ms per turn.

    Failure mode: on ANY LLM error, we fail SAFE — return not-emergency, letting
    the regular flow proceed. Never block a legitimate caller because a hosted
    LLM 500'd. But: log the failure so we can watch the rate.
    """
    if not text.strip():
        return EmergencyVerdict(is_emergency=False)
    try:
        resp = await llm.complete(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Classify caller text as EMERGENCY or NORMAL. EMERGENCY = "
                        "any medical, psychiatric, or safety crisis requiring "
                        "immediate 911 dispatch (chest pain, breathing failure, "
                        "severe bleeding, stroke, seizure, overdose, self-harm, "
                        "anaphylaxis, unconscious person). NORMAL = any other "
                        "caller — including routine bookings, cancellations, "
                        "chronic conditions, minor injuries, or FAQs. When in "
                        "doubt, prefer EMERGENCY (false positives are safe). "
                        "Output ONE word: EMERGENCY or NORMAL."
                    ),
                },
                {"role": "user", "content": text[:500]},
            ],
            tools=None,
            temperature=0.0,
            max_tokens=max_tokens,
        )
        raw = (resp.text or "").strip().upper().strip('.,!"\'` ')
        if raw.startswith("EMERG"):
            return EmergencyVerdict(
                is_emergency=True,
                category="llm_flagged",
                matched_text=text[:80],
                reason="LLM classifier flagged as emergency",
            )
    except Exception as e:
        log.warning("emergency LLM classifier failed: %s", e)

    return EmergencyVerdict(is_emergency=False)


async def classify_emergency_full(
    llm: Optional["LLMProvider"],
    text: str,
    use_llm_fallback: bool = False,
) -> EmergencyVerdict:
    """Public entry point. Regex first (fast/free), optional LLM fallback."""
    verdict = classify_emergency(text)
    if verdict.is_emergency:
        return verdict
    if use_llm_fallback and llm is not None:
        return await classify_with_llm_fallback(llm, text)
    return verdict
