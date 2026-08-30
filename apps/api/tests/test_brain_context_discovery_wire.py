"""Brain integration for task #150: DISCOVER_CONTEXT branch.

When the resolved service needs context (Follow-up visit → original
procedure/provider/date), brain injects the orchestrator's directive
INSTEAD of proceeding to booking-slot ASK_SLOT.

Also validates the Phase 2 instructions-delta wire per networking's
commit 3b99cbd: opening_system_prompt stashed on state, per-turn
delta populated on scope enter/exit.
"""
from __future__ import annotations

import pytest


class _CapturingLLM:
    name = "capturing"
    model = "capturing-model"

    def __init__(self, text="ok"):
        self.calls = []
        self._text = text

    async def complete(self, messages, *, tools=None, temperature=0.3,
                        max_tokens=200, site=""):
        self.calls.append({
            "messages": list(messages), "site": site,
        })
        from apps.api.app.providers.base import LLMResponse
        return LLMResponse(
            text=self._text, tool_calls=[],
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
    from packages.schemas import (
        BusinessProfile, BusinessHours, ServiceOffering,
    )
    tools = build_clinic_tools()
    business = BusinessProfile(
        id="biz1", name="Test Clinic", vertical="clinic",
        timezone="America/Chicago",
        hours=BusinessHours(
            monday="09:00-17:00", tuesday="09:00-17:00",
            wednesday="09:00-17:00", thursday="09:00-17:00",
            friday="09:00-17:00", saturday=None, sunday=None,
        ),
        services=[
            ServiceOffering(
                name="Follow-up visit", duration_minutes=30,
                description="",
            ),
            ServiceOffering(
                name="Adult cleaning", duration_minutes=45,
                description="",
            ),
        ],
    )
    return ReceptionistBrain(
        llm=llm, business=business, tools=tools,
        tool_handler=_null_tool_handler, extractor_llm=llm,
    )


# ── discovery orchestrator fires when needed ────────────


@pytest.mark.asyncio
async def test_follow_up_service_triggers_context_discovery(monkeypatch):
    """The Christiaan trigger: caller says 'a follow-up' → brain sees
    service resolves to 'Follow-up visit' → context discovery
    orchestrator opens → discovery directive is injected as system
    prompt on this turn."""
    class _S:
        next_action_policy_enabled = True
    import packages.core_agent.brain as _bm
    monkeypatch.setattr(_bm, "_brain_settings", _S())
    from packages.schemas import CallState
    llm = _CapturingLLM(text="Sure — a follow-up to what?")
    brain = _brain(llm)
    state = CallState(session_id="CAdisc1", business_id="biz1")
    await brain.handle_user_turn(state, "I'd like a follow-up")
    # Orchestrator opened + attached to state.
    assert getattr(state, "_context_discovery", None) is not None
    assert state._context_discovery.service_name == "Follow-up visit"
    # Directive is present in the LLM messages this turn (last system
    # message, most binding).
    sys_msgs = [
        m["content"] for m in llm.calls[0]["messages"]
        if m["role"] == "system"
    ]
    assert any("DISCOVERY" in m for m in sys_msgs), (
        f"expected DISCOVERY directive in system msgs; got: "
        f"{[m[:60] for m in sys_msgs]}"
    )


@pytest.mark.asyncio
async def test_non_discovery_service_does_not_open_orchestrator(
    monkeypatch,
):
    """A regular service (Adult cleaning) needs no context — the
    orchestrator should NOT open, existing ASK_SLOT flow proceeds."""
    class _S:
        next_action_policy_enabled = True
    import packages.core_agent.brain as _bm
    monkeypatch.setattr(_bm, "_brain_settings", _S())
    from packages.schemas import CallState
    llm = _CapturingLLM(text="Sure, what day?")
    brain = _brain(llm)
    state = CallState(session_id="CAdisc2", business_id="biz1")
    await brain.handle_user_turn(state, "I need a cleaning")
    # No orchestrator opened.
    assert getattr(state, "_context_discovery", None) is None
    # No DISCOVERY directive in messages.
    sys_msgs = [
        m["content"] for m in llm.calls[0]["messages"]
        if m["role"] == "system"
    ]
    assert not any("DISCOVERY" in m for m in sys_msgs)


@pytest.mark.asyncio
async def test_discovery_directive_replaces_policy_directive(monkeypatch):
    """Discovery directive is injected AFTER policy directive so LLM
    sees it as most binding.  This proves the LK-style narrow scope
    is what's actually driving the LLM on discovery turns."""
    class _S:
        next_action_policy_enabled = True
    import packages.core_agent.brain as _bm
    monkeypatch.setattr(_bm, "_brain_settings", _S())
    from packages.schemas import CallState
    llm = _CapturingLLM(text="ok")
    brain = _brain(llm)
    state = CallState(session_id="CAdisc3", business_id="biz1")
    await brain.handle_user_turn(state, "book me a follow-up")
    sys_msgs = [
        m["content"] for m in llm.calls[0]["messages"]
        if m["role"] == "system"
    ]
    # The LAST system message should be the DISCOVERY directive
    # (LLMs bind tightest on most-recent system).
    assert "DISCOVERY" in sys_msgs[-1]


# ── Phase 2 wire: opening_system_prompt + instructions_delta ─────


@pytest.mark.asyncio
async def test_opening_system_prompt_stashed_on_first_turn():
    """session_manager reads this at persist time to fill
    SessionRow.opening_system_prompt.  Should be set to brain.system_prompt."""
    from packages.schemas import CallState
    llm = _CapturingLLM(text="hi")
    brain = _brain(llm)
    state = CallState(session_id="CAopen1", business_id="biz1")
    await brain.handle_user_turn(state, "hi")
    assert getattr(state, "_opening_system_prompt", None) == (
        brain.system_prompt
    )


@pytest.mark.asyncio
async def test_opening_system_prompt_not_overwritten_on_later_turns():
    """Once set, stays set even when sub-agent scope fires."""
    from packages.schemas import CallState
    llm = _CapturingLLM(text="ok")
    brain = _brain(llm)
    state = CallState(session_id="CAopen2", business_id="biz1")
    await brain.handle_user_turn(state, "hi")
    first_opening = state._opening_system_prompt
    # Manually attach a slot prompt (simulating actor's stage step).
    class _P:
        instructions = "narrow scope instructions"
        tools_hint = ("update_phone_number",)
    state._slot_capture_prompt = _P()
    await brain.handle_user_turn(state, "555 one two three")
    assert state._opening_system_prompt == first_opening


@pytest.mark.asyncio
async def test_instructions_delta_populated_on_slot_capture_active(
    monkeypatch,
):
    """When slot-capture prompt is active on state, delta = that prompt's
    instructions."""
    from packages.schemas import CallState
    llm = _CapturingLLM(text="ok")
    brain = _brain(llm)
    state = CallState(session_id="CAdelt1", business_id="biz1")
    class _P:
        instructions = "SENTINEL_SLOT_PROMPT_INSTRUCTIONS_12345"
        tools_hint = ("update_phone_number",)
    state._slot_capture_prompt = _P()
    await brain.handle_user_turn(state, "555 one two three")
    assert state._pending_instructions_delta == (
        "SENTINEL_SLOT_PROMPT_INSTRUCTIONS_12345"
    )


@pytest.mark.asyncio
async def test_instructions_delta_populated_on_discovery(monkeypatch):
    """When discovery orchestrator is active, delta = its directive."""
    class _S:
        next_action_policy_enabled = True
    import packages.core_agent.brain as _bm
    monkeypatch.setattr(_bm, "_brain_settings", _S())
    from packages.schemas import CallState
    llm = _CapturingLLM(text="ok")
    brain = _brain(llm)
    state = CallState(session_id="CAdelt2", business_id="biz1")
    await brain.handle_user_turn(state, "book me a follow-up")
    delta = state._pending_instructions_delta
    assert delta and "DISCOVERY" in delta


@pytest.mark.asyncio
async def test_instructions_delta_null_on_regular_turn():
    """Regular non-scope turn → delta is None (session_manager writes
    NULL → judge falls back to opening prompt)."""
    from packages.schemas import CallState
    llm = _CapturingLLM(text="ok")
    brain = _brain(llm)
    state = CallState(session_id="CAdelt3", business_id="biz1")
    await brain.handle_user_turn(state, "hi")
    assert getattr(state, "_pending_instructions_delta", None) is None


@pytest.mark.asyncio
async def test_instructions_delta_exit_sentinel_on_scope_leave(monkeypatch):
    """Turn N in scope, turn N+1 back to wider → delta becomes
    'exit_scope' sentinel string so judge knows to resume opening."""
    class _S:
        next_action_policy_enabled = True
    import packages.core_agent.brain as _bm
    monkeypatch.setattr(_bm, "_brain_settings", _S())
    from packages.schemas import CallState
    llm = _CapturingLLM(text="ok")
    brain = _brain(llm)
    state = CallState(session_id="CAdelt4", business_id="biz1")
    # Turn 1: enter discovery.
    await brain.handle_user_turn(state, "book me a follow-up")
    assert state._last_delta_kind == "discovery"
    # Turn 2: complete all discovery tasks manually to force exit.
    disc = state._context_discovery
    disc.complete_current("filling")
    disc.complete_current("Dr. Chen")
    disc.complete_current("August 15th")
    # Turn 3 (or whichever): brain should see disc.is_complete() and
    # tear it down.
    await brain.handle_user_turn(state, "great")
    # Discovery cleared.
    assert state._context_discovery is None
    # And delta transitioned to exit_scope sentinel.
    assert state._pending_instructions_delta == "exit_scope"


# ── discovery clears when complete ────────────────────


@pytest.mark.asyncio
async def test_discovery_clears_when_all_tasks_done(monkeypatch):
    """After all context tasks complete, orchestrator is torn down so
    normal ASK_SLOT resumes on the next turn."""
    class _S:
        next_action_policy_enabled = True
    import packages.core_agent.brain as _bm
    monkeypatch.setattr(_bm, "_brain_settings", _S())
    from packages.schemas import CallState
    llm = _CapturingLLM(text="ok")
    brain = _brain(llm)
    state = CallState(session_id="CAclear", business_id="biz1")
    await brain.handle_user_turn(state, "follow-up please")
    assert state._context_discovery is not None
    # Manually complete tasks (in real flow, LLM extracts them from
    # caller answers — future work).
    d = state._context_discovery
    d.complete_current("filling")
    d.complete_current("Dr. Chen")
    d.complete_current("August 15th")
    # Next turn: orchestrator was complete → brain tears it down.
    await brain.handle_user_turn(state, "great")
    assert state._context_discovery is None
