"""Verify brain.py emits typed humanness events at the right sites.

2026-08-29: brain.py has three humanness event sites — empty-completion
watchdog first fire, rescue retry outcome, deterministic fallback.
Plus PolicyDecisionEvent when NextActionPolicy is on.

These tests exercise the brain path with a scripted LLM and confirm
the fake call_event_log picks up the typed events.
"""
from __future__ import annotations

import pytest

from packages.observability.humanness_events import (
    EmptyLlmCompletionEvent,
    EmptyLlmDeterministicFallbackEvent,
    EmptyLlmRescueEvent,
    PolicyDecisionEvent,
)


class _FakeLog:
    """In-memory log stand-in — captures emitted events."""

    def __init__(self):
        self.events = []

    def write(self, event):
        self.events.append(event)

    def by_kind(self, kind: str) -> list:
        return [e for e in self.events if e.kind == kind]


@pytest.fixture
def fake_log(monkeypatch):
    """Patch the call_event_log getter to return our fake."""
    log = _FakeLog()
    import packages.observability.call_event_log as _cel
    monkeypatch.setattr(_cel, "get_call_event_log", lambda: log)
    return log


# ── EmptyLlmCompletionEvent + rescue chain — direct emit smoke ──


def test_emit_empty_completion_event_directly(fake_log):
    """The typed event's emit helper wires cleanly through — smoke
    test the log capture layer since brain sites use the same helper."""
    from packages.observability.humanness_events import (
        emit_humanness_event,
    )
    emit_humanness_event(EmptyLlmCompletionEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
        user_text="I need help", site="brain.reply",
    ))
    assert len(fake_log.by_kind("empty_llm_completion")) == 1
    payload = fake_log.by_kind("empty_llm_completion")[0].payload
    assert payload["user_text"] == "I need help"
    assert payload["site"] == "brain.reply"


def test_emit_rescue_event_recovered(fake_log):
    from packages.observability.humanness_events import (
        emit_humanness_event,
    )
    emit_humanness_event(EmptyLlmRescueEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
        user_text="X", recovered_text=True,
    ))
    p = fake_log.by_kind("empty_llm_rescue")[0].payload
    assert p["recovered_text"] is True


def test_emit_deterministic_fallback_carries_text(fake_log):
    from packages.observability.humanness_events import (
        emit_humanness_event,
    )
    emit_humanness_event(EmptyLlmDeterministicFallbackEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
        user_text="Q",
        fallback_text="Sorry, I missed that — could you say it again?",
    ))
    p = fake_log.by_kind("empty_llm_deterministic_fallback")[0].payload
    assert "sorry" in p["fallback_text"].lower()


# ── PolicyDecisionEvent — direct emit ─────────────────────────


def test_emit_policy_decision_event(fake_log):
    from packages.observability.humanness_events import (
        emit_humanness_event,
    )
    emit_humanness_event(PolicyDecisionEvent(
        call_id="CA1", tenant_id="t1", session_id="s1",
        action="ask_slot", acknowledgment="ack_understood",
        delivery_intent="warm", max_tokens=40,
        requested_slot="phone",
        must_include_facts_count=2,
    ))
    p = fake_log.by_kind("policy_decision")[0].payload
    assert p["action"] == "ask_slot"
    assert p["requested_slot"] == "phone"
    assert p["must_include_facts_count"] == 2
    assert p["delivery_intent"] == "warm"


# ── brain integration — the actual watchdog sites ─────────────


class _ScriptedLLM:
    """Same shape as test_empty_completion_watchdog._ScriptedLLM."""

    name = "scripted"
    model = "scripted-model"

    def __init__(self, responses):
        self._queue = list(responses)
        self.call_count = 0
        self.call_sites = []

    async def complete(
        self, messages, *, tools=None, temperature: float = 0.3,
        max_tokens: int = 200, site: str = "",
    ):
        from packages.schemas import ToolCall
        self.call_count += 1
        self.call_sites.append(site)
        if not self._queue:
            raise AssertionError(
                f"ScriptedLLM exhausted at call #{self.call_count}"
            )
        spec = self._queue.pop(0)
        from apps.api.app.providers.base import LLMResponse
        tcs = []
        for tc_spec in spec.get("tool_calls", []) or []:
            tcs.append(ToolCall(
                id=tc_spec.get("id", "call_1"),
                name=tc_spec["name"],
                arguments=tc_spec.get("arguments", {}),
            ))
        return LLMResponse(
            text=spec.get("text", "") or "",
            tool_calls=tcs,
            finish_reason=("tool_calls" if tcs else "stop"),
            raw={},
        )


