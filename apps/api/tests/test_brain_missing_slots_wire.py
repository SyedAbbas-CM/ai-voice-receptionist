"""Brain-level test: state.missing is now populated → ASK_SLOT fires.

Regression lock for the BUG #146 fix. Brain.py at handle_user_turn
must pass a computed `missing` list to build_decision_state_with_signals
so NextActionPolicy sees non-empty missing on booking-intent turns and
returns action=ASK_SLOT. Live diagnosis: CA3dac680ae8661459bc74735603f2cbc9.
"""
from __future__ import annotations

import pytest


class _CapturingLLM:
    name = "capturing"
    model = "capturing-model"

    def __init__(self, response_text="ok"):
        self.calls = []
        self._response_text = response_text

    async def complete(
        self, messages, *, tools=None, temperature=0.3,
        max_tokens=200, site="",
    ):
        self.calls.append({
            "messages": list(messages), "site": site,
        })
        from apps.api.app.providers.base import LLMResponse
        return LLMResponse(
            text=self._response_text, tool_calls=[],
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
async def test_policy_fires_ask_slot_on_booking_intent_turn(monkeypatch):
    """The Abbas regression. When flag on + caller says booking phrase +
    known_slots empty → policy decision should be ASK_SLOT (not ANSWER)."""
    # Turn on the feature flag.
    class _S:
        next_action_policy_enabled = True
    import packages.core_agent.brain as _bm
    monkeypatch.setattr(_bm, "_brain_settings", _S())

    # Capture what policy decided by patching the policy class.
    decisions = []
    from packages.dialogue.next_action_policy import (
        NextActionPolicy, ConversationAction,
    )
    orig_decide = NextActionPolicy.decide

    def _spy_decide(self, state):
        d = orig_decide(self, state)
        decisions.append(d)
        return d
    monkeypatch.setattr(NextActionPolicy, "decide", _spy_decide)

    from packages.schemas import CallState
    llm = _CapturingLLM(response_text="ok")
    brain = _brain(llm)
    state = CallState(session_id="CAtest_ask_slot", business_id="biz1")

    await brain.handle_user_turn(state, "I want to book an appointment")

    # At least one decision was ASK_SLOT (not all ANSWER as pre-fix).
    assert decisions, "policy.decide never invoked"
    ask_slot_hits = [
        d for d in decisions
        if d.action == ConversationAction.ASK_SLOT
    ]
    assert ask_slot_hits, (
        f"expected ASK_SLOT decision on booking-intent turn; got "
        f"{[d.action.value for d in decisions]}"
    )
    # And it should ask for a real slot.
    assert ask_slot_hits[0].requested_slot in (
        "service", "date", "time", "caller_name", "phone",
    )


@pytest.mark.asyncio
async def test_policy_stays_answer_on_non_booking_turn(monkeypatch):
    """Non-booking utterance ('what are your hours?') should still fall
    through to ANSWER — regression guard for false positives on Bug #146
    fix."""
    class _S:
        next_action_policy_enabled = True
    import packages.core_agent.brain as _bm
    monkeypatch.setattr(_bm, "_brain_settings", _S())

    decisions = []
    from packages.dialogue.next_action_policy import (
        NextActionPolicy, ConversationAction,
    )
    orig_decide = NextActionPolicy.decide

    def _spy(self, s):
        d = orig_decide(self, s)
        decisions.append(d)
        return d
    monkeypatch.setattr(NextActionPolicy, "decide", _spy)

    from packages.schemas import CallState
    llm = _CapturingLLM(response_text="We're open 8-5 Monday to Friday.")
    brain = _brain(llm)
    state = CallState(session_id="CAtest_noask", business_id="biz1")

    await brain.handle_user_turn(state, "what are your hours?")

    ask_slot_hits = [
        d for d in decisions
        if d.action == ConversationAction.ASK_SLOT
    ]
    assert not ask_slot_hits, (
        f"unexpected ASK_SLOT on non-booking turn: "
        f"{[d.action.value for d in decisions]}"
    )
