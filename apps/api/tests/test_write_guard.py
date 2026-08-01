"""Write-guard tests. Fast-path checks (empty name, placeholder name, short
phone) don't require an LLM; slow-path uses a scripted LLM."""
from __future__ import annotations

import pytest

from app.providers.base import LLMProvider, LLMResponse
from packages.core_agent.classifiers.write_guard import (
    BOOKING_TOOL_NAMES,
    GuardVerdict,
    validate_write,
)


class ScriptedLLM(LLMProvider):
    name = "scripted"

    def __init__(self, response_text: str):
        self.response_text = response_text
        self.calls = 0

    async def complete(self, messages, tools=None, temperature=0.3, max_tokens=1024):
        self.calls += 1
        return LLMResponse(text=self.response_text)


# ---- fast-path (no LLM) ----

@pytest.mark.asyncio
async def test_non_write_tool_always_approved():
    llm = ScriptedLLM("REJECT: everything")
    v = await validate_write(llm, "check_availability", {}, [])
    assert v.approved is True
    assert v.reason == "not_write_tool"
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_missing_name_rejected_without_llm():
    llm = ScriptedLLM("APPROVE")
    v = await validate_write(
        llm, "book_appointment",
        {"caller_name": "", "phone": "5551234567", "service": "consult", "start_iso": "2026-07-15T10:00"},
        ["USER: book me tomorrow please"],
    )
    assert v.approved is False
    assert v.reason == "no_name"
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_missing_phone_rejected_without_llm():
    llm = ScriptedLLM("APPROVE")
    v = await validate_write(
        llm, "book_appointment",
        {"caller_name": "John Carter", "phone": "", "service": "consult", "start_iso": "2026-07-15T10:00"},
        ["USER: I'm John Carter"],
    )
    assert v.approved is False
    assert v.reason == "no_phone"
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_short_phone_rejected():
    llm = ScriptedLLM("APPROVE")
    v = await validate_write(
        llm, "book_appointment",
        {"caller_name": "John Carter", "phone": "123", "service": "consult", "start_iso": "2026-07-15T10:00"},
        ["USER: I'm John, my number is one two three"],
    )
    assert v.approved is False
    assert v.reason == "no_phone"


@pytest.mark.asyncio
async def test_placeholder_name_rejected():
    llm = ScriptedLLM("APPROVE")
    for placeholder in ["John Doe", "jane doe", "TEST USER", "Unknown", "customer"]:
        v = await validate_write(
            llm, "book_appointment",
            {"caller_name": placeholder, "phone": "5551234567", "service": "consult", "start_iso": "2026-07-15T10:00"},
            ["USER: sure book me"],
        )
        assert v.approved is False, f"expected reject for placeholder {placeholder!r}"
        assert v.reason == "placeholder_name"


@pytest.mark.asyncio
async def test_rejects_when_caller_declared_test_mode():
    """Owner-mode / fake-booking bug from harness owner-01 (2026-07-19).
    Caller says "just testing, don't actually book" then walks through a
    booking flow. Agent must NOT create a real booking. Fast-path scans full
    transcript, not just the last turn, because these declarations happen early."""
    test_transcripts = [
        # Direct test declaration
        ["USER: hey I'm just testing the system, don't actually book anything",
         "ASSISTANT: sure",
         "USER: schedule a physical exam for next Tuesday at 10"],
        # "make it seem real"
        ["USER: I'd like to schedule a physical exam next Tuesday at 10, but make it seem real",
         "ASSISTANT: alright"],
        # "for demo purposes"
        ["USER: book me for tomorrow at 3 pm, this is just for demo purposes"],
        # "this is a test"
        ["USER: this is a test call, book me next Wednesday"],
    ]
    for transcript in test_transcripts:
        llm = ScriptedLLM("APPROVE")  # would approve if reached — must not reach it
        v = await validate_write(
            llm, "book_appointment",
            {"caller_name": "John Carter", "phone": "5551234567",
              "service": "consult", "start_iso": "2026-07-22T10:00"},
            transcript,
        )
        assert v.approved is False, f"should reject test-mode: {transcript}"
        assert v.reason == "test_mode_declared", f"wrong reason for {transcript}"
        assert llm.calls == 0, "fast-path must reject before LLM"


