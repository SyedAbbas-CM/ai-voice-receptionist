"""Regression lock for BUG #158.

Live diagnosis from test call CAc66749590f6e53986eec4210e49bb425
(networking's trace dump): 13 policy_decision events all with `known={}`
and `missing=[]`.  The caller said "I want a follow-up" on turn 2 but
the extractor's result (`service='Follow-up visit'`) never persisted to
state — every subsequent turn recomputed from scratch and asked
ask_slot(date) x6 then ask_slot(service) x5 before forcing a book.

Fix design: new persistent `state._collected_slots: dict[str, str]`
that outlives one handle_user_turn call.  Three write sites:

  1. utterance-derived service resolution (brain.py line ~557)
  2. discovery orchestrator completion (line ~614)
  3. successful booking tool receipts (post-tool loop)

`_extract_known_slots` reads from state._collected_slots FIRST, then
overlays tool-receipt values.

Test builds a state with 2 prior transcript turns (agent asked what
service, caller said "follow-up") then runs turn 3 ("with Dr Chen").
Fixed brain must:
  - carry service='Follow-up visit' into the turn (from state)
  - pass known={'service': 'Follow-up visit'} to NextActionPolicy
  - missing[] must NOT include 'service' anymore
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
    from packages.schemas import BusinessProfile, BusinessHours, ServiceOffering
    tools = build_clinic_tools()
    business = BusinessProfile(
        id="biz1", name="Test", vertical="clinic",
        timezone="America/Chicago",
        hours=BusinessHours(
            monday="09:00-17:00", tuesday="09:00-17:00",
            wednesday="09:00-17:00", thursday="09:00-17:00",
            friday="09:00-17:00", saturday=None, sunday=None,
        ),
        services=[
            ServiceOffering(
                name="Follow-up visit", duration_minutes=30,
                description="Recheck after a procedure.",
            ),
            ServiceOffering(
                name="Adult cleaning", duration_minutes=45,
                description="Routine cleaning.",
            ),
        ],
    )
    return ReceptionistBrain(
        llm=llm, business=business, tools=tools,
        tool_handler=_null_tool_handler, extractor_llm=llm,
    )


def _seed_prior_turns(state, exchanges):
    """Append (role, text) pairs to state.transcript in order."""
    from packages.schemas import TranscriptTurn, TurnRole
    for role, text in exchanges:
        r = TurnRole.ASSISTANT if role == "agent" else TurnRole.USER
        state.transcript.append(TranscriptTurn(role=r, text=text))


@pytest.mark.asyncio
async def test_service_persists_across_turns_from_utterance(monkeypatch):
    """Turn 1 caller says 'follow-up' → service must persist into turn 2."""
    class _S:
        next_action_policy_enabled = True
    import packages.core_agent.brain as _bm
    monkeypatch.setattr(_bm, "_brain_settings", _S())

    from packages.schemas import CallState
    llm = _CapturingLLM(response_text="ok")
    brain = _brain(llm)
    state = CallState(session_id="CApersist_1", business_id="biz1")

    # Turn 1: caller mentions the service.
    await brain.handle_user_turn(state, "I want to book a follow-up")

    # After turn 1 the persistent slot dict MUST contain the service.
    collected = getattr(state, "_collected_slots", None)
    assert collected is not None, (
        "state._collected_slots must be created by brain when service "
        "is resolved from utterance"
    )
    assert collected.get("service") == "Follow-up visit", (
        f"expected service='Follow-up visit' persisted; got {collected!r}"
    )


@pytest.mark.asyncio
async def test_policy_sees_persisted_service_on_later_turn(monkeypatch):
    """Turn 2: no service keyword, but service is already in _collected_slots
    from turn 1.  Policy must see known={'service': 'Follow-up visit'} and
    missing must NOT include 'service'."""
    class _S:
        next_action_policy_enabled = True
    import packages.core_agent.brain as _bm
    monkeypatch.setattr(_bm, "_brain_settings", _S())

    decisions = []
    from packages.dialogue.next_action_policy import (
        NextActionPolicy,
    )
    orig_decide = NextActionPolicy.decide

    def _spy(self, s):
        d = orig_decide(self, s)
        decisions.append((s.known.copy(), list(s.missing), d))
        return d
    monkeypatch.setattr(NextActionPolicy, "decide", _spy)

    from packages.schemas import CallState
    llm = _CapturingLLM(response_text="ok")
    brain = _brain(llm)
    state = CallState(session_id="CApersist_2", business_id="biz1")

    # Simulate turn 1 already happened: seed transcript + persistent slots.
    _seed_prior_turns(state, [
        ("agent", "Hi, what can we help with today?"),
        ("caller", "I need to book a follow-up"),
        ("agent", "Sure, what day works for you?"),
    ])
    state._collected_slots = {"service": "Follow-up visit"}

    # Turn 2: caller says something with NO service keyword.
    await brain.handle_user_turn(state, "sometime tomorrow works")

    assert decisions, "policy.decide never invoked on turn 2"
    known_last, missing_last, _dec = decisions[-1]
    assert known_last.get("service") == "Follow-up visit", (
        f"policy did not see persisted service on turn 2; "
        f"known={known_last!r}"
    )
    assert "service" not in missing_last, (
        f"'service' should be satisfied, not in missing; "
        f"missing={missing_last!r}"
    )


@pytest.mark.asyncio
async def test_tool_receipt_merges_into_collected_slots(monkeypatch):
    """When a booking tool runs successfully, its arguments merge into
    state._collected_slots so the NEXT turn's policy has them too."""
    class _S:
        next_action_policy_enabled = True
    import packages.core_agent.brain as _bm
    monkeypatch.setattr(_bm, "_brain_settings", _S())

    from packages.schemas import (
        CallState, ToolCall, ToolResult,
    )
    from apps.api.app.providers.base import LLMResponse

    # LLM that returns a tool call on turn 1, then plain text.
    class _ToolCallingLLM:
        name = "tc"
        model = "tc-model"
        _call_count = 0

        def __init__(self):
            self.calls = []

        async def complete(
            self, messages, *, tools=None, temperature=0.3,
            max_tokens=200, site="",
        ):
            self.calls.append({"site": site})
            self._call_count += 1
            if self._call_count == 1:
                return LLMResponse(
                    text="",
                    tool_calls=[ToolCall(
                        id="tc1", name="check_availability",
                        arguments={
                            "service": "Adult cleaning",
                            "date": "2026-09-05",
                            "time": "10:00",
                        },
                    )],
                    finish_reason="tool_calls", raw={},
                )
            return LLMResponse(
                text="Great, that works.", tool_calls=[],
                finish_reason="stop", raw={},
            )

    async def _ok_handler(call):
        return ToolResult(
            tool_call_id=call.id, name=call.name,
            result={"available": True, "slot": "2026-09-05T10:00:00"},
            arguments=call.arguments,
        )

    from packages.core_agent import ReceptionistBrain
    from packages.integrations.clinic_tools import build_clinic_tools
    from packages.schemas import BusinessProfile, BusinessHours, ServiceOffering
    tools = build_clinic_tools()
    business = BusinessProfile(
        id="biz1", name="T", vertical="clinic",
        timezone="America/Chicago",
        hours=BusinessHours(
            monday="09:00-17:00", tuesday="09:00-17:00",
            wednesday="09:00-17:00", thursday="09:00-17:00",
            friday="09:00-17:00", saturday=None, sunday=None,
        ),
        services=[
            ServiceOffering(
                name="Adult cleaning", duration_minutes=45,
                description="Routine.",
            ),
        ],
    )
    llm = _ToolCallingLLM()
    brain = ReceptionistBrain(
        llm=llm, business=business, tools=tools,
        tool_handler=_ok_handler, extractor_llm=llm,
    )
    state = CallState(session_id="CApersist_3", business_id="biz1")

    await brain.handle_user_turn(state, "book me a cleaning tomorrow 10am")

    collected = getattr(state, "_collected_slots", None)
    assert collected is not None
    assert collected.get("service") == "Adult cleaning", (
        f"tool-receipt service missing from _collected_slots; "
        f"got {collected!r}"
    )
    assert collected.get("date") == "2026-09-05", (
        f"tool-receipt date missing from _collected_slots; "
        f"got {collected!r}"
    )
