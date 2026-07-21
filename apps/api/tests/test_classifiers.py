from __future__ import annotations

import json
from typing import Optional

import pytest

from app.providers.base import LLMProvider, LLMResponse
from packages.core_agent.classifiers import (
    LeadStatus,
    TranscriptExtraction,
    classify_lead,
    extract_transcript_signals,
)


class ScriptedLLM(LLMProvider):
    """Returns queued LLMResponses. Records prompts for assertions."""
    name = "scripted"

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    async def complete(self, messages, tools=None, temperature=0.3, max_tokens=1024):
        self.calls.append(messages)
        if not self.responses:
            return LLMResponse(text="")
        return LLMResponse(text=self.responses.pop(0))


# ---------------- lead_classifier ----------------

@pytest.mark.asyncio
async def test_lead_classifier_short_circuits_on_empty_transcript():
    llm = ScriptedLLM([])  # LLM should NOT be called
    result = await classify_lead(llm, transcript="")
    assert result == LeadStatus.NO_ANSWER
    assert llm.calls == []  # zero token spend


@pytest.mark.asyncio
async def test_lead_classifier_short_circuits_on_trivial_transcript():
    llm = ScriptedLLM([])
    result = await classify_lead(llm, transcript="hello?")
    assert result == LeadStatus.NO_ANSWER
    assert llm.calls == []


@pytest.mark.asyncio
async def test_lead_classifier_respects_no_answer_ended_reason():
    llm = ScriptedLLM([])
    result = await classify_lead(
        llm,
        transcript="a" * 200,  # long enough to pass short-circuit
        ended_reason="no-answer",
    )
    assert result == LeadStatus.NO_ANSWER
    assert llm.calls == []


@pytest.mark.asyncio
async def test_lead_classifier_returns_hot_lead_on_valid_output():
    llm = ScriptedLLM(["HOT_LEAD"])
    result = await classify_lead(
        llm,
        transcript="AI: We could act as the lender for you.\nUser: Really? How would that work?"
    )
    assert result == LeadStatus.HOT_LEAD


@pytest.mark.asyncio
async def test_lead_classifier_strips_wrappers():
    llm = ScriptedLLM(["`CALLBACK_REQUESTED`."])
    result = await classify_lead(
        llm,
        transcript="A" * 200,
    )
    assert result == LeadStatus.CALLBACK_REQUESTED


@pytest.mark.asyncio
async def test_lead_classifier_falls_back_on_invalid_output():
    llm = ScriptedLLM(["I think this is a hot one"])  # not a valid enum
    result = await classify_lead(llm, transcript="A" * 200)
    assert result == LeadStatus.NO_ANSWER  # safe fallback


@pytest.mark.asyncio
async def test_lead_classifier_takes_first_token_on_verbose_output():
    llm = ScriptedLLM(["COLD_LEAD - the user rejected the pitch"])
    result = await classify_lead(llm, transcript="A" * 200)
    assert result == LeadStatus.COLD_LEAD


# ---------------- transcript_extractor ----------------

@pytest.mark.asyncio
async def test_extractor_returns_default_on_empty_transcript():
    llm = ScriptedLLM([])
    result = await extract_transcript_signals(llm, transcript="")
    assert isinstance(result, TranscriptExtraction)
    assert result.rent_updated is False
    assert result.new_rent_amount is None
    assert "No transcript" in result.summary_note
    assert llm.calls == []


@pytest.mark.asyncio
async def test_extractor_parses_valid_json():
    llm = ScriptedLLM([json.dumps({
        "rent_updated": True,
        "new_rent_amount": 1800,
        "rent_difference": 200,
        "summary_note": "Owner said rent is now $1800",
        "property_confirmed_available": True,
        "callback_requested_time": None,
    })])
    result = await extract_transcript_signals(
        llm,
        transcript="AI: Is $1600 still right? User: Actually it's $1800 now.",
        old_rent_amount=1600,
    )
    assert result.rent_updated is True
    assert result.new_rent_amount == 1800
    assert result.rent_difference == 200
    assert "1800" in result.summary_note


@pytest.mark.asyncio
async def test_extractor_strips_markdown_fence():
    llm = ScriptedLLM(["```json\n" + json.dumps({
        "rent_updated": False,
        "new_rent_amount": None,
        "rent_difference": None,
        "summary_note": "Property already rented",
        "property_confirmed_available": False,
        "callback_requested_time": None,
    }) + "\n```"])
    result = await extract_transcript_signals(llm, transcript="A" * 200)
    assert result.property_confirmed_available is False
    assert result.summary_note == "Property already rented"


@pytest.mark.asyncio
async def test_extractor_falls_back_on_unparseable_output():
    llm = ScriptedLLM(["I'm sorry, I can't help with that."])
    result = await extract_transcript_signals(llm, transcript="A" * 200)
    assert isinstance(result, TranscriptExtraction)
    # Fallback returns defaults + hint in summary
    assert "failed" in result.summary_note.lower() or result.summary_note == ""


@pytest.mark.asyncio
async def test_extractor_survives_partial_json():
    """LLM returned JSON missing required keys — should not crash, should
    best-effort populate what it can."""
    llm = ScriptedLLM([json.dumps({"summary_note": "Callback next week", "invalid_field": 42})])
    result = await extract_transcript_signals(llm, transcript="A" * 200)
    assert result.summary_note == "Callback next week"


@pytest.mark.asyncio
async def test_extractor_prompt_includes_old_rent_when_supplied():
    llm = ScriptedLLM([json.dumps({
        "rent_updated": False, "new_rent_amount": None,
        "rent_difference": None, "summary_note": "",
        "property_confirmed_available": True, "callback_requested_time": None,
    })])
    await extract_transcript_signals(llm, transcript="A" * 200, old_rent_amount=1500)
    user_msg = llm.calls[0][-1]["content"]
    assert "1500" in user_msg