async def _null_tool_handler(call):
    from packages.schemas import ToolResult
    return ToolResult(
        tool_call_id=call.id, name=call.name, result={"stub": True},
    )


def _brain(llm):
    from packages.core_agent import ReceptionistBrain
    from packages.integrations.clinic_tools import build_clinic_tools
    from packages.schemas import BusinessProfile, BusinessHours
    tools = build_clinic_tools()
    business = BusinessProfile(
        id="biz1", name="Test Clinic", vertical="clinic",
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
async def test_brain_emits_empty_completion_typed_event_on_first_empty(
    fake_log,
):
    """First empty completion → typed EmptyLlmCompletionEvent lands
    in the log."""
    from packages.schemas import CallState
    llm = _ScriptedLLM([
        {"text": "", "tool_calls": []},           # first empty
        {"text": "Yes, I can help.", "tool_calls": []},  # rescue
    ])
    brain = _brain(llm)
    state = CallState(session_id="CAtest_empty", business_id="biz1")
    await brain.handle_user_turn(state, "I need help")

    empties = fake_log.by_kind("empty_llm_completion")
    assert len(empties) == 1, (
        f"expected exactly 1 empty_llm_completion event; "
        f"got kinds: {[e.kind for e in fake_log.events]}"
    )
    assert empties[0].payload["user_text"].startswith("I need help")
    assert empties[0].payload["site"] == "brain.reply"


@pytest.mark.asyncio
async def test_brain_emits_rescue_event_when_rescue_recovers(fake_log):
    """Empty → rescue succeeds → EmptyLlmRescueEvent with
    recovered_text=True."""
    from packages.schemas import CallState
    llm = _ScriptedLLM([
        {"text": "", "tool_calls": []},
        {"text": "Sure, I can help.", "tool_calls": []},
    ])
    brain = _brain(llm)
    state = CallState(session_id="CAtest_rescue", business_id="biz1")
    await brain.handle_user_turn(state, "Hi there")

    rescues = fake_log.by_kind("empty_llm_rescue")
    assert len(rescues) == 1
    assert rescues[0].payload["recovered_text"] is True


@pytest.mark.asyncio
async def test_brain_emits_deterministic_fallback_on_both_empty(fake_log):
    """Empty + rescue also empty → EmptyLlmDeterministicFallbackEvent
    fires with the canned fallback text."""
    from packages.schemas import CallState
    llm = _ScriptedLLM([
        {"text": "", "tool_calls": []},
        {"text": "", "tool_calls": []},  # rescue also empty
    ])
    brain = _brain(llm)
    state = CallState(session_id="CAtest_det", business_id="biz1")
    result = await brain.handle_user_turn(state, "Hello?")

    assert "sorry" in result.reply.lower()
    dfs = fake_log.by_kind("empty_llm_deterministic_fallback")
    assert len(dfs) == 1
    assert "sorry" in dfs[0].payload["fallback_text"].lower()
    # We should also have the earlier empty_llm_completion event.
    assert fake_log.by_kind("empty_llm_completion")


@pytest.mark.asyncio
async def test_brain_no_humanness_events_on_normal_reply(fake_log):
    """A normal non-empty reply should NOT fire any empty-completion
    typed events."""
    from packages.schemas import CallState
    llm = _ScriptedLLM([
        {"text": "Sure, I can help.", "tool_calls": []},
    ])
    brain = _brain(llm)
    state = CallState(session_id="CAtest_normal", business_id="biz1")
    result = await brain.handle_user_turn(state, "I need help")
    assert result.reply
    # No empty-completion events fired.
    assert not fake_log.by_kind("empty_llm_completion")
    assert not fake_log.by_kind("empty_llm_deterministic_fallback")
