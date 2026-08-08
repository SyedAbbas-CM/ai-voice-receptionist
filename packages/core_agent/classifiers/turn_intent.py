"""K3 + K4: fast regex-based turn-intent classifier.

Runs BEFORE the brain fires.  Labels the incoming caller turn so the
brain can branch its persona (correction → apologize + revise,
clarification → hold + probe, chitchat → keep-it-short, etc).

Regex-only, ~5 microseconds per call.  When we ship K4 proper we'll
add an 8B LLM tier for ambiguous cases; the regex handles the
high-confidence 80% cheaply.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class TurnIntent(str, Enum):
    CORRECTION = "correction"        # "no wait", "actually", "that's not"
    CLARIFICATION_REQ = "clarification_req"  # caller ASKING for clarification
    COMMITMENT = "commitment"        # "yes book it", "sounds good", "let's do it"
    REJECTION = "rejection"          # "no thanks", "not interested", "cancel"
    QUESTION = "question"            # ends with ?, or starts with what/how/when/etc
    ANSWER = "answer"                # short factual reply
    CHITCHAT = "chitchat"            # "how are you", "the weather"
    OFF_TOPIC = "off_topic"
    UNKNOWN = "unknown"


@dataclass
class IntentResult:
    intent: TurnIntent
    confidence: float
    matched: str  # snippet that triggered the classification
    system_note: str  # sentence to inject into brain prompt for this turn


# ── correction patterns ─────────────────────────────────────────────
# These are the highest-leverage ones — misclassifying a correction as
# an answer makes the agent feel deaf.  Ordered by specificity.
_CORRECTION_PATTERNS = [
    re.compile(r"^\s*no\s*[,.]?\s*wait\b", re.I),
    re.compile(r"^\s*wait\s*[,.]?\s*no\b", re.I),
    # "no + I/that/it + ..." only when NOT a rejection ("no thanks/thank you").
    re.compile(r"^\s*(?:no|nope|nah)\s*[,.]?\s+(?:i|that|it)\b(?!\s+(?:thank|thanks))", re.I),
    re.compile(r"^\s*actually\s*[,.]?", re.I),
    re.compile(r"^\s*that'?s\s+not\s+(?:what|right|correct)\b", re.I),
    re.compile(r"^\s*i\s+meant\b", re.I),
    re.compile(r"^\s*(?:scratch|forget)\s+that\b", re.I),
    re.compile(r"^\s*correction\b", re.I),
    re.compile(r"^\s*let\s+me\s+(?:correct|change|update|clarify)\b", re.I),
    re.compile(r"\bi\s+(?:said|meant|wanted)\b.*\b(?:not|instead)\b", re.I),
]

# ── commitment patterns ─────────────────────────────────────────────
_COMMITMENT_PATTERNS = [
    re.compile(r"^\s*(?:yes|yep|yeah|yup)[,.!]?\s+(?:book|lock|confirm|go|do|schedule)\b", re.I),
    re.compile(r"\b(?:sounds good|looks good|works for me|let'?s do it|book it|lock it in|confirmed?)\b", re.I),
    re.compile(r"^\s*(?:ok|okay|alright)[,.!]?\s+(?:book|do|go|schedule)\b", re.I),
    re.compile(r"^\s*go\s+ahead\b", re.I),
]

# ── rejection patterns ──────────────────────────────────────────────
_REJECTION_PATTERNS = [
    re.compile(r"^\s*(?:no|nope|nah)\s+(?:thank|thanks)\b", re.I),
    re.compile(r"^\s*(?:not|no)\s+(?:right now|today|interested)\b", re.I),
    re.compile(r"^\s*(?:cancel|nevermind|never mind|forget it)\b", re.I),
    re.compile(r"^\s*i'?ll\s+(?:call back|think about it)\b", re.I),
]

# ── clarification request ───────────────────────────────────────────
_CLARIFICATION_PATTERNS = [
    re.compile(r"\b(?:what do you mean|what does that mean|come again|say that again|repeat that|can you (?:repeat|clarify|explain))\b", re.I),
    re.compile(r"^\s*(?:sorry|pardon)\s*[,.?]?\s*(?:what|come again)\b", re.I),
    re.compile(r"^\s*(?:huh|what)\??\s*$", re.I),
]

# ── chitchat ────────────────────────────────────────────────────────
_CHITCHAT_PATTERNS = [
    re.compile(r"\b(?:how are you|how'?s your day|how'?s it going|nice to (?:meet|hear))\b", re.I),
    re.compile(r"\b(?:the weather|good morning|good afternoon|good evening)\b", re.I),
]

# Question words that suggest a real question when they lead the turn.
_QUESTION_LEADS = frozenset({
    "what", "how", "when", "where", "why", "who", "which",
    "can", "could", "would", "will", "do", "does", "did", "is", "are",
})


def classify_turn_intent(text: str) -> IntentResult:
    """Classify caller turn intent from text alone (regex tier).

    Priority order matters:
      correction > commitment > rejection > clarification > question > chitchat > answer

    Correction wins over commitment because "no, book it for 4pm" is
    a correction of a prior 3pm proposal, not a commit.
    """
    stripped = (text or "").strip()
    if not stripped:
        return IntentResult(TurnIntent.UNKNOWN, 0.0, "", "")

    # Correction — highest priority.
    for pat in _CORRECTION_PATTERNS:
        m = pat.search(stripped)
        if m:
            return IntentResult(
                intent=TurnIntent.CORRECTION,
                confidence=0.9,
                matched=m.group(0),
                system_note=(
                    "TURN INTENT: The caller is CORRECTING your previous "
                    "reply.  Briefly acknowledge the correction, revise, "
                    "and confirm you have it right now.  Do not repeat "
                    "everything from the previous reply — only address "
                    "what changed."
                ),
            )

    # Commitment — before rejection, because "yes book it" wins over "no thanks".
    for pat in _COMMITMENT_PATTERNS:
        m = pat.search(stripped)
        if m:
            return IntentResult(
                intent=TurnIntent.COMMITMENT,
                confidence=0.85,
                matched=m.group(0),
                system_note=(
                    "TURN INTENT: The caller is COMMITTING to the proposal.  "
                    "Confirm the action is being taken, state the key details "
                    "(time, service, provider), and ask for any final missing "
                    "slot (usually caller name + phone if not yet captured)."
                ),
            )

    for pat in _REJECTION_PATTERNS:
        m = pat.search(stripped)
        if m:
            return IntentResult(
                intent=TurnIntent.REJECTION,
                confidence=0.85,
                matched=m.group(0),
                system_note=(
                    "TURN INTENT: The caller is DECLINING the proposal.  "
                    "Do not push.  Offer one alternative, or gracefully "
                    "wrap up if they seem done."
                ),
            )

    for pat in _CLARIFICATION_PATTERNS:
        m = pat.search(stripped)
        if m:
            return IntentResult(
                intent=TurnIntent.CLARIFICATION_REQ,
                confidence=0.85,
                matched=m.group(0),
                system_note=(
                    "TURN INTENT: The caller is asking for CLARIFICATION.  "
                    "Rephrase your last point more simply, using different "
                    "words.  Do not repeat verbatim."
                ),
            )

    for pat in _CHITCHAT_PATTERNS:
        m = pat.search(stripped)
        if m:
            return IntentResult(
                intent=TurnIntent.CHITCHAT,
                confidence=0.7,
                matched=m.group(0),
                system_note=(
                    "TURN INTENT: CHITCHAT.  Respond briefly and warmly (one "
                    "sentence), then gently pivot back to how you can help."
                ),
            )

    # Question detection — ends with ? or starts with a question lead.
    lower_first = stripped.split()[0].lower().rstrip(",.!?") if stripped.split() else ""
    if stripped.endswith("?") or lower_first in _QUESTION_LEADS:
        return IntentResult(
            intent=TurnIntent.QUESTION,
            confidence=0.65,
            matched=lower_first or "?",
            system_note="",  # no special guidance needed
        )

    # Default — treat as answer / neutral content.
    return IntentResult(
        intent=TurnIntent.ANSWER,
        confidence=0.5,
        matched="",
        system_note="",
    )


def detect_correction_target(text: str) -> Optional[str]:
    """K3 helper: return the corrected value if the turn contains an
    'X not Y' or 'not Y, X' pattern.  Used to retract slots.

    Examples:
        'no I said 3pm not 4pm'  -> '3pm'
        'implants not implants ' -> None (same value)
        'it was Tuesday not Thursday' -> 'Tuesday'
    """
    # Pattern 1: "X not Y"
    m = re.search(r"\b(\w+(?:\s+\w+){0,3})\s+not\s+\w+", text, re.I)
    if m:
        return m.group(1).strip()
    # Pattern 2: "not Y, X" or "not Y — X"
    m = re.search(r"\bnot\s+\w+(?:\s+\w+){0,3}[,.\-—]\s+(\w+(?:\s+\w+){0,3})", text, re.I)
    if m:
        return m.group(1).strip()
    return None
