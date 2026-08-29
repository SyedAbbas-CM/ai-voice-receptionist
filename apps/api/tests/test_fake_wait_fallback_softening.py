"""BUG #147 fix: fake-wait fallback phrasing softened.

Diagnosed 2026-08-29 from CA3dac680ae8661459bc74735603f2cbc9. Old
fallback was: 'Actually, let me ask you directly — what day and time
are you looking for?' — user complaint: 'gets a bit aggressive'.

New behavior: rotate through a pool of natural receptionist phrasings.
Each fire uses next entry via a state-level counter so consecutive fires
don't sound identical.
"""
from __future__ import annotations

import re

import pytest


class _WaitPromiseLLM:
    """Always returns a wait-promise text with no tool calls, forcing
    the fake-wait guard to fire."""
    name = "waiter"
    model = "wait-promise"

    def __init__(self):
        self.calls = 0

    async def complete(self, messages, *, tools=None, temperature=0.3,
                        max_tokens=200, site=""):
        self.calls += 1
        from apps.api.app.providers.base import LLMResponse
        return LLMResponse(
            text="Let me check availability for you.",
            tool_calls=[],  # NO tool call → fake-wait triggers
            finish_reason="stop", raw={},
        )


async def _null_tool_handler(call):
    from packages.schemas import ToolResult
    return ToolResult(
        tool_call_id=call.id, name=call.name, result={},
    )


def _brain(llm):
    from packages.core_agent import ReceptionistBrain
    from packages.integrations.clinic_tools import build_clinic_tools
    from packages.schemas import BusinessProfile, BusinessHours
    tools = build_clinic_tools()
    business = BusinessProfile(
        id="biz1", name="Test", vertical="clinic",
        timezone="America/Chicago",
        hours=BusinessHours(
            monday="09:00-17:00", tuesday="09:00-17:00",
            wednesday="09:00-17:00", thursday="09:00-17:00",
            friday="09:00-17:00", saturday=None, sunday=None,
        ),
    )
    return ReceptionistBrain(
        llm=llm, business=business, tools=tools,
        tool_handler=_null_tool_handler, extractor_llm=llm,
    )


@pytest.mark.asyncio
async def test_old_jarring_phrase_never_returned():
    """The exact old phrase that caused the user complaint MUST NOT
    appear in the fallback anymore."""
    from packages.schemas import CallState
    llm = _WaitPromiseLLM()
    brain = _brain(llm)
    state = CallState(session_id="CAsoft_1", business_id="biz1")
    result = await brain.handle_user_turn(state, "book me an appointment")
    assert "let me ask you directly" not in result.reply.lower()
    assert "actually," not in result.reply.lower()[:20], (
        "old fallback started with 'Actually,' — must not appear"
    )


@pytest.mark.asyncio
async def test_fallback_is_a_natural_receptionist_line():
    """The new fallback should sound like something a real
    receptionist would say — question about day/time OR general help
    offer, no meta-language."""
    from packages.schemas import CallState
    llm = _WaitPromiseLLM()
    brain = _brain(llm)
    state = CallState(session_id="CAsoft_2", business_id="biz1")
    result = await brain.handle_user_turn(state, "book me an appointment")
    reply = result.reply.lower()
    # One of: 'day', 'time', 'come in', 'when', 'help' — the natural
    # things a receptionist keeps the conversation alive with.
    assert re.search(r"\b(day|time|come in|when|help|works)\b", reply), (
        f"softened fallback should sound like natural receptionist "
        f"speech; got: {result.reply!r}"
    )
    # And should NOT contain the meta-language patterns.
    for banned in (
        "let me ask you directly",
        "let me ask directly",
        "asking directly",
    ):
        assert banned not in reply, f"banned meta-phrase leaked: {banned}"


@pytest.mark.asyncio
async def test_consecutive_fires_rotate_phrases():
    """Two back-to-back fires on the same state should produce two
    DIFFERENT fallback lines — the rotation counter works."""
    from packages.schemas import CallState
    llm = _WaitPromiseLLM()
    brain = _brain(llm)
    state = CallState(session_id="CAsoft_3", business_id="biz1")
    r1 = await brain.handle_user_turn(state, "book me an appointment")
    r2 = await brain.handle_user_turn(state, "book me an appointment")
    assert r1.reply != r2.reply, (
        f"consecutive fake-wait fires produced identical text — "
        f"rotation broken: {r1.reply!r} == {r2.reply!r}"
    )


@pytest.mark.asyncio
async def test_fresh_state_starts_at_first_fallback():
    """Different call sessions rotate independently — each starts at
    index 0."""
    from packages.schemas import CallState
    llm_a = _WaitPromiseLLM()
    llm_b = _WaitPromiseLLM()
    ba = _brain(llm_a)
    bb = _brain(llm_b)
    sa = CallState(session_id="CAsoft_4a", business_id="biz1")
    sb = CallState(session_id="CAsoft_4b", business_id="biz1")
    ra = await ba.handle_user_turn(sa, "book")
    rb = await bb.handle_user_turn(sb, "book")
    # First fire on independent states — same fallback (index 0).
    assert ra.reply == rb.reply
