from __future__ import annotations

import json
from typing import TYPE_CHECKING

from packages.schemas import ExtractedFields, Intent, Sentiment, Urgency

if TYPE_CHECKING:
    from apps.api.app.providers.base import LLMProvider


EXTRACTION_PROMPT = """You read a receptionist transcript and return a single JSON object summarizing
what the caller wants. Return ONLY JSON, no prose.

Schema:
{
  "caller_name": string|null,
  "phone": string|null,
  "intent": "book_appointment"|"reschedule"|"cancel"|"faq"|"emergency"|"handoff"|"other",
  "service": string|null,
  "preferred_date": string|null,   // YYYY-MM-DD or "tomorrow"/"next monday" if unresolved
  "preferred_time": string|null,   // HH:MM 24h, or natural like "3pm"
  "urgency": "low"|"medium"|"high",
  "lead_score": integer 0-100,     // higher = more likely to convert
  "summary": string,               // one sentence
  "sentiment": "positive"|"neutral"|"negative"|"frustrated"  // caller's mood RIGHT NOW
}

sentiment rules:
- "positive" — polite, engaged, cooperative
- "neutral" — matter-of-fact, no strong emotion (default)
- "negative" — annoyed, curt, disagreeing but still communicating
- "frustrated" — repeating themselves, raised complaints, cursing, threatening
  to leave/go elsewhere, or explicitly asking for a human

If the field is genuinely unknown, return null (or "neutral" for sentiment).
Do not invent values.
"""


async def extract_fields(llm: "LLMProvider", transcript: list[dict]) -> ExtractedFields:
    """Run a small JSON-only call to summarize the conversation so far."""
    convo = "\n".join(
        f"{t['role'].upper()}: {t['content']}"
        for t in transcript
        if t["role"] in ("user", "assistant") and t.get("content")
    )
    messages = [
        {"role": "system", "content": EXTRACTION_PROMPT},
        {"role": "user", "content": f"Transcript:\n{convo}\n\nReturn the JSON now."},
    ]
    resp = await llm.complete(
        messages, tools=None, temperature=0.0, max_tokens=512,
        site="extractor",
    )
    text = resp.text.strip()

    if text.startswith("```"):
        text = text.strip("`").lstrip("json").strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ExtractedFields(summary="(extraction failed)")

    return ExtractedFields(
        caller_name=data.get("caller_name"),
        phone=data.get("phone"),
        intent=_parse_intent(data.get("intent")),
        service=data.get("service"),
        preferred_date=data.get("preferred_date"),
        preferred_time=data.get("preferred_time"),
        urgency=_parse_urgency(data.get("urgency")),
        lead_score=int(data.get("lead_score") or 0),
        summary=data.get("summary") or "",
        sentiment=_parse_sentiment(data.get("sentiment")),
    )


def _parse_intent(value) -> Intent:
    try:
        return Intent(value)
    except (ValueError, TypeError):
        return Intent.OTHER


def _parse_urgency(value) -> Urgency:
    try:
        return Urgency(value)
    except (ValueError, TypeError):
        return Urgency.LOW


def _parse_sentiment(value) -> Sentiment:
    try:
        return Sentiment((value or "neutral").lower())
    except (ValueError, TypeError, AttributeError):
        return Sentiment.NEUTRAL