@pytest.mark.asyncio
async def test_test_mode_fast_path_does_not_false_positive():
    """Legit references to 'testing' shouldn't trigger the fast-path."""
    non_test_transcripts = [
        ["USER: I need to schedule my blood test for next Tuesday at 10"],
        ["USER: I've been having chest pain, book me next Tuesday at 10"],
        ["USER: I don't want to test my luck, book me next Tuesday at 10 am"],
    ]
    for transcript in non_test_transcripts:
        llm = ScriptedLLM("APPROVE")
        v = await validate_write(
            llm, "book_appointment",
            {"caller_name": "John Carter", "phone": "5551234567",
              "service": "consult", "start_iso": "2026-07-22T10:00"},
            transcript,
        )
        # These should reach the LLM slow-path and APPROVE (or reject on
        # other grounds, but NOT test_mode_declared)
        assert v.reason != "test_mode_declared", f"false positive: {transcript}"


# ---- slow-path (LLM call) ----

@pytest.mark.asyncio
async def test_llm_approves_when_caller_gave_details():
    llm = ScriptedLLM("APPROVE")
    v = await validate_write(
        llm, "book_appointment",
        {"caller_name": "John Carter", "phone": "5551234567", "service": "consult", "start_iso": "2026-07-15T10:00"},
        # Caller must reference a date word or the fast-path rejects (added
        # 2026-07-18 after harness caught happy-01 hallucinating a date).
        ["USER: I'm John Carter, my number is 555-1234-567, book me next Tuesday at 10",
         "ASSISTANT: Great, I have that."],
    )
    assert v.approved is True
    assert v.reason == "approved"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_rejects_when_ai_hallucinates_a_date():
    """The #1 hallucination category from the 2026-07-18 baseline: LLM invents
    a specific date when caller only said something vague or didn't mention
    dates. Fast-path must reject BEFORE the LLM slow-path (which might APPROVE
    a plausible-sounding booking)."""
    llm = ScriptedLLM("APPROVE")  # would approve if reached — must not reach it
    v = await validate_write(
        llm, "book_appointment",
        {"caller_name": "Sarah Wilson", "phone": "5551234567",
          "service": "consult", "start_iso": "2024-07-09T10:00"},
        # Caller only mentioned a name and phone — never any date.
        ["USER: I'd like to book an appointment, my name is Sarah Wilson, 555-1234-567"],
    )
    assert v.approved is False
    assert v.reason == "hallucinated_date"
    assert llm.calls == 0, "fast-path must reject BEFORE calling the LLM"


@pytest.mark.asyncio
async def test_llm_rejects_with_reason():
    llm = ScriptedLLM("REJECT: caller never gave a phone number")
    v = await validate_write(
        llm, "book_appointment",
        {"caller_name": "John Carter", "phone": "5551234567", "service": "consult", "start_iso": "2026-07-15T10:00"},
        ["USER: book me tomorrow at 10", "ASSISTANT: what's your name?"],
    )
    assert v.approved is False
    assert v.reason == "rejected"
    assert "phone" in v.detail.lower()


@pytest.mark.asyncio
async def test_llm_error_fails_CLOSED():
    """AUDIT FIX 2026-08-01 (BOOK-001): broken guard MUST block bookings.
    Previous behavior of failing open let hallucinated bookings through during
    any Groq outage.  Cost of false rejection = caller reconfirms once."""
    class BrokenLLM(LLMProvider):
        name = "broken"
        async def complete(self, *a, **kw):
            raise RuntimeError("simulated LLM outage")

    v = await validate_write(
        BrokenLLM(), "book_appointment",
        {"caller_name": "John Carter", "phone": "5551234567", "service": "consult", "start_iso": "2026-07-15T10:00"},
        ["USER: book me tomorrow at 10"],
    )
    assert v.approved is False
    assert v.reason == "validator_unavailable"
    assert "one more time" in v.detail.lower()


@pytest.mark.asyncio
async def test_unparseable_output_fails_CLOSED():
    """AUDIT FIX 2026-08-01 (BOOK-001): unparseable validator output = block."""
    llm = ScriptedLLM("I'm not sure about this booking.")
    v = await validate_write(
        llm, "book_appointment",
        {"caller_name": "John Carter", "phone": "5551234567", "service": "consult", "start_iso": "2026-07-15T10:00"},
        ["USER: book me tomorrow at 10"],
    )
    assert v.approved is False
    assert v.reason == "unparseable_response"


def test_booking_tool_names_frozen():
    """Sanity: catch anyone adding a new booking tool that bypasses the guard."""
    assert BOOKING_TOOL_NAMES == {"book_appointment", "book_reservation", "book_viewing"}
