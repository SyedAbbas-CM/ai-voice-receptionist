"""Verify sentiment field lands in ExtractedFields and defaults sanely
when the LLM returns garbage or omits the field."""
from __future__ import annotations

import json

import pytest

from app.providers.base import LLMProvider, LLMResponse
from packages.core_agent.extractor import _parse_sentiment, extract_fields
from packages.schemas import ExtractedFields, Sentiment


class ScriptedLLM(LLMProvider):
    name = "scripted"

    def __init__(self, response_text: str):
        self.response_text = response_text

    async def complete(self, messages, tools=None, temperature=0.3, max_tokens=1024):
        return LLMResponse(text=self.response_text)


def test_defaults_to_neutral():
    ef = ExtractedFields()
    assert ef.sentiment == Sentiment.NEUTRAL
    assert ef.is_frustrated() is False


def test_frustrated_is_flagged():
    ef = ExtractedFields(sentiment=Sentiment.FRUSTRATED)
    assert ef.is_frustrated() is True


def test_negative_is_flagged():
    ef = ExtractedFields(sentiment=Sentiment.NEGATIVE)
    assert ef.is_frustrated() is True


def test_positive_not_flagged():
    ef = ExtractedFields(sentiment=Sentiment.POSITIVE)
    assert ef.is_frustrated() is False


@pytest.mark.parametrize("raw,expected", [
    ("positive", Sentiment.POSITIVE),
    ("NEUTRAL", Sentiment.NEUTRAL),
    ("negative", Sentiment.NEGATIVE),
    ("frustrated", Sentiment.FRUSTRATED),
    ("Frustrated", Sentiment.FRUSTRATED),
    (None, Sentiment.NEUTRAL),
    ("", Sentiment.NEUTRAL),
    ("angry-and-mad", Sentiment.NEUTRAL),  # unknown → neutral fallback
    (123, Sentiment.NEUTRAL),
])
def test_parse_sentiment(raw, expected):
    assert _parse_sentiment(raw) == expected


@pytest.mark.asyncio
async def test_extractor_populates_sentiment():
    payload = {
        "caller_name": "John",
        "phone": "5551234567",
        "intent": "book_appointment",
        "service": "consult",
        "preferred_date": "2026-07-15",
        "preferred_time": "10:00",
        "urgency": "medium",
        "lead_score": 70,
        "summary": "Booking a consult",
        "sentiment": "frustrated",
    }
    llm = ScriptedLLM(json.dumps(payload))
    result = await extract_fields(llm, [
        {"role": "user", "content": "I've been on hold forever!"},
        {"role": "assistant", "content": "Sorry about that."},
    ])
    assert result.sentiment == Sentiment.FRUSTRATED
    assert result.is_frustrated() is True


@pytest.mark.asyncio
async def test_extractor_defaults_missing_sentiment_field():
    """Old prompts might not include a sentiment field. Default to NEUTRAL."""
    payload = {"caller_name": "Sam", "intent": "faq", "urgency": "low",
               "lead_score": 30, "summary": "Asked about hours"}
    llm = ScriptedLLM(json.dumps(payload))
    result = await extract_fields(llm, [
        {"role": "user", "content": "What time do you close?"},
    ])
    assert result.sentiment == Sentiment.NEUTRAL


@pytest.mark.asyncio
async def test_extractor_survives_invalid_sentiment_value():
    payload = {"caller_name": "Sam", "sentiment": "ecstatic-and-plotting"}
    llm = ScriptedLLM(json.dumps(payload))
    result = await extract_fields(llm, [{"role": "user", "content": "hi"}])
    assert result.sentiment == Sentiment.NEUTRAL
