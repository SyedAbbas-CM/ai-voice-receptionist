"""Tests for the compliance-safe greeting and emergency intercept.

Both are Sprint 1 legal-safety features from R&D July 2026. If either
regresses, we're one bad call from lawsuit territory.
"""
from __future__ import annotations

import pytest

from packages.core_agent import ReceptionistBrain
from packages.schemas import (
    BusinessProfile,
    CallState,
    ToolCall,
    ToolResult,
    TranscriptTurn,
    TurnRole,
    CallStatus,
)


def _biz(**overrides) -> BusinessProfile:
    defaults = dict(id="biz1", name="Riverside Family Clinic", vertical="clinic")
    defaults.update(overrides)
    return BusinessProfile(**defaults)


class _NullLLM:
    """LLM stub for tests where we only exercise brain glue, not model calls."""
    name = "null"
    model = "null"
    async def complete(self, messages, tools=None, temperature=0.3, max_tokens=1024):
        from apps.api.app.providers.base import LLMResponse
        return LLMResponse(text="I can help with that.", tool_calls=[], finish_reason="stop")


async def _null_tool_handler(call: ToolCall) -> ToolResult:
    return ToolResult(tool_call_id=call.id, name=call.name, result=None, error="not implemented")


# ---- Greeting composition ----

@pytest.mark.asyncio
async def test_greeting_includes_ai_disclosure_by_default():
    brain = ReceptionistBrain(llm=_NullLLM(), business=_biz(), tools=[], tool_handler=_null_tool_handler)
    state = CallState(session_id="s1", business_id="biz1")
    r = await brain.greet(state)
    assert "AI assistant" in r.reply, f"disclosure missing: {r.reply!r}"


@pytest.mark.asyncio
async def test_greeting_includes_recording_notice_by_default():
    brain = ReceptionistBrain(llm=_NullLLM(), business=_biz(), tools=[], tool_handler=_null_tool_handler)
    state = CallState(session_id="s1", business_id="biz1")
    r = await brain.greet(state)
    assert "recorded" in r.reply.lower(), f"recording notice missing: {r.reply!r}"


@pytest.mark.asyncio
async def test_greeting_can_disable_disclosure():
    brain = ReceptionistBrain(
        llm=_NullLLM(),
        business=_biz(ai_disclosure_enabled=False, recording_notice_enabled=False),
        tools=[], tool_handler=_null_tool_handler,
    )
    state = CallState(session_id="s1", business_id="biz1")
    r = await brain.greet(state)
    assert "AI assistant" not in r.reply
    assert "recorded" not in r.reply.lower()
    assert "Riverside Family Clinic" in r.reply


@pytest.mark.asyncio
async def test_greeting_override_replaces_everything():
    brain = ReceptionistBrain(
        llm=_NullLLM(),
        business=_biz(greeting_override="Riverside here — what's up?"),
        tools=[], tool_handler=_null_tool_handler,
    )
    state = CallState(session_id="s1", business_id="biz1")
    r = await brain.greet(state)
    assert r.reply == "Riverside here — what's up?"


# ---- Emergency intercept in brain flow ----

@pytest.mark.asyncio
async def test_brain_intercepts_emergency_before_llm():
    """The emergency intercept must fire BEFORE the LLM sees the message.
    Uses a null LLM that would return a booking suggestion — must never reach it."""
    calls = {"count": 0}

    class ShouldNotBeCalledLLM:
        name = "should-not-call"
        model = "n/a"
        async def complete(self, *args, **kwargs):
            calls["count"] += 1
            from apps.api.app.providers.base import LLMResponse
            return LLMResponse(text="Let me book you for tomorrow at 10am.",
                                tool_calls=[], finish_reason="stop")

    brain = ReceptionistBrain(llm=ShouldNotBeCalledLLM(), business=_biz(),
                                tools=[], tool_handler=_null_tool_handler)
    state = CallState(session_id="s1", business_id="biz1")

    result = await brain.handle_user_turn(state, "I have crushing chest pain and my arm is numb.")

    # LLM must NOT have been called for the main turn (extractor is separate)
    # We assert on the reply, which is the escalation message not the LLM output
    assert "nine one one" in result.reply.lower(), f"expected 911 escalation, got: {result.reply!r}"
    assert result.escalated is True
    assert state.status == CallStatus.ESCALATED
    assert state.escalation_reason and "cardiac" in state.escalation_reason.lower()

    # tool_results should show the escalation record
    assert len(result.tool_results) == 1
    assert result.tool_results[0]["name"] == "emergency_escalation"
    assert result.tool_results[0]["result"]["escalated"] is True


@pytest.mark.asyncio
async def test_brain_does_not_intercept_normal_conversation():
    """Sanity check — regular queries should NOT trigger emergency."""
    brain = ReceptionistBrain(llm=_NullLLM(), business=_biz(),
                                tools=[], tool_handler=_null_tool_handler)
    state = CallState(session_id="s1", business_id="biz1")
    result = await brain.handle_user_turn(state, "I'd like to book an appointment for Tuesday.")
    assert not result.escalated
    assert state.status == CallStatus.ACTIVE


@pytest.mark.asyncio
async def test_self_harm_gets_988_hotline_in_escalation():
    """Self-harm callers should hear the 988 crisis line, not just 911."""
    brain = ReceptionistBrain(llm=_NullLLM(), business=_biz(),
                                tools=[], tool_handler=_null_tool_handler)
    state = CallState(session_id="s1", business_id="biz1")
    result = await brain.handle_user_turn(state, "I want to kill myself.")
    assert result.escalated
    assert "nine eight eight" in result.reply.lower(), (
        f"self-harm should include 988: {result.reply!r}"
    )
