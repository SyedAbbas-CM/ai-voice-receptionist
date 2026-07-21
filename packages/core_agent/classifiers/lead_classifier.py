"""Single-word lead classifier — ports the SubtoDealz strict decision tree.

Original: `OpenAI` node in `SubtoDealz - Official`, prompt titled
"LEAD CLASSIFICATION SYSTEM PROMPT (UPDATED & FIXED)".

Design notes:
- The model outputs ONE word. We validate it's a valid enum and fall back
  to NO_ANSWER on malformed output rather than silently misclassifying.
- The decision tree explicitly favors NO_ANSWER on ambiguous silence
  (protecting against false HOT_LEAD → wasted follow-up call).
- Empty / trivially-short transcripts short-circuit without calling the LLM
  (the original graph called GPT-4.1 just to return the literal string
  "NO_ANSWER" — pure token waste).
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from apps.api.app.providers.base import LLMProvider


log = logging.getLogger(__name__)


class LeadStatus(str, Enum):
    HOT_LEAD = "HOT_LEAD"
    COLD_LEAD = "COLD_LEAD"
    PROPERTY_UNAVAILABLE = "PROPERTY_UNAVAILABLE"
    NO_ANSWER = "NO_ANSWER"
    CALLBACK_REQUESTED = "CALLBACK_REQUESTED"


_VALID = {s.value for s in LeadStatus}


SYSTEM_PROMPT = """You are a Lead Classification Specialist for a real estate wholesaler that pitches seller financing to rental property owners.

Your task: analyze a call transcript and output ONLY ONE WORD from this list:
- HOT_LEAD
- COLD_LEAD
- PROPERTY_UNAVAILABLE
- NO_ANSWER
- CALLBACK_REQUESTED

## DECISION TREE (evaluate in order — first match wins)

### STEP 1 — Was there a real conversation?
Output NO_ANSWER if ANY of these are true:
- Transcript is empty or contains no user turns
- Only the AI spoke; user never responded
- Call was under 15 seconds
- endedReason is "no-answer" or "voicemail"
- User said only a filler like "hello?" and hung up

### STEP 2 — Did the caller say the property is off-market?
Output PROPERTY_UNAVAILABLE if the user said any of:
- "It's rented"
- "Already sold"
- "Off the market"
- "It's taken"
- "We rented it out"
Even if they were friendly, if the property is unavailable this is the answer.

### STEP 3 — Did the caller ask for a callback?
Output CALLBACK_REQUESTED if the user said any of:
- "Call me back"
- "Send me an email / text me"
- "Try me tomorrow / next week"
- "Not a good time, reach out later"
Callback requests take precedence over unclear seller-financing responses.

### STEP 4 — Seller Financing Response (CRITICAL)
Find where the AI mentions "seller financing", "act as the lender/bank", or "carry the financing".
Look at the VERY NEXT user response.

A. Silence, "uh huh", or no meaningful engagement → ALWAYS NO_ANSWER
   (Do NOT infer interest from silence. Silence after the pitch is not interest.)
B. User asks a clarifying question, asks for details, or expresses genuine interest → HOT_LEAD
C. User says "not interested", "no thanks", "I want to sell traditionally", or explicitly rejects → COLD_LEAD

### FALLBACK
If none of the above apply, output NO_ANSWER.

## OUTPUT FORMAT
Output ONLY the single word. No punctuation, no explanation, no quotes.
"""


def _short_circuit(transcript: str) -> Optional[LeadStatus]:
    """Skip the LLM when the answer is trivially NO_ANSWER."""
    text = (transcript or "").strip()
    if not text or len(text) < 40:
        return LeadStatus.NO_ANSWER
    return None


async def classify_lead(
    llm: "LLMProvider",
    transcript: str,
    ended_reason: Optional[str] = None,
) -> LeadStatus:
    """Classify a call into a single LeadStatus.

    - transcript: the concatenated call transcript (Vapi returns this as a string)
    - ended_reason: optional Vapi endedReason ("no-answer", "voicemail", etc)
    """
    short = _short_circuit(transcript)
    if short is not None:
        log.info("lead_classifier short-circuit -> %s", short.value)
        return short

    if ended_reason in {"no-answer", "voicemail", "customer-did-not-answer"}:
        return LeadStatus.NO_ANSWER

    user_msg = (
        f"endedReason: {ended_reason or 'n/a'}\n"
        f"transcript:\n{transcript.strip()}\n\n"
        "Output the single classification word now."
    )

    resp = await llm.complete(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        tools=None,
        temperature=0.0,
        max_tokens=16,
    )

    raw = (resp.text or "").strip().upper()
    # Strip common wrappers: quotes, markdown ticks, trailing punctuation
    raw = raw.strip('"\'` .,;:*_-\n\r\t')
    # If the model added prose, take just the first token
    raw = raw.split()[0] if raw else ""

    if raw in _VALID:
        return LeadStatus(raw)

    log.warning("lead_classifier got invalid output %r, falling back to NO_ANSWER", resp.text)
    return LeadStatus.NO_ANSWER
