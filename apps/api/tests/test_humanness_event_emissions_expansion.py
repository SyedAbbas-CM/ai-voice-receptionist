"""Task #141: emission tests for the four remaining humanness events.

TurnSignalReducedEvent + LlmClaimGuardEvent + SpeechGateDroppedEvent
+ BargeInDetectedEvent all had pydantic classes since commit 8ebd441
but no production emission sites.  This test file validates the
sites now emit correctly.

Direct-emit smoke tests only — full brain/actor integration is
covered by the existing suite; here we just verify the emit helper
lands the payload correctly through a fake log.
"""
from __future__ import annotations

import pytest


class _FakeLog:
    def __init__(self):
        self.events = []

    def write(self, event):
        self.events.append(event)

    def by_kind(self, kind: str) -> list:
        return [e for e in self.events if e.kind == kind]


@pytest.fixture
def fake_log(monkeypatch):
    log = _FakeLog()
    import packages.observability.call_event_log as _cel
    monkeypatch.setattr(_cel, "get_call_event_log", lambda: log)
    return log


# ── TurnSignalReducedEvent direct emit (fired from brain.py) ──


def test_turn_signal_reduced_emission_shape(fake_log):
    from packages.observability.humanness_events import (
        TurnSignalReducedEvent, emit_humanness_event,
    )
    emit_humanness_event(TurnSignalReducedEvent(
        call_id="CAtest",
        tenant_id="t1",
        session_id="s1",
        last_caller_text="my tooth really hurts",
        caller_shared_hardship=True,
        reasons=["hardship_kw:hurt"],
    ))
    events = fake_log.by_kind("turn_signal_reduced")
    assert len(events) == 1
    payload = events[0].payload
    assert payload["caller_shared_hardship"] is True
    assert "hardship_kw:hurt" in payload["reasons"]


# ── LlmClaimGuardEvent — booking + wait_promise variants ─────


def test_llm_claim_guard_booking_variant(fake_log):
    from packages.observability.humanness_events import (
        LlmClaimGuardEvent, emit_humanness_event,
    )
    emit_humanness_event(LlmClaimGuardEvent(
        call_id="CAtest",
        tenant_id="t1",
        session_id="s1",
        guard="booking",
        claim_text_preview="you're all set for Tuesday",
        receipt_present=False,
        action_taken="rewrote",
    ))
    events = fake_log.by_kind("llm_claim_guard")
    assert len(events) == 1
    payload = events[0].payload
    assert payload["guard"] == "booking"
    assert payload["receipt_present"] is False
    assert payload["action_taken"] == "rewrote"


def test_llm_claim_guard_wait_promise_variant(fake_log):
    from packages.observability.humanness_events import (
        LlmClaimGuardEvent, emit_humanness_event,
    )
    emit_humanness_event(LlmClaimGuardEvent(
        call_id="CAtest",
        tenant_id="t1",
        session_id="s1",
        guard="wait_promise",
        claim_text_preview="let me check the calendar",
        receipt_present=False,
    ))
    events = fake_log.by_kind("llm_claim_guard")
    assert len(events) == 1
    assert events[0].payload["guard"] == "wait_promise"


# ── SpeechGateDroppedEvent direct emit ────────────────────────


def test_speech_gate_dropped_emission_shape(fake_log):
    from packages.observability.humanness_events import (
        SpeechGateDroppedEvent, emit_humanness_event,
    )
    emit_humanness_event(SpeechGateDroppedEvent(
        call_id="CAtest",
        tenant_id="t1",
        session_id="s1",
        category="wait_promise",
        sentence_preview="Let me check the calendar",
    ))
    events = fake_log.by_kind("speech_gate_dropped")
    assert len(events) == 1
    assert events[0].payload["category"] == "wait_promise"


# ── BargeInDetectedEvent direct emit ─────────────────────────


def test_barge_in_detected_real_kind(fake_log):
    from packages.observability.humanness_events import (
        BargeInDetectedEvent, emit_humanness_event,
    )
    emit_humanness_event(BargeInDetectedEvent(
        call_id="CAtest",
        tenant_id="t1",
        session_id="s1",
        kind="real",
        word_count=5,
    ))
    events = fake_log.by_kind("barge_in_detected")
    assert len(events) == 1
    assert events[0].payload["kind"] == "real"
    assert events[0].payload["word_count"] == 5


def test_barge_in_detected_backchannel_kind(fake_log):
    from packages.observability.humanness_events import (
        BargeInDetectedEvent, emit_humanness_event,
    )
    emit_humanness_event(BargeInDetectedEvent(
        call_id="CAtest",
        tenant_id="t1",
        session_id="s1",
        kind="backchannel",
        word_count=1,
    ))
    events = fake_log.by_kind("barge_in_detected")
    assert events[0].payload["kind"] == "backchannel"


def test_barge_in_detected_false_positive_kind(fake_log):
    from packages.observability.humanness_events import (
        BargeInDetectedEvent, emit_humanness_event,
    )
    emit_humanness_event(BargeInDetectedEvent(
        call_id="CAtest",
        tenant_id="t1",
        session_id="s1",
        kind="false_positive",
        word_count=0,
    ))
    assert fake_log.by_kind("barge_in_detected")[0].payload["kind"] == (
        "false_positive"
    )


# ── brain integration: turn_signal fires per turn ────────


class _ScriptedLLM:
    name = "scripted"
    model = "scripted-model"

    def __init__(self, text="ok"):
        self.calls = []
        self._text = text

    async def complete(self, messages, *, tools=None, temperature=0.3,
                        max_tokens=200, site=""):
        self.calls.append(site)
        from apps.api.app.providers.base import LLMResponse
        return LLMResponse(
            text=self._text, tool_calls=[],
            finish_reason="stop", raw={},
        )


async def _null_tool_handler(call):
    from packages.schemas import ToolResult
    return ToolResult(
        tool_call_id=call.id, name=call.name, result={"stub": True},
    )


def _brain(llm):
    from packages.core_agent import ReceptionistBrain
    from packages.integrations.clinic_tools import build_clinic_tools
    from packages.schemas import (
        BusinessProfile, BusinessHours, ServiceOffering,
    )
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
                name="Adult cleaning", duration_minutes=45,
                description="",
            ),
        ],
    )
    return ReceptionistBrain(
        llm=llm, business=business,
        tools=build_clinic_tools(),
        tool_handler=_null_tool_handler,
        extractor_llm=llm,
    )


@pytest.mark.asyncio
async def test_brain_emits_turn_signal_reduced_when_policy_active(
    fake_log, monkeypatch,
):
    """When NextActionPolicy flag is on, brain emits a
    TurnSignalReducedEvent per turn from the reduced signals in
    _decision_state."""
    class _S:
        next_action_policy_enabled = True
    import packages.core_agent.brain as _bm
    monkeypatch.setattr(_bm, "_brain_settings", _S())
    from packages.schemas import CallState
    llm = _ScriptedLLM(text="hi")
    brain = _brain(llm)
    state = CallState(session_id="CAtsr1", business_id="biz1")
    await brain.handle_user_turn(state, "my tooth is killing me")
    events = fake_log.by_kind("turn_signal_reduced")
    assert len(events) >= 1
    payload = events[0].payload
    assert "tooth" in payload["last_caller_text"]
