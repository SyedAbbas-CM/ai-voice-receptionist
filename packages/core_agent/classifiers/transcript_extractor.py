"""Transcript signal extractor — ports SubtoDealz's "Real Estate Call
Transcript Analyzer" prompt.

Original: `OpenAI1` node in `SubtoDealz - Official`. It ran BEFORE the
strict single-word classifier and returned a JSON blob with:
    - lead_status (untrusted — the classifier is the source of truth)
    - rent_updated (bool)
    - new_rent_amount (int or None)
    - variables { update_needed, update_reason, sheet_action, rent_difference }

We keep the rent extraction (it's the useful part) and drop the redundant
lead_status field (the classifier owns that). Output is a Pydantic model
so callers get IDE autocomplete and validation for free.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from apps.api.app.providers.base import LLMProvider


log = logging.getLogger(__name__)


class TranscriptExtraction(BaseModel):
    """Structured signals extracted from a call transcript."""

    rent_updated: bool = False
    new_rent_amount: Optional[int] = None  # whole dollars, no symbols
    rent_difference: Optional[int] = None  # signed; positive = rent went up
    summary_note: str = ""                 # 1-sentence human note for the sheet
    property_confirmed_available: Optional[bool] = None
    callback_requested_time: Optional[str] = Field(
        default=None,
        description="Natural-language time the caller asked to be reached back, or None"
    )


SYSTEM_PROMPT = """You are a post-call analyzer for real estate voice-agent calls to rental property owners. Read the transcript and extract structured signals.

Extract ONLY these fields — do not guess or hallucinate:

1. rent_updated: true ONLY if the owner explicitly stated a rent amount different from the one the AI mentioned. Silence or non-answer = false.
2. new_rent_amount: integer whole dollars (e.g. 1500). Null if not stated.
3. rent_difference: new_rent_amount minus old rent (from the AI's mention). Signed integer or null.
4. summary_note: one short sentence for the operator's spreadsheet ("Owner said rent is now $1500", "Property already rented", "Asked for callback next week", etc). Max 100 chars.
5. property_confirmed_available: true if owner confirmed still available, false if rented/sold/off-market, null if unclear.
6. callback_requested_time: quote the caller's phrase ("next Tuesday morning", "after 5pm") or null.

Do NOT classify the lead (that's a separate step).
Do NOT invent numbers.
If unsure about any field, use null / false / empty string.

Output ONLY a valid JSON object with exactly these keys. No prose, no markdown fence."""


async def extract_transcript_signals(
    llm: "LLMProvider",
    transcript: str,
    old_rent_amount: Optional[int] = None,
) -> TranscriptExtraction:
    """Run the extractor. Returns a validated Pydantic model.

    If the transcript is empty or the LLM returns unparseable output, returns
    a default TranscriptExtraction (all None/False) — never raises."""
    if not (transcript or "").strip():
        return TranscriptExtraction(summary_note="No transcript captured")

    context_line = ""
    if old_rent_amount is not None:
        context_line = f"\nOld rent (before the call): ${old_rent_amount}\n"

    user_msg = f"{context_line}Transcript:\n{transcript.strip()}\n\nReturn the JSON now."

    resp = await llm.complete(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        tools=None,
        temperature=0.0,
        max_tokens=512,
    )

    text = (resp.text or "").strip()
    # Strip common markdown fence patterns
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("transcript_extractor got unparseable JSON: %r", text[:200])
        return TranscriptExtraction(summary_note="Extraction failed — see logs")

    try:
        return TranscriptExtraction(**data)
    except Exception as e:  # ValidationError etc.
        log.warning("transcript_extractor validation failed: %s (data=%r)", e, data)
        # Best-effort partial extraction
        return TranscriptExtraction(
            summary_note=str(data.get("summary_note") or "")[:100],
        )
